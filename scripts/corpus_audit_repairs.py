"""Text repairs found by the 2026-10-27 whole-corpus audit (docs/corpus_audit_1027.md).

Each repair is narrow and checked against the row's own Raw Text or Natural Text,
so it only removes text proven to be someone else's or only adds text proven to sit
next to the existing text on the page:

  remove_other_turns       another speaker's labelled turn left inside SOS Text
                           ("... Thank you. PRESIDENT PERES: We share their pain ...")
                           -- each non-Secretary turn of Natural Text is removed from
                           SOS Text, label included
  strip_combined_stage     "(Applause and cheers.)", "(Laughter, applause.)",
                           "(crosstalk)": stage directions strip_stage_directions misses
  restore_cut_start        a text that starts mid-word ("d morning, everyone.") gets
                           the cut word completed from Raw Text
  extend_natural           a Natural Text shorter than its SOS Text is extended over
                           the Raw Text span the SOS Text covers
  strip_chrome             page chrome left at the start ("Share this page on:") or
                           end (the Bureau of Public Affairs site footer) of a text
  fix_replacement_chars    U+FFFD runs introduced after scraping are replaced by the
                           Raw Text characters at the same place (or dropped)
  dedupe_paragraphs        a paragraph repeated in the text but not on the page

repair_row(row) applies them in that order and returns {field: new text} with the
names of the repairs that fired.
"""

from __future__ import annotations

import bisect
import re

from corpus_finalize_v4_30 import normalize_encoding

try:
    import rubio_turns as _rt
except ImportError:  # pragma: no cover
    _rt = None

SURNAMES = {
    'Christopher': 'CHRISTOPHER', 'Albright': 'ALBRIGHT', 'Powell': 'POWELL', 'Rice': 'RICE',
    'Clinton': 'CLINTON', 'Kerry': 'KERRY', 'Tillerson': 'TILLERSON', 'Pompeo': 'POMPEO',
    'Blinken': 'BLINKEN', 'Rubio': 'RUBIO',
}
_NOT_SECRETARY = re.compile(r'\b(?:MRS|MS|SENATOR|GOVERNOR|AMBASSADOR|PRESIDENT|MR)\b')


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def is_secretary_label(label: str, secretary: str) -> bool:
    """The row's own Secretary: "SECRETARY KERRY", "THE SECRETARY", and the page's
    typos ("SECRETARY ABRIGHT", "SECRETARY RUBI", "SECREATRY RUBIO")."""
    lab = re.sub(r'[^A-Z ]', ' ', (label or '').upper())
    lab = ' '.join(lab.split())
    if lab in ('SECRETARY', 'THE SECRETARY', 'SECRETARY OF STATE'):
        return True
    sur = SURNAMES.get(secretary, '')
    if not sur or _NOT_SECRETARY.search(lab.replace('SECRETARY', '')):
        return False
    words = lab.split()
    if sur in words:
        return True
    tol = 1 if len(sur) <= 5 else 2
    return (any(len(w) >= 4 and _edit_distance(w, sur) <= tol for w in words)
            and (len(words) == 1 or any(_edit_distance(w, 'SECRETARY') <= 2 for w in words)))


def _ws(text: str) -> str:
    """Whitespace-tolerant regex for text (words joined by \\s+)."""
    return r'\s+'.join(re.escape(w) for w in text.split())


_PAREN_NOTE_RE = re.compile(r'\(\s*[^()]{0,60}\)')


def _turn_body_regex(body: str) -> str:
    """Regex for another speaker's turn as it appears in SOS Text, where stage
    notes such as "(Via interpreter)" and "(Laughter.)" were already stripped."""
    return _ws(_PAREN_NOTE_RE.sub(' ', body))


# ---------------------------------------------------------------------------

def remove_other_turns(sos: str, natural: str, secretary: str) -> str:
    """SOS Text without the labelled turns Natural Text gives to other speakers."""
    if _rt is None or not natural:
        return sos
    out = sos
    for t in _rt.segment(natural, stop_at_footer=False):
        body = (t.text or '').strip()
        if not t.label or is_secretary_label(t.label, secretary) or len(body.split()) < 2:
            continue
        if {'TEXT', 'BEGIN', 'END'} & set(t.label.upper().split()):
            continue  # "BEGIN TEXT:" opens a written statement, not another speaker
        # the label as it appears in the text: words separated by spaces/newlines,
        # an optional space or newline before the colon
        lab = r'\s+'.join(re.escape(w) for w in t.label.split())
        pat = re.compile(rf'(?:(?<=\s)|^){lab}\s*:\s*{_turn_body_regex(body)}')
        out = pat.sub('', out)
    if out == sos:
        return sos
    out = re.sub(r'[ \t]+\n', '\n', out)
    out = re.sub(r'\n{3,}', '\n\n', out)
    return re.sub(r'  +', ' ', out).strip()


