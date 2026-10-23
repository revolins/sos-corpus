#!/usr/bin/env python3
"""Title column: recover the state.gov page headline and flag bad titles.

The corpus Title is the verbatim state.gov headline, secretary's name included
(decided 2026-10-22). About a third of v2026.10.19's titles were something else,
all written by the one-off v4.4-v4.11 finalize scripts and carried forward:

  speech_sentence  v4.4 "Fix AAA" took the first line of speech_text as the title
                   when the page title was not found: "That's right.", "Glad to
                   be with you.", "On behalf of the United States of America, I..."
  slug_derived     v4.5 "Fix BBB" title-cased the URL slug for every 2017+ page
                   ("Kazakhstans Republic Day", "Cbc", "Iii"), and its greedy
                   secretary-prefix strip ate words after the name ("And Greet
                   With Mission Kuwait", "To The Press", "Of Defense Lloyd...").

The real headline survives in Raw Text (the scraped page's get_text), so it is
recovered offline: page_title() reads it, bad_title_reasons() says why a Title is
wrong, and repair_title() combines the two into the patch / verifier rule.

Stdlib only: the verifier imports this module.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse

# Same patterns as corpus_finalize_v4_30.SITE_SUFFIX_RE / DATE_PREFIX_RE.
SITE_SUFFIX_RE = re.compile(
    r'\s*[-|–]\s*(?:United\s+States\s+|U\.S\.\s+)?Department\s+of\s+State\s*$',
    re.IGNORECASE,
)
DATE_PREFIX_RE = re.compile(r'^\d{1,2}/\d{1,2}/\d{2,4}\s*(?:--\s*)?:?\s*', re.S)

# Pages whose Raw Text lost its line breaks: the headline runs straight into the
# site chrome.
_FLAT_CHROME_RE = re.compile(
    r'(?:\s*[-|–]\s*(?:United States |U\.S\. )?Department of State)?'
    r'(?:\s+U\.S\. Department of State)?'
    r'\s+(?:Skip to (?:[Mm]ain )?[Cc]ontent|Other State Department|An official website|You are in:)'
)
_HIDE_RE = re.compile(r'\nhide\n')
# A first line that is not a headline: chrome, or a speaker label (Raw Text that
# starts at the transcript has no header to recover).
_NOT_HEADLINE_RE = re.compile(
    r'^(?:U\.S\. Department of State|United States Department of State|Skip to (?:main )?content'
    r'|An official website|Home|Page not found|Archive)\b'
    r"|^[A-Z][A-Z .'’-]{2,}:"
    r'|^\([^)]*\)\.?$',  # a stage direction: "(Via interpreter.)"
    re.IGNORECASE,
)
# A bare content-type label ("Remarks") where the headline should be: some pages'
# <title> or hide block carries only the type.
_BARE_TYPE_RE = re.compile(
    r'^(?:Remarks|Press Statement|Statement|Interview|Media Note|Readout|Op-Ed|Testimony|Speech)$',
    re.IGNORECASE)

# The old hosts' <title> is a terse catalogue string ("8/10/98 Albright on CBS
# Evening News") while the existing Title there is usually the fuller in-page
# heading, so a mere mismatch is not repaired on these hosts -- only a title
# flagged by bad_title_reasons() is.
LEGACY_HOSTS = frozenset({'1997-2001.state.gov', '2001-2009.state.gov', '2009-2017.state.gov'})

# v4.5's slug rule, reproduced exactly so its output can be recognised.
_SLUG_NAME_PREFIX_RE = re.compile(r'^secretary(?:-of-state)?-(?:[a-z]+-){2,4}', re.IGNORECASE)
_SLUG_DISAMBIG_RE = re.compile(r'-\d+$')

_UTTERANCE_OPEN_RE = re.compile(
    r"^(?:thank(?:s| you)|good (?:morning|afternoon|evening)|well[,. ]|yes[,. ]|yeah|no[,. ]|okay|ok[,. ]"
    r"|so[, ]|let me|i |i'm|i’m|i've|i’ve|we |we're|we’re|it's|it’s|it is|and |but |there |this is"
    r"|that's|that’s|look[,. ]|hello|hi[,. ]|ladies|everybody|everyone|please|sure|great|glad|nice"
    r"|pleasure|you're|you’re|on behalf of|to all those|all right|alright)",
    re.IGNORECASE,
)
_FIRST_PERSON_RE = re.compile(r"\b(?:I|I'm|I’m|I've|I’ve|I'll|I’ll|me|my|we|we're|we’re|our|us|you|your)\b")
_SENTENCE_END_RE = re.compile(r'[.?!]["”’)]?$')
_ABBREV_END_RE = re.compile(r'\b(?:Jr|Sr|St|U\.S|D\.C|Inc|Co|Ltd|No|Mr|Mrs|Ms|Dr)\.$')
_FRAGMENT_START_RE = re.compile(r'^(?:And|Of|To|With|At|On|For|Before|In|After|By) ')
# A heading cut at a line break: "Statement by", "Op-Ed on the Crisis in Kosovo in".
_TRUNCATED_END_RE = re.compile(
    r'(?:\b(?:by|of|the|and|for|to|in|on|at|with|a|an|from|before|after|between)|[,:;–-])$',
    re.IGNORECASE)
# First transcript speaker label: Raw Text before it is the page header (title,
# byline, dateline), after it is the speech.
_LABEL_LINE_RE = re.compile(r"\n(?:[A-Z][A-Z .'’-]{1,60}|QUESTION|Q):", re.MULTILINE)


# 1997-2001 pages split the in-page heading over lines, and the old title pass
# kept only the line after the byline: "Secretary of State Madeleine K. Albright
# and" / "Italian Foreign Minister Lamberto Dini" became "Italian Foreign
# Minister Lamberto Dini", and "...Albright" / "And His Majesty King Hussein of
# Jordan" became "And His Majesty King Hussein of Jordan".
_SECRETARY_BYLINE_RE = re.compile(r'^(?:Acting )?Secretary(?: of State)? [A-Z][\w.\' -]+?(?:,? and|,)?$')


def split_heading(title: str, raw_text: str) -> str:
    """The joined heading when Title is the second half of a split one, else ''."""
    t = (title or '').strip()
    raw = raw_text or ''
    if not t:
        return ''
    i = raw.find('\n' + t)
    if i < 0:
        return ''
    prev = raw[:i].rsplit('\n', 1)[-1].strip()
    if not _SECRETARY_BYLINE_RE.match(prev):
        return ''
    if not (re.search(r'(?:,? and|,)$', prev) or t.startswith('And ')):
        return ''
    if t.startswith('And '):
        t = 'and ' + t[4:]
    joined = f'{prev} {t}'
    # The heading may run on: "..., Secretary of Defense William Cohen," / "and ..."
    following = raw[i + 1 + len(title.strip()):].split('\n')[1:4]
    for line in following:
        if not re.search(r'(?:,? and|,)$', joined):
            break
        line = line.strip()
        if not line or len(line) > 120:
            break
        joined = f'{joined} {line}'
    return joined.rstrip(',')


# A standalone dateline ("July 4, 2023") closes the header of a written item,
# which has no speaker label.
_DATE_LINE_RE = re.compile(
    r'\n(?:January|February|March|April|May|June|July|August|September|October|November|December'
    r'|Jan\.|Feb\.|Mar\.|Apr\.|Aug\.|Sept?\.|Oct\.|Nov\.|Dec\.)\s+\d{1,2},\s+\d{4}\n')


# How far past the header to look for speech text: archive pages carry up to
# ~15k characters of navigation before the item.
_BODY_WINDOW = 20000


def raw_header(raw_text: str) -> str:
    """Raw Text before the speech starts: up to the first speaker label or, for
    a written item, its dateline."""
    raw = raw_text or ''
    if re.match(r"(?:[A-Z][A-Z .'’-]{1,60}|QUESTION|Q):", raw):
        return ''  # Raw Text that starts at the transcript has no header
    # 2017+ pages put the site navigation first; the item starts at "hide".
    m = _HIDE_RE.search(raw)
    start = m.start() if m else 0
    window = raw[start:start + _BODY_WINDOW]
    ends = [x.start() for x in (_LABEL_LINE_RE.search(window), _DATE_LINE_RE.search(window)) if x]
    return raw[:start + min(ends)] if ends else raw[:start + 1500]


def fix_text(text: str) -> str:
    """The corpus's own Title normalisation (corpus_finalize_v4_30 runs
    ftfy.fix_text over every Title): repairs mojibake ("TÃ¼rkiye") and
    straightens quotes, so a recovered headline compares equal to one the weekly
    pipeline wrote."""
    if not text:
        return text
    text = html.unescape(text)
    try:
        import ftfy  # optional; the verifier compares with norm() and never needs it
        text = ftfy.fix_text(text)
        # a lone "Â" (mangled NBSP) that ftfy leaves when nothing else is mis-encoded
        return re.sub(r'Â(?=\s|$)', '', text).strip()
    except ImportError:
        text = re.sub(r'Ã([\x80-\xbf])',
                      lambda m: bytes([0xC3, ord(m.group(1))]).decode('utf-8', errors='replace'),
                      text)
        text = re.sub(r'Â(?=\s|$)', '', text).strip()
        return text.replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"')


def norm(s: str) -> str:
    """Comparison key: lowercase alphanumerics, apostrophes dropped."""
    s = (s or '').lower().replace('’', "'").replace("'", '')
    return re.sub(r'[^0-9a-z]+', ' ', s).strip()


_SITE_PREFIX_RE = re.compile(r'^U\.S\. Department of State:\s*')


def clean_title(title: str) -> str:
    title = _SITE_PREFIX_RE.sub('', SITE_SUFFIX_RE.sub('', title or '').strip())
    title = DATE_PREFIX_RE.sub('', title).strip()
    title = re.sub(r'[\xa0\u202f\u2009\u200b]', ' ', title)
    return re.sub(r'\s{2,}', ' ', title).strip()


def _is_body_sentence(text: str) -> bool:
    """A first line that is the item's opening sentence, not a title (pages whose
    Raw Text starts at the body): long, and ending like a sentence."""
    words = text.split()
    return len(words) > 25 or (len(words) > 12 and text.rstrip().endswith('.'))


def page_title(raw_text: str) -> tuple[str, str]:
    """(headline, source) recovered from a row's Raw Text, or ('', '').

    Sources, first that yields wins:
      hide_block  2021+ pages: the line after "hide" is the bare headline
      line1       the page <title> that get_text puts first, minus the site suffix
      flat_prefix Raw Text without line breaks: the text before the site chrome
    """
    raw = raw_text or ''
    m = _HIDE_RE.search(raw)
    if m:
        line = raw[m.end():].lstrip('\n').split('\n', 1)[0].strip()
        if (4 <= len(line) <= 300 and not _NOT_HEADLINE_RE.match(line)
                and not _BARE_TYPE_RE.match(line)):
            line = clean_title(line)
            # The on-page headline is normally the fuller one; when the <title>
            # extends it ("Secretary Antony J. Blinken Remarks to the Press" vs
            # "Remarks to the Press"), the <title> is.
            first = clean_title(raw.lstrip().split('\n', 1)[0])
            if len(first) <= 300 and len(first) > len(line) and norm(first).endswith(norm(line)):
                return first, 'line1'
            return line, 'hide_block'
    first = raw.lstrip().split('\n', 1)[0].strip()
    if len(first) <= 300:
        cand, source = clean_title(first), 'line1'
        m = _FLAT_CHROME_RE.search(cand)
        if m:  # a short flattened page: the chrome is on line 1
            cand, source = clean_title(cand[:m.start()]), 'flat_prefix'
        if (4 <= len(cand) <= 300 and not _NOT_HEADLINE_RE.match(first)
                and not _BARE_TYPE_RE.match(cand) and not _is_body_sentence(cand)):
            return cand, source
    m = _FLAT_CHROME_RE.search(raw[:600])
    if m:
        cand = clean_title(raw[:m.start()])
        if (4 <= len(cand) <= 300 and not _NOT_HEADLINE_RE.match(cand)
                and not _BARE_TYPE_RE.match(cand)):
            return cand, 'flat_prefix'
    return '', ''


def title_from_slug(url: str) -> str:
    """corpus_finalize_v4_5._title_from_slug, verbatim in effect."""
    slug = (url or '').rstrip('/').rsplit('/', 1)[-1]
    slug = _SLUG_DISAMBIG_RE.sub('', slug)
    cleaned = _SLUG_NAME_PREFIX_RE.sub('', slug)
    if not cleaned or len(cleaned) < 5:
        cleaned = slug
    candidate = cleaned.replace('-', ' ').title()
    return candidate if 10 < len(candidate) < 200 else ''


def is_utterance_shaped(title: str) -> bool:
    t = (title or '').strip()
    if _UTTERANCE_OPEN_RE.match(t) or len(t.split()) > 25:
        return True
    ends = bool(_SENTENCE_END_RE.search(t)) and not _ABBREV_END_RE.search(t)
    return ends and bool(_FIRST_PERSON_RE.search(t))


def bad_title_reasons(title: str, url: str, speech_texts: tuple[str, ...] = (),
                      headline: str = '', raw_text: str = '') -> list[str]:
    """Why Title is not a page headline; [] when it looks fine.

    speech_texts: the row's SOS / Natural Text (only the head is searched).
    headline: page_title() of the row, when known.
    raw_text: the row's Raw Text. A title found in the speech but nowhere in the
    page header was lifted from the speech, whatever its shape ("Who wants
    first?", "In that order.").
    """
    t = (title or '').strip()
    if not t:
        return ['empty']
    if headline and t == headline:
        return []
    reasons = []
    nt, nh = norm(t), norm(headline)
    slug = title_from_slug(url)
    if slug and t == slug:
        reasons.append('slug_derived')
    if nt != nh and len(nt) >= 3:
        header = raw_header(raw_text) if raw_text else ''
        # The old title pass sometimes MOVED the first sentence out of the text,
        # so also look for it in the Raw Text body after the header.
        body = norm(raw_text[len(header):len(header) + _BODY_WINDOW]) if raw_text else ''
        in_speech = any(nt in norm(s[:3000]) for s in speech_texts if s) or nt in body
        if in_speech:
            if (raw_text and nt not in norm(header)) or (len(nt) >= 6 and is_utterance_shaped(t)):
                reasons.append('speech_sentence')
    if _FRAGMENT_START_RE.match(t) and nh and nh.endswith(nt) and nh != nt:
        reasons.append('fragment_start')
    if raw_text and 'speech_sentence' not in reasons and len(nt) >= 6:
        i = raw_text.find('\n' + t[:40])
        if i > 0 and _label(raw_text[:i].rsplit('\n', 1)[-1])[0]:
            reasons.append('speech_sentence')  # the line right after a speaker label
    if re.search(r"\b(?:SECRETARY|QUESTION|MODERATOR)(?: [A-Z][A-Z'’.-]+)*:", t) and 'speech_sentence' not in reasons:
        reasons.append('speech_sentence')  # a transcript line, label and all
    if _SITE_PREFIX_RE.match(t) or SITE_SUFFIX_RE.search(t):
        reasons.append('site_chrome')
    if re.search(r'Ã[\x80-\xbf]|â€|Â(?=\s|$)|\ufffd', t):
        reasons.append('mojibake')
    if _TRUNCATED_END_RE.search(t) and nh != nt:
        reasons.append('truncated')
    return reasons


def continued_heading(title: str, raw_text: str) -> str:
    """A truncated Title joined with the heading lines that follow it in Raw
    Text ("Op-Ed on the Crisis in Kosovo in" / "The Washington Post"), or ''."""
    t = (title or '').strip()
    raw = raw_text or ''
    i = raw.find('\n' + t + '\n')
    if not t or i < 0:
        return ''
    joined = t
    for line in raw[i + len(t) + 2:].split('\n')[:3]:
        line = line.strip()
        if not line or len(line) > 120 or _DATE_LINE_RE.match('\n' + line + '\n') or _label(line)[0]:
            break
        joined = f'{joined} {line}'
        if not _TRUNCATED_END_RE.search(joined):
            return joined
    return ''


def repair_title(row: dict, fix=fix_text) -> tuple[str, list[str], str]:
    """(new_title, reasons, source) for a corpus row; new_title == '' means keep.

    Rule: replace with the recovered headline when the Title is flagged, or when it
    differs from the headline at all on a modern host (www / 2017-2021 /
    2021-2025), where the headline is exactly the page's own.  fix applies the
    corpus's Title normalisation to the recovered headline.
    """
    title = row.get('Title') or ''
    url = row.get('URL') or ''
    joined = split_heading(title, row.get('Raw Text') or '')
    if joined:
        return joined, ['split_heading'], 'heading_join'
    headline, source = page_title(row.get('Raw Text') or '')
    if headline and fix:
        headline = fix(headline).strip()
    reasons = bad_title_reasons(
        title, url, (row.get('SOS Text') or '', row.get('Natural Text') or ''), headline,
        row.get('Raw Text') or '')
    if reasons == ['truncated']:
        joined = continued_heading(title, row.get('Raw Text') or '')
        if joined:
            return fix(joined).strip() if fix else joined, reasons, 'heading_join'
    if not headline or headline == title:
        return '', reasons, source
    host = urlparse(url).netloc.lower()
    if reasons:
        return headline, reasons, source
    if host not in LEGACY_HOSTS:
        return headline, ['headline_mismatch'], source
    return '', reasons, source


def title_problems(row: dict) -> list[str]:
    """Verifier rule (Check AJ): why a row's Title is not its page headline.

    bad_title_reasons() plus, on a modern host with a recoverable headline, a
    plain mismatch. Compared with norm(), so quote style, NBSP and case -- which
    the corpus normalises -- never count.
    """
    title = row.get('Title') or ''
    url = row.get('URL') or ''
    raw = row.get('Raw Text') or ''
    headline, _ = page_title(raw)
    headline = fix_text(headline) if headline else ''
    problems = bad_title_reasons(
        title, url, (row.get('SOS Text') or '', row.get('Natural Text') or ''), headline, raw)
    if not problems and split_heading(title, raw):
        problems = ['split_heading']
    if (not problems and headline and urlparse(url).netloc.lower() not in LEGACY_HOSTS
            and norm(title) != norm(headline)):
        problems = ['headline_mismatch']
    return problems


# ---------------------------------------------------------------------------
# Text the old title pass removed
# ---------------------------------------------------------------------------
# corpus_finalize_v4_30.strip_leading_title deletes leading lines of a text that
# repeat its Title. Once v4.4 had set the Title to the speech's first sentence,
# that pass deleted the sentence: written statements lost their opening sentence
# (often leaving a <sup> ordinal fragment, "nd\nanniversary ..."), interviews lost
# the Secretary's first answer from SOS Text. The deleted lines are the old
# title's lines and survive in the less-processed copy: Natural Text is restored
# from Raw Text, SOS Text from the restored Natural Text, keeping only the
# Secretary's turns (SOS Text is Secretary-only, turns joined by a blank line).

# Statuses restore_head() reports when it declines (the row is left unchanged).
RESTORE_FAILURES = frozenset({'no_head', 'too_long', 'no_title', 'not_secretary', 'boilerplate'})

_LABEL_RE = re.compile(r"^((?:[A-Z][A-Z .'’()\-]{1,60})|QUESTION|Q)\s*:\s*(.*)$"
                       r"|^((?:[A-Z][A-Z .'’()\-]{1,60}))\s*$")
_ORDINAL_SEAM_RE = re.compile(r'(\d)\n(st|nd|rd|th)\b\n?')
_ORDINAL_PUNCT_RE = re.compile(r'(\d)\n(st|nd|rd|th)\n(?=[.,;:)])')


def _label(line: str) -> tuple[str | None, str]:
    """(speaker label, rest of line) for "LABEL: text" / "LABEL" lines, else (None, line)."""
    m = _LABEL_RE.match(line.strip())
    if not m:
        return None, line
    lab = (m.group(1) or m.group(3) or '').strip()
    if m.group(3) and len(lab.split()) < 2 and lab not in ('QUESTION', 'Q', 'MODERATOR'):
        return None, line  # a lone capitalised word on its own line, not a label
    return lab, (m.group(2) or '')


def _is_secretary(label: str, surname: str) -> bool:
    return (bool(re.search(rf'\b{re.escape(surname.upper())}\b', label))
            or label in ('SECRETARY', 'THE SECRETARY'))


def _speaker_is_secretary(src: str, pos: int, surname: str) -> bool | None:
    """Who speaks the line starting at pos: its own label, else the last label
    before it; None when the text has no labels (a written item)."""
    end = src.find('\n', pos)
    own, _ = _label(src[pos:end if end >= 0 else len(src)])
    if own:
        return _is_secretary(own, surname)
    last = None
    for line in src[max(0, pos - 3000):pos].split('\n'):
        lab, _ = _label(line)
        if lab:
            last = lab
    return None if last is None else _is_secretary(last, surname)


def _join(prefix: str, text: str) -> str:
    """prefix + text, rejoining a <sup> ordinal split at the seam ("35\nth\nanniversary")."""
    out = prefix + text
    n = len(prefix) + 12
    head = _ORDINAL_PUNCT_RE.sub(r'\1\2', out[:n])
    return _ORDINAL_SEAM_RE.sub(lambda m: m.group(1) + m.group(2) + ' ', head) + out[n:]


def title_in_body(title: str, raw_text: str) -> bool:
    """The (old) title is speech content: in the Raw Text body, not its header."""
    nt = norm(title)
    header = raw_header(raw_text)
    body = raw_text[len(header):len(header) + _BODY_WINDOW]
    return len(nt) >= 6 and nt not in norm(header) and nt in norm(body)


def restore_head(text: str, src: str, title: str, surname: str,
                 secretary_only: bool) -> tuple[str, str, str]:
    """(new_text, status, restored_prefix) for one text field.

    status: 'restored'; 'present' (nothing missing); 'not_found' (title not in
    src); or a RESTORE_FAILURES reason, which leaves the text unchanged.
    """
    t = (title or '').strip()
    nt = norm(t)
    if len(nt) < 6 or not text or nt in norm(text[:len(t) + 400]):
        return text, 'present', ''
    start = -1
    for k in (40, 25):
        i = src.find(t[:k])
        if i >= 0:
            start = src.rfind('\n', 0, i) + 1
            break
    if start < 0:
        return text, 'not_found', ''
    body_text = re.sub(r'^[\s:]+', '', text)
    q = -1
    for head in (body_text[:40], body_text.split('\n', 1)[0][:40], body_text[:20]):
        if len(head.strip()) >= 3:
            q = src.find(head, start + len(t[:25]))
            if q >= 0:
                break
    if q <= start:
        return text, 'no_head', ''
    seg = src[start:q]
    if not secretary_only:
        if len(norm(seg)) > 1.6 * len(nt) + 40:
            return text, 'too_long', ''
        return _join(seg, text.lstrip()), 'restored', seg
    if _speaker_is_secretary(src, start, surname) is False:
        return text, 'not_secretary', ''
    keep, current, labelled = [], True, False
    for line in seg.split('\n'):
        lab, rest = _label(line)
        if lab:
            current, labelled, line = _is_secretary(lab, surname), True, rest
        if current and line.strip():
            keep.append(line.strip())
    kept = '\n'.join(keep)
    if nt not in norm(kept):
        return text, 'no_title', ''
    if len(norm(kept)) > 1.6 * len(nt) + 40:
        return text, 'too_long', ''
    # Separate turns are joined by a blank line; a turn that continues into the
    # existing text keeps the source's own line break.
    prefix = kept + '\n\n' if labelled else seg
    if 'available here' in prefix:
        return text, 'boilerplate', ''
    return _join(prefix, body_text), 'restored', prefix


def restore_row(row: dict, old_title: str, fix=fix_text) -> tuple[dict, dict]:
    """({field: restored text}, {field: status}) for SOS / Natural Text.

    old_title: the Title before repair (it is what strip_leading_title matched).
    fix: the corpus text normalisation applied to Raw Text before matching.
    """
    raw = row.get('Raw Text') or ''
    if not title_in_body(old_title, raw):
        return {}, {}
    # Only a title that was speech can have been stripped from the speech: a
    # headline copied from another item can also sit in the page body (in a
    # related-links list) and must not be spliced into the text.
    headline = page_title(raw)[0]
    texts = (row.get('SOS Text') or '', row.get('Natural Text') or '')
    if 'speech_sentence' not in bad_title_reasons(old_title, row.get('URL') or '', texts,
                                                  fix(headline) if headline else '', raw):
        return {}, {}
    who = row.get('Secretary', '')
    nat0, sos0 = row.get('Natural Text') or '', row.get('SOS Text') or ''
    nat, nat_status, nat_prefix = restore_head(nat0, fix(raw), old_title, who, False)
    if sos0 == nat0:  # a written item: SOS Text is the same text
        sos, sos_status = nat, nat_status
    else:
        sos, sos_status, _ = restore_head(sos0, nat, old_title, who, True)
        if (sos_status in RESTORE_FAILURES and nat_status == 'restored'
                and not any(_label(line)[0] for line in nat0.split('\n')[:200])):
            # label-free item whose SOS differs slightly from Natural: same prefix
            sos, sos_status = _join(nat_prefix, re.sub(r'^[\s:]+', '', sos0)), 'restored'
    out = {f: v for f, v, v0 in (('Natural Text', nat, nat0), ('SOS Text', sos, sos0)) if v != v0}
    return out, {'Natural Text': nat_status, 'SOS Text': sos_status}
