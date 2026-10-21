#!/usr/bin/env python3
"""rubio_turns.py — speaker-label segmenter for Rubio transcripts (plan step P1c).

Splits a state.gov transcript into speaker turns at UPPERCASE labels and decides
which turns are Secretary Rubio's.  Replaces, for the Rubio path only, the
case-insensitive `secretary_anchor_regex` + ASCII `UPPER_SPEAKER_LINE_RE` pairing in
hybrid_corpus_recovery.py, which other secretaries still use unchanged.

Failure modes addressed (docs/rubio_coverage_audit.md):
  X-03  vocative "Secretary Rubio, thank you..." inside another speaker's turn was
        taken as a Rubio anchor.  Labels here are case-sensitive and need a colon,
        and turns end only at labels.
  X-04  non-ASCII / NBSP / curly-apostrophe labels (RAMÍREZ, ŠEFČOVIČ, SA’AR,
        CHAIRMAN\\xa0RISCH) were not turn boundaries.  Detection is Unicode-aware.
  X-05  split labels from get_text('\\n'): "LABEL\\n:", "SEC\\nRETARY RUBIO:",
        "COMMISSIONER\\nŠEFČOVIČ\\n:".
  X-06  typo labels ("SECRETARY RUBI:", "SECREATRY RUBIO:") resolved by edit
        distance; "MRS RUBIO" and "SENATOR RUBIO" are explicitly NOT Rubio.
  X-10  "(In Spanish.)" placeholders standing alone in a Rubio turn are counted as
        untranscribed segments before stage directions are stripped.
  M-05  normalize_label() gives one canonical spelling per speaker for Speakers Present.

Public API (pure stdlib):
  segment(text)        -> list[Turn(label, is_rubio, text, start, end)]
  rubio_text(text)     -> (joined Rubio text, stats dict)
  is_rubio_label(lbl)  -> bool
  normalize_label(lbl) -> canonical label str
"""
import re
import sys
import unicodedata
from typing import List, NamedTuple, Optional, Tuple

# ---------------------------------------------------------------------------
# Character normalisation.  Every replacement is one char -> one char, so offsets
# computed on the normalised copy index the original text directly.
# ---------------------------------------------------------------------------
_CHAR_MAP = str.maketrans({
    '\xa0': ' ', ' ': ' ', ' ': ' ', ' ': ' ', '​': ' ',
    '‘': "'", '’': "'", 'ʼ': "'", '′': "'",
    '“': '"', '”': '"',
    '：': ':',          # fullwidth colon
    '\r': '\n',
})


def _normalize_chars(text: str) -> str:
    return (text or '').translate(_CHAR_MAP)


# ---------------------------------------------------------------------------
# Label shape
# ---------------------------------------------------------------------------
_LABEL_MIN, _LABEL_MAX = 3, 60
# Short labels that are real speaker labels despite being under _LABEL_MIN.
_SHORT_LABELS = frozenset({'Q', 'A', 'MR', 'MS'})
# Characters allowed in a label besides letters (any script) and digits.
_LABEL_PUNCT = set(" .'-&/,()")
# Single-word labels accepted in the inline "LABEL: text" form.  Multi-word inline
# labels and any label that resolves to Rubio are always accepted; this keeps an
# acronym at the start of a sentence ("NATO: ...") from ending a Rubio turn.
_INLINE_SINGLE_WORD = frozenset({
    'Q', 'QUESTION', 'MODERATOR', 'PARTICIPANT', 'PARTICIPANTS', 'AUDIENCE', 'STAFF',
    'INTERPRETER', 'REPORTER', 'ANNOUNCER', 'OPERATOR', 'HOST', 'NARRATOR',
})
# A no-colon uppercase line ending in one of these is the first half of a label
# split across lines ("COMMISSIONER\nŠEFČOVIČ\n:").
_TITLE_WORDS = frozenset({
    'SECRETARY', 'MINISTER', 'PRESIDENT', 'COMMISSIONER', 'AMBASSADOR', 'CHAIRMAN',
    'CHAIRWOMAN', 'CHAIR', 'SENATOR', 'CONGRESSMAN', 'CONGRESSWOMAN', 'GENERAL',
    'ADVISOR', 'ADVISER', 'ENVOY', 'ADMINISTRATOR', 'DIRECTOR', 'SPOKESPERSON',
    'GOVERNOR', 'MAYOR', 'KING', 'QUEEN', 'PRINCE', 'SHEIKH', 'EMIR', 'CHANCELLOR',
    'MR', 'MS', 'MRS', 'DR', 'MR.', 'MS.', 'MRS.', 'DR.',
})
# state.gov footer: an own line "Tags" / "Back to Top" closes the content block.
# (Never seen before the first label in any of the 814 v2026.10.19 Rubio rows.)
_FOOTER_LINES = frozenset({'Tags', 'Back to Top'})
# Trailing parenthetical on a label head: "(via interpreter)", "(In Spanish)".
_LABEL_PAREN_RE = re.compile(r'\s*\([^()]{0,40}\)\s*$')