# An upper-case speaker label anywhere in a line ("... thank you. PRESIDENT PERES: We
# share ..."); the colon must be followed by whitespace, so "10:30" never matches.
_INLINE_LABEL_RE = re.compile(
    r"(?<![A-Za-z])((?:[A-Z][A-Z.'’\-]*\s+){0,5}[A-Z][A-Z.'’\-]{2,})\s*(?:\([^()\n]{0,30}\)\s*)?:(?=\s)")


_ROLE_WORDS = frozenset({'QUESTION', 'MODERATOR', 'AUDIENCE', 'PARTICIPANT', 'PARTICIPANTS',
                         'STUDENT', 'STUDENTS', 'TEACHER', 'REPORTER', 'INTERPRETER', 'SPEAKER',
                         'SECRETARY', 'ANNOUNCER', 'STAFF'}) | frozenset(SURNAMES.values())
_HONORIFICS = frozenset({'MR.', 'MS.', 'MRS.', 'DR.', 'ST.'})


def _plausible_inline_label(label: str, text: str, pos: int) -> bool:
    """A mid-line speaker label, not "... in the U.S.: the same", "the OSCE:" or
    "BEGIN TEXT:": it starts a line or follows the end of a sentence, its words are
    letters (honorifics aside), and a one-word label is a known role."""
    before = text[max(0, pos - 3):pos]
    if pos and not re.search(r'(?:^|\n|[.?!"”)\]…:,;–—-]\s*|--\s*)$', text[max(0, pos - 40):pos]) \
            and not before.endswith('\n'):
        return False
    words = label.split()
    if any(w not in _HONORIFICS and not re.fullmatch(r"[A-ZÀ-Ý][A-ZÀ-Ý'’\-]+", w) for w in words):
        return False
    if 'TEXT' in words or 'BEGIN' in words or 'END' in words:
        return False
    return len(words) > 1 or words[0] in _ROLE_WORDS


def remove_inline_other_turns(sos: str, natural: str, secretary: str) -> str:
    """Second pass of remove_other_turns for labels that sit mid-line in Natural
    Text too, so line-based segmentation never splits them off: each non-Secretary
    turn runs from its label to the next label of anyone, and exactly that span is
    removed from SOS Text."""
    labels = [m for m in _INLINE_LABEL_RE.finditer(natural or '')
              if _plausible_inline_label(m.group(1), natural, m.start())]
    out = sos
    for k, m in enumerate(labels):
        if is_secretary_label(m.group(1), secretary):
            continue
        end = labels[k + 1].start() if k + 1 < len(labels) else len(natural)
        words = _PAREN_NOTE_RE.sub(' ', natural[m.end():end]).split()
        if not words:
            continue
        lab = r'\s+'.join(re.escape(w) for w in m.group(1).split())
        # the whole turn, else its longest opening of 20+ words (the old extraction
        # sometimes mangled a turn's last words at the next label)
        head = rf'(?:(?<=\s)|^){lab}\s*(?:\([^()\n]{{0,30}}\)\s*)?:\s*'

        def found(n: int) -> bool:
            return re.search(head + _ws(' '.join(words[:n])), out) is not None
        if found(len(words)):
            n = len(words)
        elif len(words) > 20 and found(20):
            lo, hi = 20, len(words) - 1          # found(lo) holds, found(hi + 1) fails
            while lo < hi:
                mid = (lo + hi + 1) // 2
                lo, hi = (mid, hi) if found(mid) else (lo, mid - 1)
            n = lo
        else:
            continue
        out = re.sub(head + _ws(' '.join(words[:n])), '', out)
    if out == sos:
        return sos
    out = re.sub(r'[ \t]+\n', '\n', out)
    out = re.sub(r'\n{3,}', '\n\n', out)
    return re.sub(r'  +', ' ', out).strip()


_LINE_START_LABEL_RE = re.compile(r"(?m)^\s*([A-Z][A-Z .'’\-]{1,60}?)\s*(?:\([^()\n]{0,30}\)\s*)?:")


