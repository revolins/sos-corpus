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


def is_secretary_label(label: str, secretary: str) -> bool:
    lab = re.sub(r'[^A-Z ]', ' ', (label or '').upper())
    lab = ' '.join(lab.split())
    if lab in ('SECRETARY', 'THE SECRETARY', 'SECRETARY OF STATE'):
        return True
    sur = SURNAMES.get(secretary, '')
    return bool(sur) and bool(re.search(rf'\b{sur}\b', lab)) and not _NOT_SECRETARY.search(
        lab.replace('SECRETARY', ''))


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
                         'STUDENT', 'STUDENTS', 'TEACHER', 'REPORTER', 'INTERPRETER', 'SPEAKER'})
_HONORIFICS = frozenset({'MR.', 'MS.', 'MRS.', 'DR.', 'ST.'})


def _plausible_inline_label(label: str, text: str, pos: int) -> bool:
    """A mid-line speaker label, not "... in the U.S.: the same", "the OSCE:" or
    "BEGIN TEXT:": it starts a line or follows the end of a sentence, its words are
    letters (honorifics aside), and a one-word label is a known role."""
    before = text[max(0, pos - 3):pos]
    if pos and not re.search(r'(?:^|\n|[.?!"”)\]]\s+)$', text[max(0, pos - 40):pos]) \
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
    head = ' '.join(text.split()[:words])
    if len(head) < 12:
        return None
    return re.search(_ws(head), raw)


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


def extend_natural(natural: str, sos: str, raw: str) -> str | None:
    """Natural Text that ends (or starts) inside the SOS Text it should contain:
    the Raw Text from the earlier of their starts to the later of their ends.
    None when not provable."""
    if len(natural.split()) >= len(sos.split()) * 0.9:
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
_KNOWN_GARBLES = {'theQUESTION: uartet': 'the Quartet', 'alQUESTION: aeda': 'al-Qaeda'}


def _fix_garbles(text: str) -> str:
    for bad, good in _KNOWN_GARBLES.items():
        text = text.replace(bad, good)
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
    step('page_chrome', strip_chrome(sos), strip_chrome(nat))
    if sos != nat:  # a transcript: SOS Text is the Secretary's turns only
        step('other_speaker_turns', remove_other_turns(sos, nat, sec))
        step('other_speaker_turns', remove_inline_other_turns(sos, nat, sec))
    step('stage_directions', strip_combined_stage(sos))
    step('cut_start', restore_cut_start(sos, raw), restore_cut_start(nat, raw))
    ext = extend_natural(nat, sos, raw)
    step('natural_extended', None, ext)
    step('duplicate_paragraph', dedupe_paragraphs(sos, raw), dedupe_paragraphs(nat, raw))
    out = {}
    if sos != sos0:
        out['SOS Text'] = sos
    if nat != nat0:
        out['Natural Text'] = nat
    return out, fired