def _is_label_shape(lbl: str) -> bool:
    """Unicode-aware: all cased letters uppercase, at least one letter, 3-60 chars."""
    lbl = lbl.strip()
    if not lbl:
        return False
    if lbl in _SHORT_LABELS:
        return True
    if not (_LABEL_MIN <= len(lbl) <= _LABEL_MAX):
        return False
    if lbl != lbl.upper() or not any(c.isalpha() for c in lbl):
        return False
    if not lbl[0].isalpha():
        return False
    for c in lbl:
        if not (c.isalpha() or c.isdigit() or c in _LABEL_PUNCT
                or unicodedata.category(c).startswith('M')):
            return False
    return True


# ---------------------------------------------------------------------------
# Label resolution
# ---------------------------------------------------------------------------
def normalize_label(label: str) -> str:
    """Canonical speaker label: uppercase, NBSP/curly quotes folded, periods dropped,
    hyphens/whitespace collapsed to single spaces.  Every Rubio variant (typos,
    DESIGNATE, OF STATE, MARCO, split words) maps to 'SECRETARY RUBIO'; MRS RUBIO and
    SENATOR RUBIO keep their own canonical forms."""
    c = _normalize_chars(label or '').upper()
    c = c.replace('.', ' ').replace('-', ' ').replace(':', ' ')
    c = ' '.join(c.split())
    if c.startswith('THE '):
        c = c[4:]
    if _resolve_rubio(c):
        return 'SECRETARY RUBIO'
    return c


# Titles that make a "... RUBIO" label someone other than the Secretary
# (audit D9: 'SENATOR RUBIO:' 2022 clip in 17194, 'MRS RUBIO' in 17610).
_NON_SECRETARY_TITLES = frozenset({
    'MRS', 'MR', 'MS', 'MISS', 'SENATOR', 'SEN', 'SENADOR', 'CONGRESSMAN',
    'REPRESENTATIVE', 'REP', 'MADAM', 'SENORA', 'SEÑORA',
})
_RUBIO_EXACT_RE = re.compile(
    r'^SECRETARY (?:DESIGNATE )?(?:OF STATE )?(?:MARCO )?RUBIO$'
)
_OPTIONAL_TOKENS = ('DESIGNATE', 'OF', 'STATE', 'MARCO')