_SPLIT_NOTE_LABEL_RE = re.compile(r"(?m)^([A-Z][A-Z .'’\-]{1,60}?)[ \t]*\n\([ \t]*\n?([^()\n]{0,60}?)[ \t]*\n?\)?[ \t]*\n:")


def join_split_labels(natural: str) -> str:
    """"QUESTION\n(\nunofficial translation from Czech\n: What is ..." -> "QUESTION
    (unofficial translation from Czech): What is ...", so the label is found."""
    return _SPLIT_NOTE_LABEL_RE.sub(lambda m: f'{m.group(1)} ({m.group(2).strip()}):', natural)


_SPLIT_NOTE_LABEL2_RE = re.compile(
    r"(?m)^([A-Z][A-Z .'’\-]{1,60}?)[ \t]*\n\([ \t]*\n?([^()\n]{0,60}?)[ \t]*\n?\)[ \t]*\n?(?::[ \t]*)?")
_BARE_LABEL_LINE_RE = re.compile(r"(?m)^([A-Z][A-Z .'’\-]{2,60}?)[ \t]*(?:\n\(|:?[ \t]*$)")


def sos_from_labelled_natural(sos: str, natural: str, secretary: str) -> str | None:
    """SOS Text that still shows another speaker's label line ("QUESTION\n(unofficial
    translation from Czech)\nMr. President Putin ..."; rows 975, 3077): rebuild it as
    the Secretary's turns of Natural Text, with labels split over lines joined first."""
    if _rt is None:
        return None
    labs = [m.group(1).strip() for m in _BARE_LABEL_LINE_RE.finditer(sos)]
    if not any(len(x.split()) <= 6 and not is_secretary_label(x, secretary) and
               (x in _ROLE_WORDS or len(x.split()) >= 2) for x in labs):
        return None
    joined = _SPLIT_NOTE_LABEL2_RE.sub(lambda m: f'{m.group(1)} ({m.group(2).strip()}):', natural)
    joined = re.sub(r'(?m)^(QUESTION|MODERATOR|PARTICIPANT|AUDIENCE)[ \t]*$', r'\1:', joined)
    turns = _rt.segment(joined, stop_at_footer=False)
    mine = [t.text.strip() for t in turns if t.label and is_secretary_label(t.label, secretary)]
    new = '\n\n'.join(x for x in mine if x)
    if not new or len(new.split()) < 0.3 * len(sos.split()):
        return None
    return new


def remove_foreign_paragraphs(sos: str, natural: str, secretary: str) -> str:
    """Third pass for other speakers' words: an SOS paragraph that Natural Text puts
    inside another speaker's turn. The old extraction took a vocative ("QUESTION:
    Secretary Kerry, Syria's main opposition ...") for the Secretary's own label, so
    the rest of the question became SOS Text. That turn's words, from where the
    paragraph starts to the next label, are removed; any of the Secretary's words
    merged into the same paragraph stay."""
    labels = [(m.start(), m.group(1)) for m in _LINE_START_LABEL_RE.finditer(natural)]
    labels += [(m.start(), m.group(1)) for m in _INLINE_LABEL_RE.finditer(natural)
               if _plausible_inline_label(m.group(1), natural, m.start())]
    if _rt is not None:   # labels split over lines ("UNDER SECRETARY CATHY\nNOVELLI\n:")
        labels += [(max(t.start - 1, 0), t.label) for t in _rt.segment(natural, stop_at_footer=False)
                   if t.label]
    labels.sort()
    if not labels:
        return sos
    starts = [p for p, _ in labels]
    out = []
    changed = False
    for para in re.split(r'(\n\s*\n)', sos):
        if not para.strip() or len(para.split()) < 4 or re.fullmatch(r'\n\s*\n', para):
            out.append(para)
            continue
        m = (_raw_position(natural, para.strip()) or _raw_position(natural, para.strip(), 5)
             if para.strip()[:1] in ',;' else _raw_position(natural, para.strip()))
        k = bisect.bisect_right(starts, m.start()) - 1 if m else -1
        if k < 0 or is_secretary_label(labels[k][1], secretary):
            out.append(para)
            continue
        end = starts[k + 1] if k + 1 < len(starts) else len(natural)
        words = _PAREN_NOTE_RE.sub(' ', natural[m.start():end]).split()
        rest = None
        for n in range(len(words), 0, -1):
            g = re.match(r'\s*' + _ws(' '.join(words[:n])), para)
            if g:
                rest = para[g.end():].strip()
                break
        if rest is None:
            out.append(para)
            continue
        changed = True
        if rest:
            out.append(rest)
    if not changed:
        return sos
    text = ''.join(out)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'^(?:\s*\n)+|(?:\n\s*)+$', '', text)
    if len(text.split()) < 0.2 * len(sos.split()):
        return sos   # would remove nearly everything: left for review, not repaired blind
    return text


