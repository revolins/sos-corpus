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
    r'\s*[-|–]\s*(?:United States |U\.S\. )?Department of State\b'
    r'|\s+(?:U\.S\. Department of State\b|Skip to (?:Main )?Content\b|Other State Department\b'
    r'|An official website of the United States)'
)
_HIDE_RE = re.compile(r'\nhide\n')
# A first line that is not a headline: chrome, or a speaker label (Raw Text that
# starts at the transcript has no header to recover).
_NOT_HEADLINE_RE = re.compile(
    r'^(?:U\.S\. Department of State|United States Department of State|Skip to (?:main )?content'
    r'|An official website|Home|Page not found|Archive)\b'
    r"|^[A-Z][A-Z .'’-]{2,}:",
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


def raw_header(raw_text: str) -> str:
    """Raw Text before the speech starts: up to the first speaker label or, for
    a written item, its dateline."""
    raw = raw_text or ''
    # 2017+ pages put the site navigation first; the item starts at "hide".
    m = _HIDE_RE.search(raw)
    start = m.start() if m else 0
    window = raw[start:start + 6000]
    ends = [x.start() for x in (_LABEL_LINE_RE.search(window), _DATE_LINE_RE.search(window)) if x]
    return raw[:start + min(ends)] if ends else raw[:start + 1500]


def fix_text(text: str) -> str:
    """The corpus's own Title normalisation (corpus_finalize_v4_30 runs
    ftfy.fix_text over every Title): repairs mojibake ("TÃ¼rkiye") and
    straightens quotes, so a recovered headline compares equal to one the weekly
    pipeline wrote."""
    if not text:
        return text
    try:
        import ftfy  # optional; the verifier compares with norm() and never needs it
        return ftfy.fix_text(text)
    except ImportError:
        text = re.sub(r'Ã([\x80-\xbf])',
                      lambda m: bytes([0xC3, ord(m.group(1))]).decode('utf-8', errors='replace'),
                      text)
        return text.replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"')


def norm(s: str) -> str:
    """Comparison key: lowercase alphanumerics, apostrophes dropped."""
    s = (s or '').lower().replace('’', "'").replace("'", '')
    return re.sub(r'[^0-9a-z]+', ' ', s).strip()


def clean_title(title: str) -> str:
    title = SITE_SUFFIX_RE.sub('', title or '').strip()
    title = DATE_PREFIX_RE.sub('', title).strip()
    title = re.sub(r'[\xa0\u202f\u2009\u200b]', ' ', title)
    return re.sub(r'\s{2,}', ' ', title).strip()


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
                and not _BARE_TYPE_RE.match(cand)):
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
        body = norm(raw_text[len(header):len(header) + 3000]) if raw_text else ''
        in_speech = any(nt in norm(s[:3000]) for s in speech_texts if s) or nt in body
        if in_speech:
            if (raw_text and nt not in norm(header)) or (len(nt) >= 6 and is_utterance_shaped(t)):
                reasons.append('speech_sentence')
    if _FRAGMENT_START_RE.match(t) and nh and nh.endswith(nt) and nh != nt:
        reasons.append('fragment_start')
    return reasons


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