def _edit_distance(a: str, b: str, cap: int = 3) -> int:
    """Levenshtein distance, early-exit once every cell exceeds `cap`."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _resolve_rubio(c: str) -> bool:
    """`c` is an already-canonicalised label (upper, no periods/hyphens)."""
    if not c:
        return False
    toks = c.split()
    if toks[0] in _NON_SECRETARY_TITLES:
        return False
    if _RUBIO_EXACT_RE.match(c):
        return True
    # Drop the optional middle tokens, then compare fuzzily.
    core = ' '.join(t for t in toks if t not in _OPTIONAL_TOKENS)
    if _edit_distance(core, 'SECRETARY RUBIO') <= 2:
        return True
    # Joined split word ("SEC RETARY RUBIO") — compare with spaces removed too.
    if _edit_distance(core.replace(' ', ''), 'SECRETARYRUBIO') <= 2:
        return True
    # Bare surname: distance <= 1 only (RUBI / RUBIOS), so RUBEN-like names stay out.
    if len(toks) == 1 and _edit_distance(core, 'RUBIO') <= 1:
        return True
    return False


def is_rubio_label(label: str) -> bool:
    """True when `label` (with or without trailing colon) names Secretary Rubio."""
    return normalize_label(label) == 'SECRETARY RUBIO'


def _is_suspicious(label: str) -> bool:
    """Non-Rubio labels worth logging: they mention RUB or SECR but are neither a
    clearly different person (SECRETARY HEGSETH) nor an explicit exclusion."""
    c = normalize_label(label)
    if c == 'SECRETARY RUBIO':
        return False
    toks = c.split()
    if toks and toks[0] in _NON_SECRETARY_TITLES and toks[-1] == 'RUBIO':
        return False                                  # MRS RUBIO / SENATOR RUBIO
    if 'RUB' in c:
        return True
    # A bare "SECRETARY" / "SECRETARY OF STATE" (whose?), or a mangled SECRETARY
    # token ("SECREATRY", "SECRETARYRUBI").  "SECRETARY HEGSETH" is not logged.
    if any('SECR' in t and t != 'SECRETARY' for t in toks):
        return True
    return bool(toks) and toks[0] == 'SECRETARY' and all(
        t in _OPTIONAL_TOKENS or t == 'SECRETARY' for t in toks)


# ---------------------------------------------------------------------------
# Stage directions
# ---------------------------------------------------------------------------
_LANGS = (r'spanish|french|arabic|korean|russian|chinese|mandarin|japanese|german|'
          r'portuguese|italian|hebrew|farsi|persian|turkish|polish|ukrainian|thai|'
          r'vietnamese|hindi|urdu|swahili|english|greek|hungarian|slovak|dutch|'
          r'tagalog|indonesian|malay|creole|haitian creole|guarani')
# "(In Spanish.)", "(Speaking in Spanish.)", "(Spanish)" — the untranscribed marker.
_LANGUAGE_MARKER_RE = re.compile(
    rf'\(\s*(?:(?:speaking|speaks|continues|responds|answers)\s+)?(?:in\s+)?(?:{_LANGS})'
    r'\s*\.?\s*\)', re.IGNORECASE)
# Capitalised language name not in _LANGS: "(In Icelandic.)", "(Speaking in Tagalog.)".
# Case-sensitive so "(in progress)" is not taken for a language.
_LANGUAGE_MARKER_CAP_RE = re.compile(
    r'\(\s*(?:[Ss]peaking\s+)?[Ii]n\s+[A-Z][a-z]{3,}(?:ic|ish|ese|an|ian|ch)\s*\.?\s*\)')
_STAGE_DIRECTION_RES = (
    re.compile(r'\(\s*(?:laughter(?:\s+and\s+applause)?|applause(?:\s+and\s+laughter)?|'
               r'sustained\s+applause|cheers?|cheering|clapping|booing|crosstalk|'
               r'laughter\s+off[- ]?mike)\s*\.?\s*\)', re.IGNORECASE),
    re.compile(r'\(\s*inaudible[^)]{0,30}\)', re.IGNORECASE),
    re.compile(r'\(\s*(?:via|through)\s+(?:an\s+)?(?:interpreter|interpretation|translation|'
               r'translator)\s*\.?\s*\)', re.IGNORECASE),
    re.compile(r'\(\s*(?:off[- ]?mike|off\s+the\s+record|break|in\s+progress|'
               r'closed\s+press\s+coverage|audio\s+break|technical\s+difficulties)\s*\.?\s*\)',
               re.IGNORECASE),
    re.compile(r'\(\s*(?:the\s+)?[^()]{3,60}?\s+(?:was|were|has\s+been|have\s+been)\s+'
               r'(?:signed|recited|presented|played|sung|performed|observed|awarded|given|'
               r'administered|exchanged|shown|read)\s*\.?\s*\)', re.IGNORECASE),
    _LANGUAGE_MARKER_RE,
    _LANGUAGE_MARKER_CAP_RE,
)


def _strip_stage(s: str) -> str:
    for rx in _STAGE_DIRECTION_RES:
        s = rx.sub('', s)
    return s.strip(' \t.:;,-\u2013\u2014')


def _count_untranscribed(body: str) -> int:
    """Count "(In Spanish.)"-type placeholders whose words were never transcribed.

    A marker at the start of a line with text after it on the same line introduces a
    translation ("(In Spanish.) We are working on that." -- 17658, 17655) and is not
    counted.  A marker alone on its line (16920) or in mid-line ("an honor to be
    (in Spanish).  But..." -- 17045, 16911, 17189) is a gap.  "(In English)" only
    signals a switch back to English (17357) and is never counted."""
    n = 0
    for line in body.split('\n'):
        for rx in (_LANGUAGE_MARKER_RE, _LANGUAGE_MARKER_CAP_RE):
            for m in rx.finditer(line):
                if 'english' in m.group(0).lower():
                    continue
                if rx is _LANGUAGE_MARKER_CAP_RE and _LANGUAGE_MARKER_RE.match(m.group(0)):
                    continue                          # already counted by the list regex
                before, after = _strip_stage(line[:m.start()]), _strip_stage(line[m.end():])
                if not before and after:
                    continue                          # translation follows the marker
                n += 1
    return n


def _clean_turn(body: str) -> str:
    t = body.replace('\xa0', ' ')
    for rx in _STAGE_DIRECTION_RES:
        t = rx.sub('\x00', t)
    # "an honor to be (in Spanish)." -> "an honor to be." rather than "be ."
    t = re.sub(r'\s*\x00\s*(?=[.,;:?!])', '', t).replace('\x00', ' ')
    return re.sub(r'\s+', ' ', t).strip()


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------
class Turn(NamedTuple):
    label: Optional[str]      # canonical label (normalize_label), None for preamble
    is_rubio: bool
    text: str                 # cleaned body: stage directions removed, whitespace collapsed
    start: int                # body offsets in the input text: text_in[start:end]
    end: int


class _Label(NamedTuple):
    raw: str                  # label as written (fragments joined), no colon
    lstart: int               # offset where the label begins (ends the previous turn)
    bstart: int               # offset where the turn body begins


def _lines(norm: str) -> List[Tuple[int, int, str]]:
    """(start, end, stripped_line) for every line of `norm`."""
    out = []
    pos = 0
    for raw in norm.split('\n'):
        out.append((pos, pos + len(raw), raw.strip()))
        pos += len(raw) + 1
    return out


def _next_nonblank(lines, i):
    j = i + 1
    while j < len(lines) and not lines[j][2]:
        j += 1
    return j if j < len(lines) else None


def _prev_nonblank(lines, i):
    j = i - 1
    while j >= 0 and not lines[j][2]:
        j -= 1
    return j if j >= 0 else None


def _find_labels(norm: str) -> Tuple[List[_Label], int]:
    """Return (labels, content_end).  content_end is the footer line offset (or len)."""
    lines = _lines(norm)
    labels: List[_Label] = []
    used = set()               # line indexes consumed as part of a label
    content_end = len(norm)
    i = 0
    while i < len(lines):
        ls, le, s = lines[i]
        if not s:
            i += 1
            continue
        if labels and s in _FOOTER_LINES:
            content_end = ls
            break
        found = None           # (raw_label, first_label_line_idx, body_start, last_line_idx)
        lead = ls + (len(norm[ls:le]) - len(norm[ls:le].lstrip()))
        colon = s.find(':')
        if colon > 0:
            head, tail = s[:colon].strip(), s[colon + 1:].strip()
            # "FOREIGN MINISTER WANG (via interpreter):" -- the lowercase
            # parenthetical would fail the all-caps shape test and leak the
            # speaker's words into the open Rubio turn; judge the label without it.
            head = _LABEL_PAREN_RE.sub('', head) or head
            if _is_label_shape(head):
                if not tail:
                    found = (head, i, lead + colon + 1, i)
                else:
                    multi = len(head.split()) > 1
                    if multi or head in _INLINE_SINGLE_WORD or is_rubio_label(head):
                        found = (head, i, lead + colon + 1, i)
        elif _is_label_shape(s):
            k = _next_nonblank(lines, i)
            if k is not None and lines[k][2].startswith(':'):
                # "LABEL\n:" or "LABEL\n: text"
                kls, kle, ks = lines[k]
                kcol = kls + (len(norm[kls:kle]) - len(norm[kls:kle].lstrip()))
                found = (s, i, kcol + 1, k)
            elif k is not None:
                # three-line "COMMISSIONER\nŠEFČOVIČ\n:" — first fragment ends in a title
                ks = lines[k][2]
                m = _next_nonblank(lines, k)
                if (m is not None and lines[m][2].startswith(':') and ':' not in ks
                        and _is_label_shape(ks) and s.split()[-1] in _TITLE_WORDS):
                    mls, mle, _ = lines[m]
                    mcol = mls + (len(norm[mls:mle]) - len(norm[mls:mle].lstrip()))
                    found = (s + ' ' + ks, i, mcol + 1, m)
        if found is None:
            i += 1
            continue
        raw, first, bstart, last = found
        lstart = lines[first][0] + (len(norm[lines[first][0]:lines[first][1]])
                                    - len(norm[lines[first][0]:lines[first][1]].lstrip()))
        # Join a split-off leading fragment on the previous line ("SEC\nRETARY RUBIO:",
        # "COMMISSIONER\nŠEFČOVIČ:").  Only when the join resolves to Rubio and the
        # label alone does not, or when the fragment ends in a title word and the
        # label is a single word -- never for headings like "AS PREPARED".
        p = _prev_nonblank(lines, first)
        if p is not None and p not in used:
            ps = lines[p][2]
            if ':' not in ps and _is_label_shape(ps) and len(ps) + len(raw) < _LABEL_MAX:
                joined = None
                if not is_rubio_label(raw):
                    for cand in (ps + raw, ps + ' ' + raw):
                        if is_rubio_label(cand):
                            joined = cand
                            break
                if (joined is None and len(raw.split()) == 1
                        and raw not in _INLINE_SINGLE_WORD and ps.split()[-1] in _TITLE_WORDS):
                    joined = ps + ' ' + raw
                if joined is not None:
                    raw = joined
                    lstart = lines[p][0] + (len(norm[lines[p][0]:lines[p][1]])
                                            - len(norm[lines[p][0]:lines[p][1]].lstrip()))
                    used.add(p)
        labels.append(_Label(raw, lstart, bstart))
        used.update(range(first, last + 1))
        i = last + 1
    return labels, content_end


def segment(text: str, stop_at_footer: bool = True) -> List[Turn]:
    """Split `text` into speaker turns.

    Text before the first label is returned as a preamble Turn with label=None.
    A turn runs from its label to the next label (or, with stop_at_footer, to an
    own-line "Tags"/"Back to Top" footer marker after the first label).  No minimum
    turn length: a one-word "Okay." turn is kept.
    """
    text = text or ''
    norm = _normalize_chars(text)
    labels, content_end = _find_labels(norm)
    if not stop_at_footer:
        content_end = len(text)
    turns: List[Turn] = []
    first = labels[0].lstart if labels else content_end
    if first > 0:
        turns.append(Turn(None, False, _clean_turn(text[:first]), 0, first))
    for n, lab in enumerate(labels):
        end = labels[n + 1].lstart if n + 1 < len(labels) else content_end
        end = max(end, lab.bstart)
        turns.append(Turn(normalize_label(lab.raw), is_rubio_label(lab.raw),
                          _clean_turn(text[lab.bstart:end]), lab.bstart, end))
    return turns


def rubio_text(text: str, stop_at_footer: bool = True, log: bool = False,
               context: str = '') -> Tuple[str, dict]:
    """Concatenate Secretary Rubio's turns (joined with blank lines).

    Returns (joined_text, stats) with stats keys:
      n_rubio_turns, n_other_turns   -- labelled turns only (preamble not counted)
      untranscribed_segments         -- standalone "(In Spanish.)"-type placeholders
                                        inside Rubio turns (X-10)
      unresolved_labels              -- non-Rubio labels mentioning RUB/SECR
      labels                         -- distinct canonical labels in order of appearance
    An empty string with n_rubio_turns == 0 means the text has no Rubio label; the
    caller decides the label-less fallback (written statements, video messages).
    """
    turns = segment(text, stop_at_footer=stop_at_footer)
    norm_text = text or ''
    parts, n_r, n_o, untr = [], 0, 0, 0
    unresolved, seen = [], []
    for t in turns:
        if t.label is None:
            continue
        if t.label not in seen:
            seen.append(t.label)
        if t.is_rubio:
            n_r += 1
            untr += _count_untranscribed(_normalize_chars(norm_text[t.start:t.end]))
            if t.text:
                parts.append(t.text)
        else:
            n_o += 1
            if _is_suspicious(t.label) and t.label not in unresolved:
                unresolved.append(t.label)
    if log and unresolved:
        print(f'[rubio_turns] {context + ": " if context else ""}unresolved labels '
              f'mentioning RUB/SECR: {unresolved}', file=sys.stderr)
    stats = {
        'n_rubio_turns': n_r,
        'n_other_turns': n_o,
        'untranscribed_segments': untr,
        'unresolved_labels': unresolved,
        'labels': seen,
    }
    return '\n\n'.join(parts), stats


if __name__ == '__main__':
    # Ad-hoc check: python rubio_turns.py FILE  -> prints Rubio text and stats.
    if len(sys.argv) != 2:
        print('usage: rubio_turns.py FILE', file=sys.stderr)
        sys.exit(2)
    with open(sys.argv[1], encoding='utf-8') as fh:
        body, st = rubio_text(fh.read(), log=True, context=sys.argv[1])
    print(body)
    print(st, file=sys.stderr)