def _turn_labels(natural: str) -> list[tuple[int, str]]:
    labels = [(m.start(), m.group(1)) for m in _LINE_START_LABEL_RE.finditer(natural)]
    labels += [(m.start(), m.group(1)) for m in _INLINE_LABEL_RE.finditer(natural)
               if _plausible_inline_label(m.group(1), natural, m.start())]
    if _rt is not None:
        labels += [(max(t.start - 1, 0), t.label) for t in _rt.segment(natural, stop_at_footer=False)
                   if t.label]
    return sorted(labels)


def drop_vocative_remainders(sos: str, natural: str, secretary: str, raw: str = '') -> str:
    """Paragraphs that open with a comma are the rest of someone else's sentence after
    "Secretary <Name>" ("QUESTION: Secretary Pompeo, thank you for having us." became
    SOS ", thank you for having us. Hugh, it's great to be with you ..."). The speaker's
    words up to the next label in Natural Text are removed; the Secretary's answer
    merged into the same paragraph stays, and so does a comma-led paragraph that is
    the Secretary's own (a statement whose subject or a hyperlinked word was lost):
    text is removed only where the page puts it inside another speaker's labelled
    turn -- Natural Text, else Raw Text. A comma-led line of eight words or fewer
    (", welcome.", ", please.") is dropped outright."""
    sur = SURNAMES.get(secretary, '')
    if not sur or ',' not in sos:
        return sos
    sources = [natural] + ([raw] if raw else [])
    out, changed = [], False
    for part in re.split(r'(\n\s*\n)', sos):
        q = part.strip()
        if not q.startswith(','):
            out.append(part)
            continue
        if len(q.split()) <= 8:
            changed = True
            continue
        first = re.split(r'(?<=[.?!])\s', q, maxsplit=1)[0].split()[:12]
        voc = re.compile(rf'Secretary\s+(?:of\s+State\s+)?(?:[A-Z][\w.\'’-]*\s+){{0,3}}{sur.title()}\s*'
                         + r'(?:\s+|\s*\([^()\n]{0,40}\)\s*)+'.join(re.escape(w) for w in first))
        words = None
        for src in sources:
            m = voc.search(src)
            if not m:
                continue
            labels = _turn_labels(src)
            starts = [x for x, _ in labels]
            body_start = src.index(first[0], m.start())
            k = bisect.bisect_right(starts, body_start)
            if k == 0 or is_secretary_label(labels[k - 1][1], secretary):
                break                       # not inside another speaker's turn
            end = starts[k] if k < len(starts) else len(src)
            words = _PAREN_NOTE_RE.sub(' ', src[body_start:end]).split()
            break
        if not words:
            out.append(part)
            continue
        rest = None
        for n in range(len(words), 0, -1):
            g = re.match(r'\s*' + _ws(' '.join(words[:n])), q)
            if g:
                rest = q[g.end():].strip()
                break
        if rest is None:
            out.append(part)
            continue
        changed = True
        if rest:
            out.append(rest)
    if not changed:
        return sos
    text = re.sub(r'\n{3,}', '\n\n', ''.join(out))
    text = re.sub(r'^(?:\s*\n)+|(?:\n\s*)+$', '', text)
    return text if text.strip() else sos


_STAGE_WORDS = r'(?:laughter|applause|cheers|cheering|crosstalk)'
_COMBINED_STAGE_RE = re.compile(
    rf'\s*\(\s*{_STAGE_WORDS}(?:\s*(?:[,/&]|and|\.)\s*{_STAGE_WORDS})*\s*\.?\s*\)', re.I)


def strip_combined_stage(text: str) -> str:
    out = _COMBINED_STAGE_RE.sub('', text)
    if out == text:
        return text
    out = re.sub(r'[ \t]+\n', '\n', out)
    return re.sub(r'\n{3,}', '\n\n', out)




def _raw_position(raw: str, text: str, words: int = 8) -> re.Match | None:
    """Where text's first words sit in raw; stage notes such as "(Cheers.)" or
    "(inaudible)", stripped from the text but kept on the page, may sit between them."""
    head = text.split()[:words]
    if len(' '.join(head)) < 12:
        return None
    return re.search(r'(?:\s+|\s*\([^()\n]{0,40}\)\s*)+'.join(re.escape(w) for w in head), raw)


def restore_cut_start(text: str, raw: str) -> str | None:
    """Text whose first word is cut ("d morning", "ank you"): complete that word
    from Raw Text. Only the cut word is restored -- a text that starts at a word
    boundary is left alone (what precedes it may be another speaker's). None when
    the cut is not provable."""
    body = text.lstrip()
    if not body or not re.match(r'[a-z]', body):
        return None
    m = _raw_position(raw, body)
    if not m or m.start() == 0 or not raw[m.start() - 1].isalpha():
        return None
    k = m.start()
    while k > 0 and (raw[k - 1].isalpha() or raw[k - 1] in "'’"):
        k -= 1
    missing = raw[k:m.start()]
    if not missing or len(missing) > 20:
        return None
    return missing + body


_MONTHS = 'January|February|March|April|May|June|July|August|September|October|November|December'
# Where a written statement's text starts on the page: after its release line,
# its "Share" widget or its dateline.
_STATEMENT_START_RES = (
    re.compile(r'As released by the Office of the Spokes(?:man|person)\s+U\.S\. Department of State\s*'),
    re.compile(r'Share this page on:\s*'),
    re.compile(rf'(?:{_MONTHS})\s+\d{{1,2}},\s+\d{{4}}\s*\n'),
    re.compile(rf'(?:{_MONTHS})\s+\d{{1,2}},\s+\d{{4}}\s+(?=[A-Z])'),   # flattened 2001-2009 pages
)
_CHROME_WORDS_RE = re.compile(r'Other State Department Archive Sites|You are in:|Skip to|Subject Index|'
                              r'Official websites use|Back to Top|Released on|Following are excerpts|'
                              r'\[Also see')
_LINE_LABEL_RE = re.compile(r"^\s*([A-Z][A-Z .'’\-]{1,60}?)\s*(?:\([^()\n]{0,30}\)\s*)?:\s*(.*)$")


def restore_statement_start(text: str, raw: str, title: str = '', secretary: str = '') -> str | None:
    """A label-free statement that starts mid-sentence (the old half-text fallback
    cut it): put back what the page has between the statement's start (release
    line, Share widget or dateline) and the cut. None when not provable."""
    body = text.lstrip()
    if (not body or not (body[0].islower() or body[0] in ',;')
            or _LINE_LABEL_RE.match(body.split('\n', 1)[0])):
        return None
    m = _raw_position(raw, body)
    if not m:
        return None
    head = raw[:m.start()]
    starts = [x.end() for r in _STATEMENT_START_RES for x in r.finditer(head)]
    if not starts:
        return None
    prefix = head[max(starts):]
    title_m = re.match(r'\s*' + _ws(title), prefix) if title else None
    if title_m and body[0].isalpha():   # the headline repeated (not a sentence's own subject)
        prefix = prefix[title_m.end():]
    lab = _LINE_LABEL_RE.match(prefix)                          # "SECREETARY ALBRIGHT:" (typo)
    if lab and SURNAMES.get(secretary, '#') in lab.group(1).upper():
        prefix = prefix[lab.start(2):]
    if (not re.search(r'[A-Za-z]', _PAREN_NOTE_RE.sub('', prefix)) or len(prefix.split()) > 600
            or _CHROME_WORDS_RE.search(prefix)
            or any(_LINE_LABEL_RE.match(line) for line in prefix.split('\n'))):
        return None
    if prefix[-1:].isalpha():
        return prefix + body
    if body[0] in ',;':
        return ' '.join(prefix.split()) + body
    return prefix.rstrip() + ' ' + body


def restore_turn_head(sos: str, natural: str, secretary: str) -> str | None:
    """SOS Text whose first words were dropped after the Secretary's own label
    ("SECRETARY POMPEO:\nGood\nmorning, everyone" -> SOS "morning, everyone"): put
    back the few words between the label and the SOS start. None when not provable."""
    body = sos.lstrip()
    m = _raw_position(natural, body)
    if not m:
        return None
    line_start = natural.rfind('\n', 0, m.start()) + 1
    between = natural[line_start:m.start()]
    lab = _LINE_LABEL_RE.match(between)
    if lab:
        if not is_secretary_label(lab.group(1), secretary):
            return None
        prefix = lab.group(2)
    else:
        prev = natural[:line_start].rstrip('\n').rsplit('\n', 1)[-1]
        plab = _LINE_LABEL_RE.match(prev)
        if not (plab and not plab.group(2).strip() and is_secretary_label(plab.group(1), secretary)):
            return None
        prefix = between
    prefix = _PAREN_NOTE_RE.sub(' ', prefix).strip()
    if not re.search(r'[A-Za-z]', prefix) or len(prefix.split()) > 12 or ':' in prefix:
        return None
    if not natural[m.start() - 1:m.start()].isspace():           # "It'" + "s an honor"
        return prefix + body
    return prefix + ' ' + body


def drop_leading_foreign_fragment(sos: str, natural: str, secretary: str) -> str | None:
    """SOS Text that opens with the rest of another speaker's sentence after a mention
    of the Secretary ("Secretary Albright | and I had an extensive discussion", the old
    extraction took "Secretary Albright" for a label): drop that speaker's turn from the
    start of SOS Text. None when not provable."""
    body = sos.lstrip()
    if not body or not body[0].islower():
        return None
    m = _raw_position(natural, body)
    sur = SURNAMES.get(secretary, '')
    if not m or not sur or not re.search(rf'Secretary\s+(?:of\s+State\s+)?(?:[A-Z][a-z.]+\s+)*{sur.title()}\s*$',
                                         natural[max(0, m.start() - 80):m.start()]):
        return None
    labels = [x for x in re.finditer(r"(?m)^\s*([A-Z][A-Z .'’\-]{1,60}?)\s*:", natural)]
    before = [x for x in labels if x.start() < m.start()]
    if not before or is_secretary_label(before[-1].group(1), secretary):
        return None
    after = [x for x in labels if x.start() > m.start()]
    end = after[0].start() if after else len(natural)
    words = _PAREN_NOTE_RE.sub(' ', natural[m.start():end]).split()
    for n in range(len(words), 0, -1):
        g = re.match(r'\s*' + _ws(' '.join(words[:n])), sos)
        if g:
            return sos[g.end():].lstrip()
    return None


def _anchor_end(raw: str, text: str, words: int = 8) -> int:
    toks = text.split()
    for k in range(len(toks) - words, max(len(toks) - 200, -1), -1):
        m = re.search(_ws(' '.join(toks[k:k + words])), raw)
        if m:
            return m.end()
    return -1


def _anchor_start(raw: str, text: str, before: int, words: int = 8) -> int:
    """Raw Text position of the text's first anchorable words: the last match
    before `before`, so a headline that repeats the opening words is skipped."""
    toks = text.split()
    for k in range(0, min(len(toks) - words, 200) + 1):
        hits = [m.start() for m in re.finditer(_ws(' '.join(toks[k:k + words])), raw)
                if m.start() < before]
        if hits:
            return hits[-1]
    return -1


_COV_WORD_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)?")


def _shingles(text: str, k: int = 5, by_paragraph: bool = False) -> set:
    parts = re.split(r'\n\s*\n', text) if by_paragraph else [text]
    out = set()
    for part in parts:
        t = [w.lower() for w in _COV_WORD_RE.findall(part.replace('’', "'"))]
        out |= {tuple(t[i:i + k]) for i in range(len(t) - k + 1)}
    return out


def coverage(text: str, source: str, by_paragraph: bool = True) -> float:
    """Share of text's 5-word sequences (within its paragraphs) found in source."""
    a = _shingles(text, by_paragraph=by_paragraph)
    return len(a & _shingles(source)) / len(a) if a else 1.0


def sos_from_natural(sos: str, natural: str, raw: str, secretary: str) -> str | None:
    """SOS Text that is not from this row's page at all (another item's text, e.g. 7467
    "U.S.-Spain Council" carried the Rio+20 speech) while Natural Text is: rebuild SOS
    Text from Natural Text -- the Secretary's turns, or all of it when unlabelled."""
    if len(_shingles(sos)) < 20 or coverage(sos, raw) >= 0.1 or coverage(natural, raw, False) < 0.9:
        return None
    if _rt is not None:
        turns = _rt.segment(natural, stop_at_footer=False)
        if any(t.label for t in turns):
            mine = [t.text.strip() for t in turns if t.label and is_secretary_label(t.label, secretary)]
            return '\n\n'.join(x for x in mine if x) or None
    return natural


def extend_natural(natural: str, sos: str, raw: str) -> str | None:
    """Natural Text that ends (or starts) inside the SOS Text it should contain:
    the Raw Text from the earlier of their starts to the later of their ends.
    None when not provable."""
    if len(natural.split()) >= len(sos.split()) * 0.9 and coverage(sos, natural) >= 0.9:
        return None
    ne, se = _anchor_end(raw, natural), _anchor_end(raw, sos)
    if min(ne, se) < 0:
        return None
    ns, ss = _anchor_start(raw, natural, ne), _anchor_start(raw, sos, se)
    if min(ns, ss) < 0 or max(ne, se) <= min(ns, ss):
        return None
    new = raw[min(ns, ss):max(ne, se)].strip()
    return new if len(new.split()) > len(natural.split()) else None


# Characters Raw Text itself has lost (it holds the mis-decoded "ï¿½" there too),
# restored by hand: Sándor Petőfi (row 5880), Epeli Hau'ofa (row 8948).
_KNOWN_LOST = {"Pet\ufffd'fi": 'Petőfi', 'Pet\ufffdfi': 'Petőfi', 'Hau\ufffd\ufffdofa': "Hau'ofa"}


_LEAD_CHROME_RE = re.compile(r'^.{0,400}?Share this page on:\s*', re.S)
_TAIL_CHROME_RE = re.compile(
    r'\s*The Office of (?:Website Management|Electronic Information), Bureau of Public Affairs, '
    r'manages this site.*$', re.S)


def strip_chrome(text: str) -> str:
    """Page chrome left at the start ("... Secretary of State June 10, 2020 Share this
    page on:") or end (the Bureau of Public Affairs site footer) of a text."""
    return _TAIL_CHROME_RE.sub('', _LEAD_CHROME_RE.sub('', text))


def tidy_paragraph_starts(text: str) -> str:
    """Paragraph-start debris: the colon of a label split over lines ("SECRETARY ALBRIGHT\n:
    Thank you" leaves ": Thank you"), the ")" of a half-stripped "(As Delivered)", and a
    paragraph break the page puts before a comma at a hyperlink ("pursuant to Executive
    Order 14098\n\n, which authorizes"), where the sentence runs on."""
    out = re.sub(r'(?<=[.?!])\s*\n\s*\n\s*\.(?=\s|$)', '', text)       # "home.\n\n. Thank" -> "home. Thank"
    out = re.sub(r'\s*\n\s*\n\s*(?=[,;.](?:\s|$))', '', out)
    out = re.sub(r'(^|\n\s*\n)[ \t]*[:)][ \t]*', r'\1', out)
    return out.lstrip() if out != text else text


def fix_replacement_chars(text: str, raw: str) -> str:
    """Replace each U+FFFD run with what Raw Text has between the same neighbours;
    where Raw Text has lost the character too, use a known spelling or drop the run
    (the runs trail sentences, where a closing quote or a non-breaking space was)."""
    if '\ufffd' not in text:
        return text
    for bad, good in _KNOWN_LOST.items():
        text = text.replace(bad, good)

    def repl(m: re.Match) -> str:
        pre = text[max(0, m.start() - 25):m.start()].split()
        post = text[m.end():m.end() + 25].split()
        pre_s = ' '.join(pre[1:]) if len(pre) > 1 else ' '.join(pre)
        post_s = ' '.join(post[:-1]) if len(post) > 1 else ' '.join(post)
        if pre_s and post_s:
            g = re.search(_ws(pre_s) + r'([^\n]{0,6}?)\s*' + _ws(post_s), raw)
            if g is not None and '\ufffd' not in g.group(1) and 'ï¿½' not in g.group(1):
                return g.group(1)
        return ''
    return re.sub('\ufffd+', repl, text)


def dedupe_paragraphs(text: str, raw: str) -> str:
    """Drop a repeat of a long paragraph that the page itself has only once."""
    paras = text.split('\n')
    seen, out = set(), []
    flat_raw = ' '.join(raw.split())
    for p in paras:
        key = ' '.join(p.split())
        if len(key) > 80 and key in seen and flat_raw.count(key) <= 1:
            continue
        seen.add(key)
        out.append(p)
    if len(out) == len(paras):
        return text
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(out))


# ---------------------------------------------------------------------------

# Text the original scrape garbled in one row (3778: the "Q" of "Quartet" and
# "al-Qaeda" turned into a fake QUESTION label).
_KNOWN_GARBLES = {'theQUESTION: uartet': 'the Quartet', 'alQUESTION: aeda': 'al-Qaeda',
}
# 684: the joint statement's subject was taken for a speaker label; only a text or
# paragraph that opens with the comma lost it.
_KNOWN_GARBLE_RES = ((re.compile(r'(?:^|(?<=\n\n))\s*, (?=Estonian President Meri, Latvian President Ulmanis)'),
                      'Secretary Albright, '),)


def _fix_garbles(text: str) -> str:
    for bad, good in _KNOWN_GARBLES.items():
        text = text.replace(bad, good)
    for pat, good in _KNOWN_GARBLE_RES:
        text = pat.sub(good, text)
    return text


def repair_row(row: dict) -> tuple[dict, list[str]]:
    """({field: new text}, [repairs that fired]) for one row."""
    sec = row.get('Secretary', '')
    # Raw Text normalised like the text fields (straight quotes, no mojibake), so
    # Raw Text and the text fields can be matched against each other.
    raw = normalize_encoding(row.get('Raw Text', '') or '')
    sos0, nat0 = row.get('SOS Text', '') or '', row.get('Natural Text', '') or ''
    sos, nat, fired = sos0, nat0, []

    def step(name, new_sos=None, new_nat=None):
        nonlocal sos, nat
        changed = False
        if new_sos is not None and new_sos != sos:
            sos, changed = new_sos, True
        if new_nat is not None and new_nat != nat:
            nat, changed = new_nat, True
        if changed:
            fired.append(name)

    step('known_garbles', _fix_garbles(sos), _fix_garbles(nat))
    step('replacement_chars', fix_replacement_chars(sos, raw), fix_replacement_chars(nat, raw))
    rebuilt = sos_from_natural(sos, nat, raw, sec)
    if rebuilt is not None:
        from corpus_finalize_v4_30 import strip_stage_directions
        step('sos_from_natural', strip_stage_directions(rebuilt))
    step('page_chrome', strip_chrome(sos), strip_chrome(nat))
    step('paragraph_starts', tidy_paragraph_starts(sos), tidy_paragraph_starts(nat))
    if sos != nat:  # a transcript: SOS Text is the Secretary's turns only
        rebuilt = sos_from_labelled_natural(sos, nat, sec)
        if rebuilt is not None:
            from corpus_finalize_v4_30 import strip_stage_directions
            step('other_speaker_turns', strip_stage_directions(rebuilt))
        nat_l = join_split_labels(nat)
        step('other_speaker_turns', remove_other_turns(sos, nat_l, sec))
        step('other_speaker_turns', remove_inline_other_turns(sos, nat_l, sec))
        step('other_speaker_turns', remove_foreign_paragraphs(sos, nat_l, sec))
        step('other_speaker_turns', remove_foreign_paragraphs(sos, nat_l, sec))
        step('vocative_remainder', drop_vocative_remainders(sos, nat_l, sec, join_split_labels(raw)))
    step('stage_directions', strip_combined_stage(sos))
    step('cut_start', restore_cut_start(sos, raw) or restore_cut_start(sos, nat),
         restore_cut_start(nat, raw))
    labelled = sos != nat and any(_LINE_LABEL_RE.match(x) for x in nat.split('\n')[:400])
    if labelled:
        step('foreign_fragment', drop_leading_foreign_fragment(sos, nat, sec)
             or drop_leading_foreign_fragment(sos, raw, sec))
        step('turn_head', restore_turn_head(sos, nat, sec))
    else:
        title = row.get('Title', '')
        new_sos = restore_statement_start(sos, raw, title, sec)
        step('statement_start', new_sos,
             new_sos if (new_sos and sos == nat) else restore_statement_start(nat, raw, title, sec))
    ext = extend_natural(nat, sos, raw)
    step('natural_extended', None, ext)
    step('duplicate_paragraph', dedupe_paragraphs(sos, raw), dedupe_paragraphs(nat, raw))
    out = {}
    if sos != sos0:
        out['SOS Text'] = sos
    if nat != nat0:
        out['Natural Text'] = nat
    return out, fired


# ---------------------------------------------------------------------------
# Verifier detectors (corpus_verify_research_grade Checks AK-AM)
# ---------------------------------------------------------------------------

def text_problems(row: dict) -> list[str]:
    """Why this row's text is not clean, by the same rules that repair it: the
    repairs repair_row would still make, plus SOS Text that is mostly not on the
    row's own page. [] for a clean row."""
    _, fired = repair_row(row)
    problems = sorted(set(fired))
    sos = row.get('SOS Text', '') or ''
    if len(sos.split()) > 30 and coverage(sos, normalize_encoding(row.get('Raw Text', '') or '')) < 0.5:
        problems.append('sos_not_on_page')
    return problems
