#!/usr/bin/env python3
"""Research-grade QA finalization: v4.29.csv -> v4.30.csv.

Changes from v4.29:

1. LOCATION_PATCHES fully replaced (~210 rows):
   Deep audit of v4.29 Location column found values that passed Check L
   (have a comma) but are not proper City, Country format. Three classes:

   a) Venue names concatenated to city without comma — e.g. "Treaty Room
      Washington, D.C.", "Grand Hyatt Hotel Seoul, South Korea". All stripped
      to bare City, Country (45 D.C. venue rows + ~60 non-D.C. venue rows).

   b) Three-part entries (venue, city, country with 2 commas) — e.g.
      "Diaoyutai, Beijing, China". Reduced to last two parts (15 rows).

   c) Country naming inconsistencies standardized:
      - South Korea / Korea → Republic of Korea (~69 rows)
      - People's Republic of China / Peoples Republic of China → China (8 rows)
      - The Netherlands / the Netherlands → Netherlands (9 rows)
      - Cote d'Ivoire → Côte d'Ivoire (4 rows)
      - England → United Kingdom (2 rows)
      - Abbreviations: MD → Maryland, UAE → United Arab Emirates,
        JAPAN → Japan (3 rows)

   Additionally: 3 factually wrong locations fixed, 3 residual En Route
   values stripped, 4 misspellings corrected (Port-au-Prince, Sharm
   el-Sheikh), 6 wrong Andrews AFB name forms corrected, 4 misc fixes.

   NOTE: v4.29 LOCATION_PATCHES NOT carried forward — v4.29 output already
   has those corrections applied; re-applying would be no-ops at best.

"""

from __future__ import annotations

import argparse
import csv
import difflib
import html
import re
import requests
from collections import Counter, defaultdict
import pickle
from datetime import date, datetime
from pathlib import Path

from charset_normalizer import from_bytes as _cn_from_bytes
import ftfy

csv.field_size_limit(10 ** 8)

BASE_DIR = Path(__file__).resolve().parent

OUTPUT_COLUMNS = [
    'Row Id', 'Secretary', 'Date', 'Title', 'Speech Type',
    'Location', 'URL', 'SOS Text', 'Natural Text', 'Raw Text',
]

# Map v4.13 input column names → v4.14 output column names
COL_RENAME = {
    'row_id': 'Row Id',
    'secretary': 'Secretary',
    'speech_type': 'Speech Type',
    'url': 'URL',
    'speech_text': 'SOS Text',
    'speaker_text': 'Natural Text',
    'raw_text': 'Raw Text',
}

SECRETARY_CANON = {
    'christopher': 'Christopher',
    'albright': 'Albright',
    'powell': 'Powell',
    'rice': 'Rice',
    'clinton': 'Clinton',
    'kerry': 'Kerry',
    'tillerson': 'Tillerson',
    'pompeo': 'Pompeo',
    'blinken': 'Blinken',
    'rubio': 'Rubio',
}

LASTNAME = {
    'Christopher': 'CHRISTOPHER',
    'Albright': 'ALBRIGHT',
    'Powell': 'POWELL',
    'Rice': 'RICE',
    'Clinton': 'CLINTON',
    'Kerry': 'KERRY',
    'Tillerson': 'TILLERSON',
    'Pompeo': 'POMPEO',
    'Blinken': 'BLINKEN',
    'Rubio': 'RUBIO',
}

SPEECH_TYPE_TITLECASE = {
    'remarks': 'Remarks',
    'speech': 'Speech',
    'interview': 'Interview',
    'testimony': 'Testimony',
    'press statement': 'Press Statement',
    'statement': 'Statement',
    'press availability': 'Press Availability',
    'joint press availability': 'Joint Press Availability',
    'press briefing': 'Press Briefing',
    'op-ed': 'Op-Ed',
    'town hall': 'Town Hall',
    'remarks to the press': 'Remarks to the Press',
}

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}

MONTH_DATE_RE = re.compile(
    r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+((?:19|20)\d{2})\b',
    re.IGNORECASE,
)
URL_YM_RE = re.compile(r'/((?:19|20)\d{2})/(\d{2})/')

SITE_SUFFIX_RE = re.compile(
    r'\s*[-|–]\s*(?:United\s+States\s+|U\.S\.\s+)?Department\s+of\s+State\s*$',
    re.IGNORECASE,
)

DATE_PREFIX_RE = re.compile(r'^\d{1,2}/\d{1,2}/\d{2,4}\s*(?:--\s*)?:?\s*', re.S)

SPEAKER_LABEL_RE = re.compile(
    r'(?i)(?<![A-Za-z])(?:QUESTION|Q|SECRETARY-GENERAL(?:\s+[A-Z .\'\-]+)?|SECRETARY(?:\s+[A-Z .\'\-]+)?|MODERATOR|HOST|REPORTER|NARRATOR|PARTICIPANT|'
    r'MR\.?\s+[A-Z][A-Z\'\-]+|MS\.?\s+[A-Z][A-Z\'\-]+|MRS\.?\s+[A-Z][A-Z\'\-]+|DR\.?\s+[A-Z][A-Z\'\-]+|'
    r'CHAIRMAN(?:\s+[A-Z][A-Z\'\-]+)?|SENATOR(?:\s+[A-Z][A-Z\'\-]+)?|AMBASSADOR(?:\s+[A-Z .\'\-]+)?|ADMINISTRATOR(?:\s+[A-Z .\'\-]+)?|'
    r'(?:[A-Z]+\s+)*FOREIGN\s+MINISTER(?:-[A-Z]+)?(?:\s+[A-Z .\'\-]+)?|MINISTER(?:-[A-Z]+)?(?:\s+[A-Z .\'\-]+)?|'
    r'PRIME\s+MINISTER(?:-[A-Z]+)?(?:\s+[A-Z .\'\-]+)?|COMMISSIONER(?:\s+[A-Z .\'\-]+)?)'
    r'(?:\s*\([^)]{1,30}\))?\s*\|?\s*:\s*'
)

# Broader multiline pattern matching any all-caps speaker label at line start.
_BROAD_LABEL_RE = re.compile(r'(?m)^\s*([A-Z][A-Z .\'\-]{2,70}):\s*')

CONTENT_LABEL_RE = re.compile(
    r'(?i)(?<![A-Za-z])(?:QUESTION|Q|SECRETARY-GENERAL(?:\s+[A-Z .\'\-]+)?|SECRETARY(?:\s+[A-Z .\'\-]+)?|MODERATOR|HOST|REPORTER|NARRATOR|PARTICIPANT|'
    r'MR\.?\s+[A-Z][A-Z\'\-]+|MS\.?\s+[A-Z][A-Z\'\-]+|MRS\.?\s+[A-Z][A-Z\'\-]+|DR\.?\s+[A-Z][A-Z\'\-]+|'
    r'CHAIRMAN(?:\s+[A-Z][A-Z\'\-]+)?|SENATOR(?:\s+[A-Z][A-Z\'\-]+)?|AMBASSADOR(?:\s+[A-Z .\'\-]+)?|ADMINISTRATOR(?:\s+[A-Z .\'\-]+)?|'
    r'(?:[A-Z]+\s+)*FOREIGN\s+MINISTER(?:-[A-Z]+)?(?:\s+[A-Z .\'\-]+)?|MINISTER(?:-[A-Z]+)?(?:\s+[A-Z .\'\-]+)?|'
    r'PRIME\s+MINISTER(?:-[A-Z]+)?(?:\s+[A-Z .\'\-]+)?|COMMISSIONER(?:\s+[A-Z .\'\-]+)?)'
    r'(?:\s*\([^)]{1,30}\))?\s*\|?\s*:\s*'
)

ARCHIVE_RELEASE_RE = re.compile(r'As\s+released\s+by\s+the\s+Office\s+of\s+the\s+Spokesman\b', re.I)
DEPT_STATE_RE = re.compile(r'(?:U\.S\.\s+Department\s+of\s+State|Department\s+of\s+State)', re.I)

MODERN_ARCHIVE_HEADER_RE = re.compile(
    r'(?is)^\s*(?:2017-2021\s+ARCHIVED\s+CONTENT|.*?You\s+are\s+viewing\s+ARCHIVED\s+CONTENT).*?'
    r'(?:Share\s+this\s+page\s+on:\s*)'
)

LEADING_STATE_HEADER_RE = re.compile(
    r'(?is)^\s*Secretary\s+of\s+State\s+(?:[A-Z][\w.\-]+\s*){0,5}'
    r'(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+(?:19|20)\d{2}\s+'
    r'Share\s+this\s+page\s+on:\s*.*?'
)

FOOTER_RES = [
    re.compile(r'(?is)\s+Back\s+to\s+Top\b.*$'),
    re.compile(r'(?is)\nTags\n.*$'),
    re.compile(r'(?is)\s+(?:\[\d+\]\s+)?(?:Foreign\s+)?Tags\s+(?:Secretary|Iran|Israel|North\s+Korea|Office\s+of|Anti-Semitism)\b.*$'),
    re.compile(r'(?is)\s+Tags\s+(?:Iran|Israel|North\s+Korea|Office\s+of|Secretary|Anti-Semitism)\b.*$'),
    # Modern state.gov tag cloud — "Tags <topic list> Office of the Spokesperson ..." at doc end.
    # Optional [N] word... or * word prefix before "Tags" handles footnote-marker variants.
    re.compile(r'(?is)\s+(?:\*\s+\w+\s+|\[\d+\]\s+(?:\w+\s+){1,4})?\bTags\b\s+\S.*?\bOffice\s+of\s+the\s+Spokesperson\b.*$'),
    re.compile(r'(?is)\s+Released\s+on\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+(?:19|20)\d{2}\s+Updates\b.*$'),
    re.compile(r'(?is)\s+The\s+Office\s+of\s+Electronic\s+Information,\s+Bureau\s+of\s+Public\s+Affairs,\s+manages\s+this\s+site\b.*$'),
    re.compile(r'(?is)\s*\[?As\s+released\s+by\s+the\s+Office\s+of\s+the\s+Spokesman\b.*$'),
    re.compile(r'(?is)[ \t]+PRN:\s+\S+[^\n]*$'),
    re.compile(
        r'(?is)\s*(?:Secretary(?:\'s)?\s+Home\s+Page|'
        r'State\s+Department\s+Home\s+Page|DOSFAN\s+Home\s+Page)\s*(?:\|.*)?$'
    ),
    re.compile(r'(?is)\s+Updates\s*\|\s*Frequent\s+Questions\b.*$'),
    re.compile(r'[ \t]+\d{4}/\d+\s*$'),
    # Kerry/Clinton state.gov sidebar nav scraped inline (not on own line, so NAV_BLOCK_RE misses it).
    # "In This Section: Go Back" starts before the document midpoint in these rows, so
    # _STATE_NAV_ANCHOR_RES (position-gated at half) also misses them — hence FOOTER_RES here.
    re.compile(r'(?is)\s+\bIn\s+This\s+Section:\s+Go\s+Back\b.*$'),
    # 7 Clinton rows where the same nav block starts mid-phrase (missing "In This Section" prefix);
    # "Key Officers of Foreign Service Posts" is a unique state.gov directory link, never in speeches.
    re.compile(r'(?is)\s+\bKey\s+Officers\s+of\s+Foreign\s+Service\s+Posts\b.*$'),
    # Treasury cross-reference footer on State Dept sanctions announcements (Blinken/Pompeo, ~48 rows).
    # Variants: "on today's action/actions/designations", "about these designations",
    # "about the sanctioned entities", "about today's designations", with/without apostrophe.
    re.compile(r"(?is)\s+For\s+(?:additional|more)\s+information\b[^.]{0,80}"
               r"please\s+see\s+the\s+Department\s+of\s+the\s+Treasury.*$"),
    # v2026.10.02 patch: Clinton/Kerry state.gov business sidebar scraped onto the end of
    # 7 op-ed/remarks rows ("... Go Back Highlights Learn More Business Business Support:
    # Frequently Asked Questions (FAQ) Office of Global Partnerships ...").  The phrase
    # sequence is a nav menu, never speech.
    re.compile(r'(?is)\s+\bGo\s+Back\s+Highlights\s+Learn\s+More\s+Business\b.*$'),
]

# ── v4.14 boilerplate stripping patterns ──────────────────────────────────────

# Pass 1: doc-end markers on their own line (###, # # #, [END], [End of Document], ---, * * *)
DOC_END_RE = re.compile(
    r'(?m)^[ \t]*(?:#{3,}|(?:#\s+){2,}#|\[END(?:\s+of\s+Document)?\]'
    r'|(?:-\s*){3,}|(?:\*\s*){3,})\s*$.*',
    re.DOTALL | re.IGNORECASE,
)

# Pass 1b: [End of Document] inline at end of text (no preceding newline — Albright era)
DOC_END_INLINE_RE = re.compile(r'\s*\[End(?:\s+of\s+Document)?\]\s*$', re.IGNORECASE)

# Pass 1c: inline spaced-hash separator (# # # ...) at text end (Clinton era)
SPACED_HASH_INLINE_RE = re.compile(r'\s+(?:#\s+){2,}#.*$', re.DOTALL | re.IGNORECASE)

# Pass 2: "Return to … Home Page" navigation — match the phrase itself;
# a helper function strips back to the clean cut point.
RETURN_HOME_CORE_RE = re.compile(
    r'(?i)\bReturn\s+to\s+(?:the\s+)?'
    r'(?:DOSFAN|Secretary(?:\'s)?(?:\s+of\s+State)?'
    r'|Department\s+of\s+State|State\s+Department)'
    r'\s+Home\s+Page\b'
)

# Pass 3: leading press-release header lines
PRESS_HEADER_RE = re.compile(
    r'^(?:[ \t]*(?:FOR\s+IMMEDIATE\s+RELEASE|PRESS\s+RELEASE'
    r'|OFFICE\s+OF\s+THE\s+SPOKESMAN'
    r'|U\.S\.\s+DEPARTMENT\s+OF\s+STATE'
    r'|Department\s+of\s+State)[ \t]*\n)+',
    re.IGNORECASE,
)

# Pass 5: navigation sidebar blocks
NAV_BLOCK_RE = re.compile(
    r'(?im)^(?:In\s+This\s+Section|Related\s+(?:Topics|Links|Resources)'
    r'|Other\s+(?:Resources|Links))[^\n]*\n'
    r'(?:[ \t]*[^\n]+\n)*',
)

# Pass 6: standalone click/learn-more/read-more anchor lines
STANDALONE_NAV_RE = re.compile(
    r'(?im)^[ \t]*(?:Click\s+here|Learn\s+more|Read\s+more'
    r'|View\s+more|Download\s+(?:PDF|the\s+full))[^\n.]{0,60}$\n?'
)

# Pass 7: trailing bare URL lines
TRAILING_URL_RE = re.compile(
    r'(?:\n+[ \t]*(?:https?://|www\.)\S+[ \t]*)+$',
    re.IGNORECASE,
)

# Pass 8: 2009-2017 archive notice (Clinton/Kerry trailing banner)
ARCHIVE_2009_RE = re.compile(
    r'(?i)\s*(?:Back\s+to\s+Top\s+)?(?:Top\s+)?'
    r'You\s+are\s+entering\s+the\s+2009[-–]2017\s+Archive\b.*',
    re.DOTALL,
)

# Pass 10: trailing bare press-release number YYYY/NNN (Rice)
DOC_NUMBER_RE = re.compile(r'(?m)\s*^\d{4}/\d+\s*$.*', re.DOTALL)

# Pass 11: legacy "Updates | Frequent Questions | Contact Us" nav fragment (Powell era)
_UPDATES_NAV_RE = re.compile(
    r'Updates\s*\|\s*Frequent\s+Questions\s*\|\s*Contact\s+Us',
    re.IGNORECASE,
)

# Pass 12: inline "Released on [Month DD, YYYY]" press-release date stamp
_RELEASED_ON_RE = re.compile(
    r'Released\s+on\s+(?:January|February|March|April|May|June|July|'
    r'August|September|October|November|December)\s+\d{1,2},\s+\d{4}',
    re.IGNORECASE,
)

# Pass 13: boundary doc reference codes YYYY/NNN at start or end of text (but not mid-text
# year ranges like "1997/1998 period")
_BOUNDARY_DOC_CODE_RE = re.compile(r'\b\d{4}/\d{3,4}\b')

# Stage direction patterns — stripped from SOS Text only (not Natural Text / Raw Text)
_STAGE_DIR_AUDIENCE_RE = re.compile(
    r'\(\s*(?:laughter(?:\s+and\s+applause)?|applause(?:\s+and\s+laughter)?|'
    r'sustained\s+applause|cheers?|cheering|clapping|booing|'
    r'laughter\s+off[- ]?mike)\s*\.?\s*\)',
    re.IGNORECASE,
)
_STAGE_DIR_INAUDIBLE_RE = re.compile(
    r'\(\s*inaudible[^)]{0,30}\)',
    re.IGNORECASE,
)
_STAGE_DIR_LANGUAGE_RE = re.compile(
    r'\(\s*(?:in\s+(?:french|spanish|arabic|korean|russian|chinese|mandarin|japanese|'
    r'german|portuguese|italian|hebrew|farsi|persian|turkish|polish|ukrainian|thai|'
    r'vietnamese|hindi|urdu|swahili|english)|via\s+interpreter)\s*\.?\s*\)',
    re.IGNORECASE,
)
# Rubio path only (strip_stage_directions(rubio_path=True), audit X-09): "(Via
# translation)", "( Via translation)", "(Via interpretation)" -- 17189, 17347, 17357,
# 17530, 17630.  Kept out of _STAGE_DIR_LANGUAGE_RE so other secretaries' rows stay
# bit-identical.
_STAGE_DIR_VIA_TRANSLATION_RE = re.compile(
    r'\(\s*(?:via|through)\s+(?:an\s+)?(?:translation|translator|interpretation|interpreter)'
    r'\s*\.?\s*\)',
    re.IGNORECASE,
)
_STAGE_DIR_TRANSCRIPT_RE = re.compile(
    r'\(\s*(?:off[- ]?mike|off\s+the\s+record|break|in\s+progress|'
    r'closed\s+press\s+coverage)\s*\.?\s*\)',
    re.IGNORECASE,
)
_STAGE_DIR_CEREMONY_RE = re.compile(
    r'\(\s*[Tt]he\s+[^)]{5,60}\s+(?:was|were|has\s+been)\s+'
    r'(?:recited|presented|played|sung|performed|observed|awarded|given)\s*\.?\s*\)',
    re.IGNORECASE,
)

# v2026.10.02 patch: translation-link header at the very start of a document
# ("PDF version As Prepared Persian Translation [PDF] ...", "Translations: Arabic (top
# PDF)| Hebrew (bottom PDF) ...").  Start-anchored, so a Secretary's own bracketed
# translation ("... Danke. [Translation: Good morning ...]") is never touched.
_LEADING_TRANSLATION_LINKS_RE = re.compile(
    r'^\s*(?:PDF\s+version\s+)?(?:As\s+Prepared\s+)?(?:[A-Z][a-z]+\s+)?Translations?\s*'
    r'(?:\[PDF\]|:\s*(?:[A-Z][a-z]+\s*\((?:top|bottom)\s+PDF\)\s*\|?\s*)+)\s*'
)

# ── end boilerplate patterns ──────────────────────────────────────────────────

# ── Encoding normalization ────────────────────────────────────────────────────

_CTRL_CHAR_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_LONE_A_CIRCUMFLEX_RE = re.compile(r'\xc2(?![\x80-\xbf])')
# v2026.10.03 patch: a broken paragraph-separator character that reached the source
# pages as "ã——" (always at a paragraph boundary, 17 Clinton/Kerry rows, also in Raw
# Text).  It carries no content; removing it keeps the paragraph break.  Matched only
# as the exact two-em-dash unit, so Portuguese "ã" (São Paulo, não) is never touched.
_SEPARATOR_MOJIBAKE_RE = re.compile(r'[ \t]*(?:ã——)+[ \t]*(\n?)')
# Guard for Natural Text recovery: Raw Text on 2009-2017 archive pages starts with
# "Skip to Main Content / You are viewing: / Archived / Content /..." which does not
# match MODERN_ARCHIVE_HEADER_RE (wrong phrasing) so strip_leading_headers() leaves it.
# If recovered content still starts with nav text, revert the recovery.
_RECOVERY_NAV_GUARD_RE = re.compile(
    r'(?i)^\s*(?:skip\s+to|share\s+this\s+page|you\s+are\s+viewing\s*:\s*(?:archived)?)',
    re.MULTILINE,
)
# Guard for speaker extraction: exclude DEPUTY/ASSISTANT SECRETARY labels from is_sec.
_DEPUTY_TITLE_RE = re.compile(r'(?i)^\s*(?:DEPUTY|ASSISTANT)\s+SECRETARY')


def _fix_mojibake(text: str) -> str:
    """Re-encode as Latin-1 and detect UTF-8 via charset-normalizer."""
    try:
        raw = text.encode('latin-1')
    except UnicodeEncodeError:
        return text  # contains true Unicode > U+00FF — not mojibake
    result = _cn_from_bytes(raw)
    best = result.best()
    if best and 'utf' in best.encoding.lower():
        return str(best)
    return text


def normalize_encoding(text: str) -> str:
    """Unescape HTML entities, strip control chars, repair UTF-8 mojibake."""
    if not text:
        return text
    text = html.unescape(text)
    text = _CTRL_CHAR_RE.sub('', text)
    text = ftfy.fix_text(text)
    # Fallback: ftfy misses Ã+continuation-byte mojibake when legitimate high-Unicode
    # chars (em-dash, curly quotes) are present in the same text. Decode manually.
    text = re.sub(r'Ã([\x80-\xbf])',
                  lambda m: bytes([0xC3, ord(m.group(1))]).decode('utf-8', errors='replace'),
                  text)
    text = _LONE_A_CIRCUMFLEX_RE.sub('', text)  # strip lone Â scraping artifact
    text = strip_separator_mojibake(text)
    return text


def strip_separator_mojibake(text: str) -> str:
    """Remove the "ã——" paragraph-separator artifact, keeping the break (v2026.10.03)."""
    if not text or 'ã——' not in text:
        return text
    text = _SEPARATOR_MOJIBAKE_RE.sub(lambda m: m.group(1) or ' ', text)
    return text.strip(' ') if text[:1] == ' ' or text[-1:] == ' ' else text


# ── MinHash / LSH constants (near-duplicate detection) ───────────────────────

_LARGE_PRIME = (1 << 61) - 1
_MH_PARAMS   = [(i * 2654435761 + 1, i * 2246822519 + 7) for i in range(64)]
_MH_BANDS    = 16   # 64 hashes / 16 bands of 4 — high sensitivity at Jaccard ≥ 0.85
_MH_ROWS     = 4

# ── end encoding / MinHash constants ─────────────────────────────────────────

# ── v4.22 row-level patches ───────────────────────────────────────────────────
# All v4.18 row-specific patches (ROWS_TO_DROP, COPY_NAT_TO_SOS,
# CLINTON_WAYBACK_REFETCH) were applied in v4.18 against original Row IDs
# BEFORE renumbering (line 1026: r['Row Id'] = str(i)).  v4.19 reads the
# renumbered v4.18 output, so those original IDs no longer exist — all sets
# must be empty to avoid hitting wrong rows by coincidence.

ROWS_TO_DROP: set[str] = set()  # v4.28 dropped 4830/4832; those positions now hold legitimate rows

COPY_NAT_TO_SOS: set[str] = {
    # Wrong-extraction rows (contaminated SOS Text; Natural Text is clean labeled transcript)
    '2981',   # Powell, TRANSLATOR: turns in SOS Text — re-extract from Natural Text
    '8267',   # Kerry, "SECRETARY-GENERAL BAN: Do you want to say --" prefix in SOS Text
    # Wrong-person rescue rows (Natural Text properly labeled; re-extraction isolates Secretary turns)
    '13732',  # Blinken, SOS=Raab's speech; Natural Text has SECRETARY BLINKEN:/FOREIGN SECRETARY RAAB:
    '2175',   # Powell, SOS=Powell intro+Natsios speech; Natural Text has SECRETARY POWELL:/ADMINISTRATOR NATSIOS:
    # Rows 3120 and 3577 (Rice, International Women's Day 2005/2006) are in SOS_TEXT_PATCHES instead:
    # their Natural Text also has breadcrumb prefix, so COPY_NAT_TO_SOS would not fix the issue.
    # v4.28: Row 8355 (Kerry, Huffington Post Op-Ed) — SOS Text was just the op-ed title;
    # full body is in Natural Text. SOS_TEXT_PATCHES overrides below to strip the "Media Note" preamble.
    '8355',
}

# Natural Text ← SOS Text for rows where Natural Text is corrupted and SOS is the full content.
COPY_SOS_TO_NAT: set[str] = {
    '4414',   # Rice, Excerpt From Meeting With Hu Jintao — Natural Text is website footer
    '4175',   # Rice, Remarks to the Press Regarding Lebanon — Natural Text is website footer
    '4929',   # Clinton, Lao New Year — Natural Text has same irreparable Lao script corruption as SOS Text;
              # copy the SOS_TEXT_PATCHES-fixed SOS content to Natural Text
    '10495',  # Kerry, Remarks on Middle East Peace (UNHCR) — Natural Text is Jolie intro, wrong doc
}

# Row IDs (v4.28 output, post-drop) confirmed as legitimate short utterances — excluded
# from coherence advisory flags. Derived from v4.25 coherence audit + v4.28 adjustments:
# - Removed: 4548, 4830 (dropped), 7467, 8355, 14980, 16325 (all fixed with long SOS Text)
# - All IDs > 4832 decremented by 2 (rows 4830 and 4832 dropped before renumbering)
# One genuine gap remains unwhitelisted: Row 5415 (Clinton/Smiley interview truncation, was 5417).
COHERENCE_WHITELIST: set[str] = {
    '35', '1392', '1425', '1429', '1460', '1461', '1563', '1841', '1842', '1890',
    '1920', '2013', '2060', '2087', '2198', '2337', '2338', '2826', '2851', '2963',
    '4056', '4380', '4566', '4681', '4684', '4743', '4790', '5334', '5580', '5656',
    '5766', '6035', '6260', '6323', '7422', '8023', '8581', '8747', '9421', '9491',
    '10452', '10703', '10835', '10854', '10926', '10964', '11011', '11012', '11022', '11033',
    '11036', '11037', '11038', '11070', '11145', '11146', '11246', '11411', '11421', '11468',
    '11470', '11473', '11627', '11903', '12024', '13257', '13933', '14314', '14410', '15339',
    '15693', '16121', '16531', '16695', '17065', '17266',
}

CLINTON_WAYBACK_REFETCH: dict[str, str] = {}

ALERT_SHELL_ROWS: set[str] = set()

# Rows where Secretary column is misattributed — patch to correct secretary name.
SECRETARY_PATCHES: dict[str, str] = {}  # v4.28 applied '6367'→Kerry; v4.28 output row 6365 already correct

# Rows where Speech Type is misclassified — patch applied AFTER infer_speech_type().
# All 33 rows have Location='Op-Ed' and Raw Text confirms "Op-Ed / [Publication]"
# but Speech Type was labeled Remarks/Speech/Press Statement in the source data.
# Row 8355 (Kerry, Huffington Post) is included and also gets SOS Text fixed below.
SPEECH_TYPE_PATCHES: dict[str, str] = {
    '1371': 'Op-Ed',   # Powell — Op-Ed The New York Times
    '1541': 'Op-Ed',   # Powell — Op-Ed Wall Street Journal
    '1678': 'Op-Ed',   # Powell — Op-Ed The Atlanta Journal-Constitution
    '1955': 'Op-Ed',   # Powell — Op-Ed The Washington Post
    '2028': 'Op-Ed',   # Powell — Op-Ed Wall Street Journal
    '2234': 'Op-Ed',   # Powell — Op-Ed
    '2240': 'Op-Ed',   # Powell — Op-Ed
    '2256': 'Op-Ed',   # Powell — Op-Ed
    '2332': 'Op-Ed',   # Powell — Op-Ed
    '2357': 'Op-Ed',   # Powell — Op-Ed
    '2389': 'Op-Ed',   # Powell — Op-Ed
    '2399': 'Op-Ed',   # Powell — Op-Ed
    '2407': 'Op-Ed',   # Powell — Op-Ed
    '2454': 'Op-Ed',   # Powell — Op-Ed
    '2465': 'Op-Ed',   # Powell — Op-Ed
    '2491': 'Op-Ed',   # Powell — Op-Ed
    '2572': 'Op-Ed',   # Powell — Op-Ed
    '2584': 'Op-Ed',   # Powell — Op-Ed
    '2619': 'Op-Ed',   # Powell — Op-Ed
    '2643': 'Op-Ed',   # Powell — Op-Ed
    '2716': 'Op-Ed',   # Powell — Op-Ed
    '2735': 'Op-Ed',   # Powell — Op-Ed
    '2800': 'Op-Ed',   # Powell — Op-Ed
    '2881': 'Op-Ed',   # Powell — Op-Ed
    '2956': 'Op-Ed',   # Powell — Op-Ed
    '8902': 'Op-Ed',   # Kerry — Op-Ed
    '9248': 'Op-Ed',   # Kerry — Op-Ed
    '9271': 'Op-Ed',   # Kerry — Op-Ed
    '9416': 'Op-Ed',   # Kerry — Op-Ed
    '10220': 'Op-Ed',  # Kerry — Op-Ed
    '10433': 'Op-Ed',  # Kerry — Op-Ed
    '11616': 'Op-Ed',  # Pompeo — Op-Ed
    '12145': 'Op-Ed',  # Pompeo — Op-Ed
    '8355': 'Op-Ed',   # Kerry — Huffington Post Op-Ed; SOS Text also fixed in SOS_TEXT_PATCHES
}

# Rows where SOS Text is manually patched with correct secretary speech content.
# Used when SOS Text and Natural Text both have headers/labeling errors that prevent re-extraction.
SOS_TEXT_PATCHES: dict[str, str] = {
    # Clinton, Lao New Year 2009 — Lao script characters (§¡-©ó-¯ó-ÃÏú) are irreparably
    # corrupted (bytes decoded as Latin-1 with non-continuation high bytes). Replace with
    # the romanized transliteration already present in the source text.
    '4929': (
        "On the occasion of the traditional Lao New Year celebration, I would like to extend "
        "my warm wishes for a peaceful, prosperous year to the Government and the people of "
        "the Lao People's Democratic Republic. In this year of new beginnings, I look forward "
        "to continuing and enhancing cooperation between our two countries on many areas of "
        "mutual interest. Sook dii pii mai – Happy New Year!"
    ),
    # Rice International Women's Day 2005 — both SOS and Natural Text start with nav breadcrumb.
    # Speech content extracted from Natural Text after stripping header up to last date line.
    '3120': (
        "As the world celebrates International Women's Day tomorrow, I will have the honor of "
        "welcoming to Washington women leaders from Afghanistan and Iraq. These women represent "
        "the millions of others back in their homelands who have inspired the international "
        "community with their courage, determination, and faith in the force of freedom.\n"
        "One of the women who will be with us, Nermin Othman, Iraq's Minister of Women's "
        "Affairs, told us last year, \"The terrorists will not succeed this time. We are "
        "determined to win.\" Minister Othman was exactly right. Women turned out in force to "
        "vote in the January elections, and will represent over 30 percent of the new National "
        "Assembly.\n"
        "We believe that democracy must be homegrown and that elections are a critical step "
        "toward it. We recognize that despite the success of the recent elections, the people of "
        "Afghanistan and Iraq face many challenges ahead. Freedom, the protection of fundamental "
        "human rights, economic opportunity and prosperity, equality and the rule of law – "
        "these are all elements of the democratic process. Women are integral to the process of "
        "building responsible governments and democratic institutions. Women's participation and "
        "empowerment at all levels of society will be key to moving these new democracies "
        "forward.\n"
        "As partners in the pursuit of freedom, the United States will continue to support these "
        "women as they move toward building peaceful, prosperous and democratic societies. "
        "President Bush has said, “Our goal…is to help others find their own voice, "
        "attain their own freedom and make their own way.” The women of Iraq and Afghanistan "
        "are indeed finding their own voices, and they are being heard throughout the world."
    ),
    # Rice International Women's Day 2006 — same breadcrumb issue.
    '3577': (
        "As we observe International Women's Day 2006, I am proud to honor the diverse "
        "contributions of women in advancing the common cause of freedom, justice, and equality. "
        "Over the past year, we have witnessed women of courage, compassion, strength, and hope "
        "playing transformative roles in their societies, inspiring the international community, "
        "and enriching our world.\n"
        "Around the globe, women are making impressive political gains. In countries like "
        "Afghanistan, Iraq, Kuwait, Lebanon, Morocco, and throughout the broader Middle East, "
        "women are contributing to and leading democratic change that cannot be stopped. Many "
        "have put their lives at risk to forge free and democratic societies for future "
        "generations. Today more women than ever before have the right to vote. More women are "
        "running for office, and many have been elected as leaders in their countries. In "
        "Liberia, Chile, and Germany, for the first time women have been elected to their "
        "country's highest office.\n"
        "Even as we celebrate the fact that more women have taken their rightful place in "
        "society, we must also recognize that there is still much to be done. In too many parts "
        "of the world women still struggle for basic liberties under governments that tolerate "
        "discrimination, systematic abuse, exploitation, and violence against women. We must not "
        "accept women being sold into sexual slavery, terrorized by rape, denied an education, "
        "killed for so-called \"honor crimes\", or silenced for speaking out. We are resolved to "
        "remove the barriers to political, economic, and social empowerment so that women "
        "everywhere may freely chart their own way.\n"
        "This week we honor brave women such as Aung San Suu Kyi, who remains under house "
        "arrest in Burma. Her efforts to bring democracy are a source of inspiration to women "
        "around the world. We must not forget that the advance of women's rights and the advance "
        "of human liberty go hand in hand. Around the world, women of courage are standing up "
        "for freedom and human dignity. The United States stands with them."
    ),
    # v4.28 — Rice/Hariri partial transcript; full speech from Raw Text, artifact stripped
    '4548': (
        'I would like to thank Sheikh Saad for hosting me here with members of the March 14 coalition. We remember well former Prime Minister Rafik Hariri and the fact that he was brutally assassinated, but in giving his blood really helped to launch Lebanon on a more democratic and independent path. And we’ve talked today about the support of the United States for the continuation of a path of greater democracy, greater freedom, about the United States’ commitment to a Lebanon that is truly sovereign and independent and where foreign interference and where foreign intimidation should never be permitted. So thank you very much.\n'
        '\n'
        'Sheikh Saad.\n'
        '\n'
        'For having me here.\n'
    ),
    # v4.28 — Blinken/DRC; full speech from Natural Text; pipeline strips (Via interpreter)/(In English)
    '14980': (
        '(Via interpreter)\n'
        '\n'
        "I am delighted to meet with the prime minister and to continue the discussions we started yesterday.  I do know that we have a very important agenda around various topics: food security, climate, the work that we're conducting together to bring peace and security in the east of Congo. All of these topics reflect the depth of our relationship.\n"
        "(In English) It's a pleasure to be here working on so many different things with the prime minister, following up on the discussions that we had yesterday with the president. I think the agenda that we have is proof of the breadth and depth of the partnership between the United States and the DRC, a partnership, as we've called it, for peace and prosperity, for preservation of the environment. And I'm very pleased with the opportunity to speak with the prime minister today about the work that we're doing together. Thanks.\n"
    ),
    # v4.28 — Kerry Huffington Post Op-Ed; preamble/footer stripped
    '8355': (
        "I'm sometimes asked how, as someone who testified 42 years ago against the Vietnam War in which I had fought, I could testify in favor of action to hold the Assad regime accountable today. The answer is, I spoke my conscience in 1971 and I'm speaking my conscience now in 2013. Secretary Hagel and I support limited military action against Syrian regime targets not because we've forgotten the lessons and horrors of war -- but because we remember them. Make no mistake: If another Vietnam or another Iraq were on the table in the Situation Room, I wouldn't be sitting at the witness table before Congress advocating for action. I spent two years of my life working to stop the war in Vietnam, and made enemies and lost friends because of my decision to speak my mind. So I don't come to my view on the use of military force anywhere without real reflection. I do so with an eye towards facts and reason. I am informed by Vietnam, not imprisoned by it. And I am informed by Iraq, not imprisoned by it, either. The faulty intelligence of the Iraq War was a legacy burned into all of us who present the case for action in Syria to the Congress: It has made us press with extra urgency to know that we are highly confident of what we speak now. For me and for Chuck Hagel, who voted once before on an intelligence case that turned out not to be true -- and regretted it deeply -- we would never put any Member of Congress in that same position today, period. I understand the temptation to remember Vietnam and Iraq and reflexively paint any subsequent possible military action with the same brush. But to do so ignores what Syria is, and what it isn't. There will be no boots on the ground in Syria. There will be no open-ended commitment. There will be no assuming responsibility for another country's civil war. These and other differences with Iraq are the exact reasons why many members of Congress who opposed that war and voted against it are supporting this action against Syria today. So what is Syria? It would be a tailored action to make clear that the world will not stand by and allow the international norm against the use of chemical weapons to be violated with impunity by a brutal dictator willing to gas hundreds of children to death while they sleep. Our action would be a limited and targeted military action, against military targets in Syria, designed to deter Syria's use of chemical weapons and degrade the Assad regime's capabilities to use or transfer such weapons in the future. So what's at stake here that caused the President to come to the Congress and ask Congress to authorize action? Those of us who believe in the international order and believe in efforts to ensure that certain international norms against chemical weapons are respected -- we have much at stake in this debate. For nearly 100 years, the world has stood up for an international norm against the use of chemical weapons. There's a reason why the United States joined the Geneva Conventions. There's a reason why the United States and 98 percent of the world are signatories to the Chemical Weapons Convention. The reason is this: our nation is safer when these norms are strong. Our interests are protected when these weapons cease to exist. Our allies and partners are protected when these threats are reduced. The world agrees with us: chemical weapons were used in east Damascus on August 21st. Dozens of countries or organizations around the world acknowledge the use of chemical weapons in Syria, and of those, many have said so publicly. Many countries or organizations have also stated in public or in private that the Assad regime is responsible. And we continue building support around the world every day. As part of that effort, this weekend I will meet with European Foreign Ministers in Vilnius, Lithuania, where I will continue to lay out the evidence we have collected and seek to broaden support for a limited military response to deter the Assad regime from launching another chemical weapons attack. Let me be clear: I have no doubt that Assad will use chemical weapons again and again unless we take action. I have no doubt that we will never get to the negotiating table for the peace talks we have pushed for if Assad believes he can gas his way out of his predicament, just as we'd never have gotten to the peace talks that lead to the Dayton Accords if military action hadn't been part of the equation. I have no doubt that if we look the other way, we risk not only Assad's repeated use of chemical weapons within Syria, but downstream consequences for our allies and friends in the region including Israel, Turkey, Jordan, Lebanon, and Iraq. When I hear firsthand about panicked parents in Israel rushing to buy gas masks for their children, I am reminded of so many who live so close to Assad's reign of terror. And I have no doubt that for anyone who wants to see a diplomatic solution to two of the world's most pressing proliferation challenges -- Iran and North Korea -- ask yourselves: Are these two countries more or less likely to plunge ahead with proliferation and provocation if they see Assad's actions go unanswered? I would argue that we all know the answer to this question: They are more likely to do so. The costs of inaction here are much greater than the costs of action. Some people have asked why we would consider acting without the backing of the United Nations Security Council. It's the same reason that President Clinton in Kosovo did not bind his conscience to a Russian or Chinese veto in New York: In Kosovo, without a single American combat casualty, countries of conscience acted and the world is a better place because we did. It was the right thing to do then, and it is the right thing to do now. We already know who used chemical weapons. We know when they were used and how they were used. We wish the United Nations today were in a position to defend these norms rather than being blocked from acting by Russian and Chinese obstruction -- because we believe in the institution. But we also believe in the principle that we cannot turn our backs and say there's nothing we can do. We cannot allow these weapons to be used to slaughter innocents with impunity. This is a vote of conscience. And I know that the same reasons that compelled me to join the United States Navy and serve, and the same reasons that compelled me to speak out against the war in which I'd fought, tell me now that the cause of conscience and conviction is the cause for action in Syria.\n"
    ),
    # v4.28 — Clinton UN sustainability speech; "Secretary General Sha:" false label cut fixed
    '7467': (
        'Good morning. To President Rousseff, the Brazilian Government, the Brazilian people for hosting us, to Secretary General Ban, Secretary General Sha: Thank you for convening this conference. And thanks to all of you representing governments, civil society, the private sector, young people, men, women, and children everywhere.\n'
        "Brazil has done the world a great service by hosting us all here. This can be a fractious time. But thanks to Brazil's deft and effective leadership, we have coalesced around an outcome document that marks a real advance for sustainable development. We know this is one of the most pressing matters of our time, because how we grow together over the long term isn't a question for only some countries. It is a question for all countries. And here in Rio, thanks to Brazil, we are at the center of our shared efforts to find answers.\n"
        'I want to thank the President of Samoa for his remarks and the reminder that we meet at a critical moment. For some countries and some people around the world, this is not just a matter for long-term planning, but for immediate, pressing action. And we know that voices are being raised demanding expanded opportunities and a greater role in the decisions that affect the lives of us all. We have the potential to answer that call. Hundreds of millions of people have been lifted out of extreme poverty in just the past generation, laying the groundwork for even more progress. We are working together to end chronic hunger, an area where Brazil has shown particularly strong leadership. I believe we can end preventable child deaths and chart a path towards an AIDS-free generation.\n'
        "In short, this is a time for us to be pragmatic, but also optimistic. A more prosperous future is within our reach, a future where all people benefit from sustainable development no matter who they are or where they live. But let's be honest. We know what is possible. We know what we could do. But we also know that future is not guaranteed, because the resources that we all depend upon – fresh water, thriving oceans, arable land, a stable climate – are under increasing pressure. And that is why, in the 21st century, the only viable development is sustainable development. The only way to deliver lasting progress for everyone is by preserving our resources and protecting our common environment.\n"
        'So we have come together, here in Rio, to identify practical ways we can all promote sustainable development. And while our views may differ sometimes, I believe we agree on some fundamental principles. We cannot be boxed in by the orthodoxies of the past. We should and must make decisions based on research and scientific evidence about what works. And above all, we need fresh, agile, action-oriented partnerships that can produce results year after year after year.\n'
        'So while the outcome document adopted here contains many important principles and proposals, the most compelling products of this conference are the examples of new thinking that can lead to models for future action. It should be said of Rio that people left here thinking, as the late Steve Jobs put it, not just big, but different.\n'
        'We should be thinking different about harnessing the power of the market. Remember in the 1960s, official development assistance accounted for 70 percent of the capital flows to developing nations, but today it amounts to only 13 percent, while at the same time, development budgets have actually increased. Why is that Well, you know very well. Because while continuing to provide assistance, the private sector investments, using targeted resources and smart policies, have catalyzed more balanced, inclusive, sustainable growth.\n'
        "The United States has taken this idea to heart. And earlier today, I helped launch a partnership between the United States and African nations that will use $20 million in U.S. Government funding to unlock hundreds of millions of dollars in private financing for clean energy projects in Africa and beyond. It's part of our contribution to the United Nations Sustainable Energy for All initiative, which has secured significant private sector investments for sustainable energy. And we hope to see even more coming out of Rio.\n"
        "You also see the power of the market in the Global Alliance for Clean Cookstoves, which aims to help 100 million families adopt clean cookstoves and fuels by 2020. By supporting consumer research and creating incentives for manufacturers, we're helping to create a market for stoves that people will pay for and use, while at the same time preventing health problems in women and children, and cleaning the air of black soot.\n"
        "Now in addition to tapping into the private sector, we should be thinking different about new types of partnerships to solve problems that might otherwise seem insurmountable. Here in Rio, the United States launched joint efforts on everything from deforestation and water to solid waste. We're also leading Feed the Future, a global effort to improve food security that is helping food producers adapt to climate change even as they reduce their own greenhouse gas emissions.\n"
        'And earlier this year, I was privileged to host six countries in the United Nations Environment Program as we launched the Climate and Clean Air Coalition. The goal is to reduce short-lived climate pollutants that cause more than 30 percent of current global warming, as well as millions of premature deaths and extensive crop losses. We know we have to keep working together on CO2, but we think that our Climate and Clean Air Coalition, to which many more countries are joining, and we welcome you, can take targeted action and produce results with respect to methane and black soot and HFCs.\n'
        "We also have to be thinking different about development in our cities. That is, after all, where most of the world's population lives today, where most of the growth is and will take place, and where innovative ideas are being put into action. Under the Joint Initiative on Urban Sustainability that President Rousseff and President Obama kicked off last year, we are bringing political officials from every level – from state, county, local, municipalities – together along with industry leaders and developers to find creative ways to generate sustainable economic growth. If, as I heard earlier today, that 70 percent of the structures that will be needed in 30 years to house, to provide economic opportunities for the world's population have yet to be built, then we have a tremendous opportunity we cannot waste.\n"
        'And finally, the only prosperous, sustainable economy is an inclusive economy. That means we should think different about how we recognize the needs of workers in the informal economy, how we unleash the talent and energy of young people, and how we act on the compelling evidence most recently published by the World Bank that women are essential drivers of sustainable development. I applaud the bold call to action issued here in Rio by UN Women, and likewise the Rio+20 outcome document devotes a strong section to expanding opportunities for women.\n'
        "And while I am very pleased that this year's outcome document endorses sexual and reproductive health and universal access to family planning, to reach our goals in sustainable development we also have to ensure women's reproductive rights. Women must be empowered to make decisions about whether and when to have children. And the United States will continue – (applause) – the United States will continue to work to ensure that those rights are respected in international agreements.\n"
        "Now none of this is an abstract discussion. There is just too much at stake, too much still to be done. And many of you visited the U.S. Center here in Rio and saw practical solutions related to some of the work I've discussed and other goals we hold in common. We believe solutions require action by all of us. Governments, yes; let's do our part. Let's do more than our part. Let's pave the way for more clean energy investments, take on the entrenched political and economic interests that stand in the way of clean energy, technology, and sources being used in nations around the world. Let's use the private sector, particularly the consumer goods companies, as they have agreed to do, to make sure they have sustainable supply chains, the right kind of packaging and marketing that puts the least amount of burden on the earth we share.\n"
        "Let's bring in the nonprofits, the civil society organizations, faith groups, individuals, all of us, committed to realizing the sustainable development goals that we have embraced. We know that we will be judged not by what we say nor even by what we intend to do, but by whether we deliver results for people alive today, and whether we keep faith with future generations. I'm very honored to be here with all of you, and I pledge my country's, the Obama Administration's, and my own personal efforts to continue our work together. We simply cannot afford to fail.\n"
        'Thank you all very much.\n'
    ),
    # v4.28 — Blinken Summit for Democracy; "Foreign Minister Cho:" false label cut fixed
    '16325': (
        'Thank you very, very much. President Yoon, Foreign Minister Cho:  Thank you for hosting this third Summit for Democracy. And I hope that the extraordinary spirit of nori that we just saw a few moments ago pervades the next three days, and indeed, helps us carry forward from Seoul as we not only do our work here, but then continue it in the many weeks and months ahead.\n'
        "It's particularly fitting – in fact, it's even a little bit poignant – that we're gathered here in Seoul, in Korea, for this third Summit for Democracy – a nation that transformed, over a single generation, into one of the strongest, most dynamic democracies in the world, a champion of democracy for the world.\n"
        'I also want to thank our partners who are here with us today from the United Kingdom and Ecuador in helping to carry forward the banner of the Summit for Democracy. And everyone present today: Thank you for being here.\n'
        "We come from nations large and small; East and West; developed and developing economies.  But we're all united by a fierce conviction: that democracy is the single most powerful tool for unleashing human potential and delivering results for our people.\n"
        'Three years ago, President Biden launched the Summit for Democracy, out of a recognition that democratic governance has reached what he calls an inflection point.\n'
        'There was a powerful wave of political opening around the world after the end of the Cold War, but that has been followed by nearly two decades of democratic backsliding.  In many places, basic freedoms have eroded, electoral rights have come under assault, corruption has eaten away at hope.\n'
        'Under the summit banner, over the past three years, we have risen together to meet this challenge.  More than 100 governments from every part of the world have committed to taking concrete action to strengthen and renew democracy both in their countries, but also working together to tackle shared threats around the world.\n'
        "We're fighting corruption.  Bulgaria is protecting whistleblowers and increasing accountability for public prosecutors.  Kenya adopted open contracting data standards for public procurement and created a public beneficial ownership register.  Canada is investing millions in combatting money laundering around the world.\n"
        "We're promoting free and fair elections.  Switzerland is supporting emerging democracies with technological expertise and guidance to reduce the risk of violence ahead of elections.\n"
        'Botswana is engaging regional bodies like the African Union and the regional economic communities to advocate for presidential term limits.  Australia is providing electoral support to Southeast Asian and Pacific nations.\n'
        "We're protecting independent media and journalists.  The United States has supported grants around more than a dozen countries to strengthen the long-term stability of media outlets through the International Fund for Public Internet Media – excuse me, Public Interest Media.  And we're improving the security of journalists, including those operating in exile, under the Journalism Protection Platform.\n"
        "We're upholding and defending human rights.  Nepal increased the statute of limitations to file a rape case.  Czechia has begun a program to issue emergency visas for human rights defenders who may be under threat.  The Bahamas launched a mobile tool to connect people with disabilities to services and employment opportunities.\n"
        'And critically, we are bringing partners outside of government into the important work of protecting and promoting democracy.  The United States is launching a new National Action Plan on Responsible Business Conduct.  This plan sets out more than 40 concrete actions across the U.S. Government to help ensure that the companies that we work with are actually upholding human rights.\n'
        'President Biden has made the defense of democracy a core pillar of American diplomacy – because safeguarding and accelerating our democratic renewal at home requires that we help shape a world in which democracies can thrive.\n'
        'Shoring up traditional pillars of democracy is more important than ever.\n'
        "But we also need to show that democracy is responsive to our people's needs and can deliver on the issues that matter most in their lives and for their livelihoods.\n"
        'Like the climate crisis – which, for millions of people, is literally an existential threat to their livelihoods today.\n'
        'Inclusive economic growth that protects the dignity of work and strengthens the economic competitiveness of democracies.\n'
        'Social rights, like food, health care, education, housing.\n'
        'The rights and needs of the vulnerable and historically marginalized: women and girls, people living with disabilities, the LGTBQI+ community, religious and ethnic minorities, young people.  Our democracy cannot be for some – it must be for all.\n'
        "That's why we're investing in new Summit for Democracy initiatives like the Youth Democracy Network to connect young democratic leaders across our countries.  We need to be informed and animated by their needs, by their aspirations – and act on them, if we want rising generations to support democratic societies.\n"
        "Revitalizing democracy will also require us to shape a technological future that's inclusive, that's rights-respecting, directed at driving progress in people's lives.\n"
        'As authoritarian and repressive regimes deploy technologies to undermine democracy and human rights, we need to ensure that technology sustains and supports democratic values and norms.\n'
        "A little bit later this morning, we'll have an opportunity to talk about ways to make sure that our technological progress – specifically artificial intelligence and digital tools – actually spur democratic renewal and do not undermine it.\n"
        "As President Biden often says, democracy doesn't happen by accident.  We have to renew it constantly.  And we have to confront our own shortcomings, transparently, even when it's uncomfortable, even when it's painful.\n"
        'We do so secure in the knowledge that democracy is a shared aspiration for people around the world – and that demand for representative democracy remains strong.\n'
        'Safeguarding our democracies is a collective effort, one that depends on our governments and on our people working together.  All people, all nations, all sectors have contributions to make.  And none of us has a monopoly on ideas, never mind good ideas.\n'
        "And that's, in a way, the power of the Summit for Democracy – the ability to actually share and scale solutions; to catalyze collective action; to shift norms.  In doing that, we continue to earn the confidence of our people and the confidence of the world in our democratic model.\n"
        'So to everyone here, thank you. Thank you for your engagement. Thank you for your leadership each and every day by each and every country here. And looking around this room, hearing the commitments from so many partners, I remain more than optimistic that we will – we will – meet the challenge of this moment.\n'
        'Thank you very much.\n'
    ),
    '14615': (
        "Well, good morning, everyone. It's a great pleasure to welcome Prime Minister Holness "
        "to Washington, to the State Department. Great to have you here. We now 60 years of "
        "independence to celebrate, 60 years of our own diplomatic relationship, but especially "
        "the work that we're doing together, today, that really is making a difference and making "
        "a difference in the lives of people in both our countries.\n"
        "I really want to applaud, Mr. Prime Minister, your very strong economic leadership in "
        "Jamaica, and our partnership in the Caribbean, which I think is never been better, and "
        "that's in large part due to your engagement and your leadership. So, we're very "
        "pleased to have you here to talk about many of the issues that bring us together in our "
        "own region, but also issues that are affecting people globally – from climate to "
        "COVID, to other things. And we're of course, very much forward to working together "
        "on the Summit of the Americas, which is coming up.\n"
        "So thank you very much for being here. I'm looking forward to a good conversation. Welcome."
    ),
}

TITLE_PATCHES: dict[str, str] = {
    '11027': 'On Otto Warmbier',
    '15756': 'United States Leads in Space with Diplomacy',
    '16616': '75th Anniversary of the Adoption of the Geneva Conventions of 1949',
}

# Location patches applied to v4.29 output Row IDs (= v4.30 input Row IDs;
# no rows were dropped in v4.29 so IDs are identical).
# v4.29 LOCATION_PATCHES NOT carried forward — v4.29 output already has those
# corrections applied; this dict targets only remaining anomalies.
LOCATION_PATCHES: dict[str, str] = {
    # ── CAT 1: Factually wrong locations ────────────────────────────────────
    '509':   'Brasília, Brazil',            # "Alongside Argentina" — Albright at Brazil NPT ceremony
    '8248':  'Washington, D.C.',            # "Vice President Joe Biden, Secretary of Treasury..." — U.S.-China S&ED
    '11396': 'Yokota Air Base, Japan',      # "On Plane ert Yokota..." — garbled text
    # ── CAT 2: En Route still in Location ───────────────────────────────────
    '16968': 'Jeddah, Saudi Arabia',        # En Route to Jeddah
    '17001': 'Miami, Florida',              # En Route Miami
    '17297': 'Doha, Qatar',                 # En Route to Doha
    # ── CAT 3a: Venue concatenated to Washington, D.C. (no intervening comma)
    '1553':  'Washington, D.C.',            # House International Relations Committee Washington, D.C.
    '2275':  'Washington, D.C.',            # Benjamin Franklin Room Washington, D.C.
    '2276':  'Washington, D.C.',            # Benjamin Franklin Room Washington, D.C.
    '2278':  'Washington, D.C.',            # Loy Henderson Auditorium Washington, D.C.
    '5372':  'Washington, D.C.',            # Treaty Room Washington, D.C.
    '5373':  'Washington, D.C.',            # Treaty Room Washington, D.C.
    '5580':  'Washington, D.C.',            # Washington Convention Center Washington, D.C.
    '5587':  'Washington, D.C.',            # Treaty Room Washington, D.C.
    '5780':  'Washington, D.C.',            # Treaty Room Washington, D.C.
    '6017':  'Washington, D.C.',            # Treaty Room Washington, D.C.
    '6133':  'Washington, D.C.',            # Treaty Room Washington, D.C.
    '6251':  'Washington, D.C.',            # Treaty Room Washington, D.C.
    '6356':  'Washington, D.C.',            # Treaty Room Washington, D.C.
    '6357':  'Washington, D.C.',            # Treaty Room Washington, D.C.
    '6369':  'Washington, D.C.',            # Old Executive Office Building Washington, D.C.
    '6523':  'Washington, D.C.',            # The Loy Henderson Auditorium Washington, D.C.
    '6533':  'Washington, D.C.',            # The White House Washington, D.C.
    '6537':  'Washington, D.C.',            # Loy Henderson Auditorium Washington, D.C.
    '6591':  'Washington, D.C.',            # Treaty Room Washington, D.C.
    '6709':  'Washington, D.C.',            # Treaty Room Washington, D.C.
    '6754':  'Washington, D.C.',            # Ronald Reagan Building Washington, D.C.
    '6878':  'Washington, D.C.',            # Benjamin Franklin Room Washington, D.C.
    '6891':  'Washington, D.C.',            # Benjamin Franklin Room Washington, D.C.
    '6892':  'Washington, D.C.',            # Benjamin Franklin Room Washington, D.C.
    '6899':  'Washington, D.C.',            # Treaty Room Washington, D.C.
    '6905':  'Washington, D.C.',            # Ben Franklin Room Washington, D.C.
    '7126':  'Washington, D.C.',            # Treaty Room Washington, D.C.
    '7137':  'Washington, D.C.',            # Treaty Room Washington, D.C.
    '7364':  'Washington, D.C.',            # Treaty Room Washington, D.C.
    '7425':  'Washington, D.C.',            # Treaty Room Washington, D.C.
    '7444':  'Washington, D.C.',            # George C. Marshall Center Washington, D.C.
    '7695':  'Washington, D.C.',            # Treaty Room Washington, D.C.
    '7804':  'Washington, D.C.',            # Benjamin Franklin Room Washington, D.C.
    '7905':  'Washington, D.C.',            # Newseum Washington, D.C.
    '7909':  'Washington, D.C.',            # Ben Franklin Room Washington, D.C.
    '7993':  'Washington, D.C.',            # Treaty Room Washington, D.C.
    '8069':  'Washington, D.C.',            # Treaty Room Washington, D.C.
    '8134':  'Washington, D.C.',            # Treaty Room Washington, D.C.
    '8168':  'Washington, D.C.',            # Treaty Room Washington, D.C.
    '8192':  'Washington, D.C.',            # Press Briefing Room Washington, D.C.
    '8468':  'Washington, D.C.',            # U.S. Capitol Washington, D.C.
    '8740':  'Washington, D.C.',            # Dean Acheson Auditorium Washington, D.C.
    '9481':  'Washington, D.C.',            # Benjamin Franklin Room Washington, D.C.
    '10677': 'Washington, D.C.',            # Loy Henderson Auditorium Washington, D.C.
    '10884': 'Washington, D.C.',            # Press Briefing Room Washington, D.C.
    # ── CAT 3b: Non-DC venue concatenated to city (no comma between venue/city)
    '1581':  'Louisville, Kentucky',        # University of Louisville, Kentucky
    '2565':  'Kuwait City, Kuwait',         # Released in Kuwait City, Kuwait
    '5452':  'Jeddah, Saudi Arabia',        # Dar Al Hekma College Jeddah, Saudi Arabia
    '5453':  'Jeddah, Saudi Arabia',
    '5454':  'Jeddah, Saudi Arabia',
    '5707':  'Lima, Peru',                  # JW Marriott Lima, Peru
    '5800':  'Islamabad, Pakistan',         # U.S. Embassy Islamabad, Pakistan
    '5802':  'Islamabad, Pakistan',
    '6086':  'Melbourne, Australia',        # Grand Hyatt Hotel Melbourne, Australia
    '6269':  'Port-au-Prince, Haiti',       # Ambassador's Residence Port-au-Prince, Haiti
    '6386':  'Cairo, Egypt',               # Four Seasons Cairo, Egypt
    '6471':  'Seoul, Republic of Korea',   # Grand Hyatt Hotel Seoul, South Korea (venue + Korea fix)
    '6566':  'Paris, France',              # OECD Conference Center Paris, France
    '6666':  'Vilnius, Lithuania',         # Radisson Hotel Lietuva Vilnius, Lithuania
    '6713':  'Istanbul, Turkey',           # Ciragan Palace Istanbul, Turkey
    '6911':  'Tripoli, Libya',             # Tripoli University Tripoli, Libya
    '6912':  'Tripoli, Libya',             # U.S. Embassy Tripoli, Libya
    '6922':  'Kabul, Afghanistan',         # U.S. Embassy Kabul Kabul, Afghanistan
    '6936':  'Dushanbe, Tajikistan',       # Ismaili Center Dushanbe, Tajikistan
    '6993':  'Manila, Philippines',        # Reuters Soundbite Manila, Philippines
    '7003':  'Bali, Indonesia',            # Bali International Convention Center Bali, Indonesia
    '7046':  'Bonn, Germany',              # Bonn Conference Center Bonn, Germany
    '7330':  'Beijing, China',             # Diaoyutai Club House Beijing, China
    '7338':  'Beijing, China',             # Diaoyutai Beijing, China
    '7339':  'Beijing, China',
    '7348':  'New Delhi, India',           # Ambassador's Residence New Delhi, India
    '7407':  'Yerevan, Armenia',           # U.S. Embassy Yerevan Yerevan, Armenia
    '7416':  'Baku, Azerbaijan',           # Embassy Baku Baku, Azerbaijan
    '7419':  'Istanbul, Turkey',           # Conrad Hotel Istanbul, Turkey
    '7485':  'St. Petersburg, Russia',     # Consul General's Residence St. Petersburg, Russia
    '7516':  'Ulaanbaatar, Mongolia',      # Government House Ulaanbaatar, Mongolia
    '7521':  'Hanoi, Vietnam',             # Foreign Trade University Hanoi, Vietnam
    '7533':  'Phnom Penh, Cambodia',       # Peace Palace Phnom Penh, Cambodia
    '7579':  'Dakar, Senegal',             # Presidential Palace Dakar, Senegal
    '7612':  'Abuja, Nigeria',             # Presidential Villa Abuja, Nigeria
    '7613':  'Abuja, Nigeria',             # U.S. Embassy Abuja, Nigeria
    '7636':  'Rarotonga, Cook Islands',    # New Zealand High Commissioner's Residence, Cook Islands
    '7663':  'Vladivostok, Russia',        # U.S. Consulate Vladivostok, Russia
    '7791':  'Pristina, Kosovo',           # Government Building Pristina, Kosovo
    '7852':  'Prague, Czech Republic',     # Reuters Soundbite Prague, Czech Republic
    '7865':  'Belfast, Northern Ireland',  # Stormont Castle Belfast, Northern Ireland
    '7947':  'Berlin, Germany',            # The Chancellery Berlin, Germany
    '7948':  'Berlin, Germany',            # U.S. Embassy Berlin Berlin, Germany
    '7958':  'Rome, Italy',               # Villa Madama Rome, Italy
    '8014':  'Baghdad, Iraq',             # Embassy Baghdad Baghdad, Iraq
    '8039':  'London, United Kingdom',    # Lancaster House London, United Kingdom
    '8051':  'Beijing, China',            # Diaoyutai Hotel Beijing, China
    '8052':  'Seoul, Republic of Korea',  # Grand Hyatt Hotel Seoul, South Korea (venue + Korea fix)
    '8074':  'Istanbul, Turkey',          # Adile Sultan Palace Istanbul, Turkey
    '8083':  'Brussels, Belgium',         # Egmont Palace Brussels, Belgium
    '8109':  'Moscow, Russia',            # Spaso House Moscow, Russia
    '8219':  'Kuwait City, Kuwait',       # Bayan Palace, Kuwait (also CAT 8 misc)
    '8453':  'Rome, Italy',              # Villa Taverna Rome, Italy
    '8537':  'London, United Kingdom',   # Winfield House London, United Kingdom
    '8645':  'Berlin, Germany',          # German Chancellery Berlin, Germany
    '9133':  'Honolulu, Hawaii',         # East-West Center Honolulu, Hawaii
    '9192':  'Jeddah, Saudi Arabia',     # Royal Terminal Jeddah, Saudi Arabia
    '9563':  'Tunis, Tunisia',           # Solidarity March in Tunis, Tunisia
    '9753':  'Vienna, Austria',          # Palais Coburg Vienna, Austria
    '10343': 'Manama, Bahrain',          # Four Seasons Hotel Manama, Bahrain
    '10352': 'Hiroshima, Japan',         # Grand Prince Hotel Hiroshima, Japan
    '10379': 'Houston, Texas',           # Rice University Houston, Texas
    '11509': 'Abu Dhabi, United Arab Emirates',  # Fairmont Hotel Abu Dhabi, UAE
    '14050': 'Doha, Qatar',             # Qatari Compound Doha, Qatar
    '15552': 'Addis Ababa, Ethiopia',   # Affairs Addis Ababa, Ethiopia
    '16271': 'Munich, Germany',         # Bayerischer Hof Munich, Germany
    '16272': 'Munich, Germany',
    '17024': 'Paris, France',           # Le Bourget Airport Paris, France
    # ── CAT 4: Three-part (venue, city, country — 2 commas) ─────────────────
    '10911': 'Beijing, China',           # Diaoyutai, Beijing, China
    '10912': 'Beijing, China',
    '10913': 'Beijing, China',
    '10921': 'Ankara, Turkey',           # Turkish Foreign Ministry, Ankara, Turkey
    '11499': 'Hanoi, Vietnam',           # Sofitel Metropole Hotel, Hanoi, Vietnam
    '11507': 'Abu Dhabi, United Arab Emirates',  # Fairmont Hotel, Abu Dhabi, UAE
    '11508': 'Abu Dhabi, United Arab Emirates',
    '12051': 'Beverly Hills, California',        # Beverly Wilshire Hotel, Beverly Hills, CA
    '12266': 'Indianapolis, Indiana',    # Indiana Convention Center, Indianapolis, Indiana
    '12289': 'Manhattan, Kansas',        # Kansas State University, Manhattan, Kansas
    '12330': 'Abu Dhabi, United Arab Emirates',  # Al Bateen Airport, Abu Dhabi, UAE
    '12338': 'New York City, New York',  # Lotte Palace Hotel, New York, New York
    '12413': 'Wichita, Kansas',          # Hilton Garden Inn Hotel, Wichita, Kansas
    '12414': 'Wichita, Kansas',
    '12417': 'Wichita, Kansas',
    # ── CAT 5: Country naming standardization ───────────────────────────────
    # Korea: South Korea / Korea → Republic of Korea
    '32':    'Demilitarized Zone, Republic of Korea',
    '391':   'Seoul, Republic of Korea',
    '1247':  'Seoul, Republic of Korea',
    '1248':  'Seoul, Republic of Korea',
    '1464':  'Seoul, Republic of Korea',
    '1954':  'Seoul, Republic of Korea',
    '1955':  'Seoul, Republic of Korea',
    '2063':  'Seoul, Republic of Korea',
    '3153':  'Seoul, Republic of Korea',
    '3158':  'Seoul, Republic of Korea',
    '3159':  'Seoul, Republic of Korea',
    '3162':  'Seoul, Republic of Korea',
    '3163':  'Seoul, Republic of Korea',
    '3321':  'Seoul, Republic of Korea',
    '3322':  'Seoul, Republic of Korea',
    '3473':  'Busan, Republic of Korea',
    '3904':  'Seoul, Republic of Korea',
    '3908':  'Seoul, Republic of Korea',
    '4408':  'Seoul, Republic of Korea',
    '4574':  'Seoul, Republic of Korea',
    '4575':  'Seoul, Republic of Korea',
    '4842':  'Seoul, Republic of Korea',
    '4845':  'Seoul, Republic of Korea',
    '4846':  'Seoul, Republic of Korea',
    '4848':  'Seoul, Republic of Korea',
    '4849':  'Seoul, Republic of Korea',
    '5676':  'Seoul, Republic of Korea',
    '5815':  'Seoul, Republic of Korea',
    '5818':  'Seoul, Republic of Korea',
    '6465':  'Seoul, Republic of Korea',
    '6467':  'Seoul, Republic of Korea',
    '7017':  'Busan, Republic of Korea',
    '7019':  'Busan, Republic of Korea',
    '7022':  'Busan, Republic of Korea',
    '8041':  'Seoul, Republic of Korea',
    '8043':  'Seoul, Republic of Korea',
    '8046':  'Seoul, Republic of Korea',
    '8662':  'Seoul, Republic of Korea',
    '8663':  'Seoul, Republic of Korea',
    '9676':  'Seoul, Republic of Korea',
    '9677':  'Seoul, Republic of Korea',
    '9678':  'Seoul, Republic of Korea',
    '9679':  'Seoul, Republic of Korea',
    '11456': 'Seoul, Republic of Korea',
    '11463': 'Seoul, Republic of Korea',
    '11647': 'Seoul, Republic of Korea',
    '11653': 'Seoul, Republic of Korea',
    '12128': 'Seoul, Republic of Korea',
    '13623': 'Seoul, Republic of Korea',
    '13625': 'Seoul, Republic of Korea',
    '13627': 'Seoul, Republic of Korea',
    # Korea: Republic of Korea rows already correct (2904-2907, 13613, 13617, 13626, etc.) — no patch needed
    # China: PRC variants → China
    '1545':  'Shanghai, China',           # Peoples Republic of China
    '15793': 'Beijing, China',            # People's Republic of China
    '15794': 'Beijing, China',
    '15797': 'Beijing, China',
    '16375': 'Shanghai, China',
    '16380': 'Beijing, China',
    '16381': 'Beijing, China',
    '16385': 'Beijing, China',
    # Netherlands: The Netherlands / the Netherlands → Netherlands
    '103':   'The Hague, Netherlands',
    '2437':  'Maastricht, Netherlands',
    '2978':  'The Hague, Netherlands',
    '4906':  'The Hague, Netherlands',
    '12082': 'The Hague, Netherlands',
    '12083': 'The Hague, Netherlands',
    '12085': 'The Hague, Netherlands',
    '12086': 'The Hague, Netherlands',
    '14894': 'The Hague, Netherlands',
    # Côte d'Ivoire: ASCII variant → proper accented form
    '7113':  "Abidjan, Côte d'Ivoire",
    '7116':  "Abidjan, Côte d'Ivoire",
    '16241': "Abidjan, Côte d'Ivoire",
    '16243': "Abidjan, Côte d'Ivoire",
    # England → United Kingdom
    '13726': 'London, United Kingdom',
    '15804': 'London, United Kingdom',
    # Abbreviations
    '10873': 'Annapolis, Maryland',        # MD
    '11789': 'Abu Dhabi, United Arab Emirates',  # UAE
    '13611': 'Tokyo, Japan',              # TOKYO, JAPAN
    # ── CAT 6: Misspellings ─────────────────────────────────────────────────
    '367':   'Port-au-Prince, Haiti',     # Port-au Prince (missing hyphen)
    '795':   'Sharm el-Sheikh, Egypt',    # Sharm el Sheikh (missing hyphen)
    '892':   'Sharm el-Sheikh, Egypt',    # Sharm el-Sheik (missing final h)
    '9986':  'Port-au-Prince, Haiti',     # Port au Prince (missing both hyphens)
    # ── CAT 7: Andrews AFB wrong name forms (all pre-2009, correct name was
    #           "Andrews Air Force Base"; renamed Joint Base Andrews in 2009)
    '21':    'Andrews Air Force Base, Maryland',  # Joint Andrews Base
    '476':   'Andrews Air Force Base, Maryland',
    '3483':  'Andrews Air Force Base, Maryland',  # Andrews Air Base
    '4215':  'Andrews Air Force Base, Maryland',  # Joint Andrews Base
    '4243':  'Andrews Air Force Base, Maryland',  # Andrews Air Base
    '4744':  'Andrews Air Force Base, Maryland',
    # ── CAT 8: Misc single-row fixes ────────────────────────────────────────
    '4450':  'Salvador da Bahia, Brazil', # Salvador de Bahia (de→da, correct Portuguese)
    '12586': 'Lajes, Portugal',           # "Lajes The Azores, Portugal" — Lajes AFB, Azores (PT)
}

# Path to the latest rubio_backfill output CSV.  Set to '' to skip Pre-pass C.
_SCHEME_WWW_RE = re.compile(r'^https?://(?:www\.)?', re.IGNORECASE)


def _canonical_url_key(url: str) -> str:
    key = (url or '').strip().lower()
    key = _SCHEME_WWW_RE.sub('', key)
    return key.rstrip('/')


RUBIO_BACKFILL_CSV: str = str(BASE_DIR / 'rubio_backfill_fresh.csv')


def read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def write_audit(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def rename_columns(rows: list[dict[str, str]]) -> None:
    """Rename v4.13 column keys to v4.14 schema in-place."""
    for row in rows:
        for old, new in COL_RENAME.items():
            if old in row:
                row[new] = row.pop(old)


def normalize_title(title: str) -> str:
    title = SITE_SUFFIX_RE.sub('', title or '').strip()
    title = DATE_PREFIX_RE.sub('', title).strip()
    title = title.replace(' ', ' ')
    title = re.sub(r'  +', ' ', title)
    return title[:1].upper() + title[1:] if title and title[0].islower() else title


def parse_month_date(month: str, day: str, year: str) -> str:
    return date(int(year), MONTHS[month.lower()], int(day)).isoformat()


def url_year_month(url: str) -> tuple[int, int] | None:
    m = URL_YM_RE.search(url or '')
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def recover_date_from_raw(row: dict[str, str]) -> tuple[str, str]:
    """Return (date, evidence) when Raw Text has a date matching URL year/month."""
    ym = url_year_month(row.get('URL', ''))
    if not ym:
        return '', ''
    old = row.get('Date', '') or ''
    if old.startswith(f'{ym[0]:04d}-{ym[1]:02d}'):
        return '', ''

    raw = row.get('Raw Text', '') or ''
    for m in MONTH_DATE_RE.finditer(raw[:12000]):
        iso = parse_month_date(m.group(1), m.group(2), m.group(3))
        if iso.startswith(f'{ym[0]:04d}-{ym[1]:02d}'):
            s = max(0, m.start() - 180)
            e = min(len(raw), m.end() + 220)
            return iso, raw[s:e].replace('\n', ' | ')
    return '', ''


def infer_speech_type(row: dict[str, str]) -> str:
    title = (row.get('Title') or '').strip()
    st = (row.get('Speech Type') or '').strip().lower().replace('_', ' ')
    url = (row.get('URL') or '').lower()
    text = f'{title} {url}'.lower()

    if 'op-ed' in text or 'op ed' in text:
        return 'op-ed'
    if re.search(r'\b(joint\s+press\s+availability)\b', text):
        return 'joint press availability'
    if re.search(r'\b(press\s+availability|press\s+conference)\b', text):
        return 'press availability'
    if re.search(r'\b(press\s+briefing|special\s+briefing)\b', text):
        return 'press briefing'
    if re.search(r'\bremarks\s+to\s+(?:the\s+)?press\b', text):
        return 'remarks to the press'
    if re.search(r'\b(before\s+the\s+(?:senate|house)|committee|subcommittee|testimony|hearing)\b', text):
        return 'testimony'
    if re.search(r'\binterview\b', text) or re.search(r'\bwith\s+[^/]{2,80}\s+of\s+(?:fox|cnn|nbc|abc|cbs|pbs|bbc|ndtv|newsmax|wftv|ytn|india\s+today|meet\s+the\s+press)', text):
        return 'interview'
    if st == 'press statement' or '/press-statement' in url:
        return 'press statement'
    if title.lower().startswith('statement') or re.search(r'\bstatement\b', st):
        return 'statement'
    if re.search(r'\b(town\s+hall|townhall)\b', text):
        return 'town hall'
    if re.search(r'\b(address|speech)\b', text) or st in {'address', 'speech'}:
        return 'speech'
    if re.search(r'\bremarks?\b', text) or 'remarks' in st:
        return 'remarks'
    return st or 'remarks'


def strip_footer(text: str) -> str:
    out = text or ''
    for pat in FOOTER_RES:
        m = pat.search(out)
        if m and m.start() > 40:
            out = out[:m.start()].rstrip()
    return out.strip()


def strip_leading_headers(text: str) -> str:
    out = text or ''

    # 1997-2001 archive release lines
    prefix = out[:900]
    m = ARCHIVE_RELEASE_RE.search(prefix)
    if m:
        after = out[m.end():]
        dept = DEPT_STATE_RE.search(after[:320])
        if dept:
            out = after[dept.end():].lstrip(' |\n\t')
        else:
            dm = MONTH_DATE_RE.search(after[:320])
            out = after[dm.end():].lstrip(' |\n\t') if dm else after.lstrip(' |\n\t')

    # 2001-2009 archive breadcrumb/header
    if ('Other State Department Archive Sites' in out[:1200]
            or 'You are in:' in out[:1200]
            or 'Former Secretary of State' in out[:1200]):
        nav_end = 0
        for _marker in ('Other State Department Archive Sites', 'You are in:', 'Former Secretary of State'):
            _idx = out[:1200].find(_marker)
            if _idx >= 0:
                nav_end = max(nav_end, _idx + len(_marker))
        first = CONTENT_LABEL_RE.search(out, nav_end)
        if first:
            out = out[first.start():].lstrip(' |\n\t')
        else:
            rel = re.search(
                r'(?is)\s+Released\s+on\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+(?:19|20)\d{2}\b',
                out,
            )
            if rel:
                out = out[:rel.start()].strip()
            if nav_end > 0 and nav_end < len(out):
                out = out[nav_end:].lstrip(' |\n\t').strip()
            elif not rel and out.startswith('Other State Department Archive Sites'):
                out = out[len('Other State Department Archive Sites'):].lstrip(' |\n\t')

    # 2017-2021 archive metadata/page header
    if MODERN_ARCHIVE_HEADER_RE.match(out) or out[:900].lower().count('archived content'):
        m_arch = MODERN_ARCHIVE_HEADER_RE.match(out)
        first = CONTENT_LABEL_RE.search(out)
        music = re.search(r'\[MUSIC PLAYING\]', out, re.I)
        cut = first.start() if first else (music.start() if music else -1)
        if cut > 0:
            out = out[cut:].lstrip(' |\n\t')
        elif m_arch:
            out = out[m_arch.end():].lstrip(' |\n\t').strip()

    # Modern state.gov compact header
    m = LEADING_STATE_HEADER_RE.match(out)
    if m:
        rest = out[m.end():]
        first = CONTENT_LABEL_RE.search(rest)
        if first:
            out = rest[first.start():].lstrip(' |\n\t')
        elif len(rest) > 50:
            out = rest.lstrip(' |\n\t')

    # Duplicated title/metadata block preceding "Share this page on:"
    share = re.search(r'Share\s+this\s+page\s+on:\s*', out[:1200], re.I)
    if share:
        rest = out[share.end():]
        first = CONTENT_LABEL_RE.search(rest)
        if first:
            out = rest[first.start():].lstrip(' |\n\t')

    return out.strip()


def secretary_turns_from_transcript(text: str, secretary: str) -> str:
    """Extract Secretary turns from speaker-labeled transcript text."""
    text = text or ''
    narrow = list(SPEAKER_LABEL_RE.finditer(text))
    if not narrow:
        return ''
    narrow_starts = {m.start() for m in narrow}
    broad_extra = [m for m in _BROAD_LABEL_RE.finditer(text) if m.start() not in narrow_starts]
    all_labels = sorted(narrow + broad_extra, key=lambda m: m.start())
    lname = LASTNAME.get(secretary, '').upper()
    turns: list[str] = []
    for i, m in enumerate(all_labels):
        if m.start() not in narrow_starts:
            continue
        label = m.group(0).upper()
        is_sec = (
            'SECRETARY' in label
            and not _DEPUTY_TITLE_RE.match(label)
            and (
                not lname or lname in label or
                label.strip().startswith('SECRETARY:') or
                re.match(r'SECRETARY\s+OF\s+STATE\s*:', label.strip()) is not None
            )
        )
        if not is_sec:
            continue
        end = all_labels[i + 1].start() if i + 1 < len(all_labels) else len(text)
        turn = text[m.end():end].strip(' |\n\t')
        if len(turn) >= 10:
            turns.append(turn)
    return '\n\n'.join(turns).strip()


_GARBLED_SECRETARY_RE = re.compile(r'\bS\s+ECRETARY\b', re.IGNORECASE)


def strip_return_home(text: str) -> str:
    """Strip 'Return to X Home Page' and everything after, trimming (###) separators."""
    m = RETURN_HOME_CORE_RE.search(text)
    if not m:
        return text
    cut = text[:m.start()].rstrip()
    # Also strip a trailing (###) or ### doc-end separator if present
    for sep in ('(###)', '###'):
        if cut.endswith(sep):
            cut = cut[:-len(sep)].rstrip()
            break
    return cut


# Pass 9: state.gov navigation sidebar (Kerry/Clinton, position-gated to second half)
_STATE_NAV_ANCHOR_RES = [
    # v4.15 anchors (consular/public-diplomacy sidebar section)
    re.compile(r'(?i)\bExchange\s+Visitor\s+Program\b'),
    re.compile(r'(?i)\bU\.S\.\s+Diplomacy\s+Center\b'),
    re.compile(r'(?i)\bIntercountry\s+Adoption\s+Parental\b'),
    re.compile(r'(?i)\bPer\s+Diem\s+Rates\b'),
    # v4.16 anchors (careers/business sidebar section — Kerry/Clinton)
    # Note: "In This Section: Go Back" moved to FOOTER_RES (v4.20) — it appears before the
    # document midpoint in 59 rows, so the half-gate here caused those rows to be missed.
    re.compile(r'(?i)\bUSAJobs\b'),
]


def strip_state_nav_footer(text: str) -> str:
    """Strip state.gov sidebar nav block from second half of document only."""
    half = max(len(text) // 2, 100)
    earliest = None
    for pat in _STATE_NAV_ANCHOR_RES:
        m = pat.search(text, half)
        if m and (earliest is None or m.start() < earliest):
            earliest = m.start()
    if earliest is not None and earliest > 100:
        return text[:earliest].rstrip()
    return text


def strip_leading_title(text: str, title: str) -> str:
    """Strip leading lines that duplicate the row's Title field.

    Only applies to multi-line texts where the title appears as a separate
    header line before the speech content begins.
    """
    if not title or not text:
        return text
    lines = text.splitlines(keepends=True)
    if len(lines) < 2:
        # Single-paragraph text: title is embedded in the content, not a header
        return text
    title_norm = re.sub(r'\s+', ' ', title.lower().strip())
    if len(title_norm) < 15:
        return text
    # Accumulate first 1-4 lines and check similarity to the title
    accumulated = ''
    last_i = 0
    for i, line in enumerate(lines[:4]):
        accumulated += re.sub(r'\s+', ' ', line.lower())
        last_i = i
        if len(accumulated) >= len(title_norm) * 0.9:
            break
    ratio = difflib.SequenceMatcher(
        None, title_norm, accumulated.strip()[:len(title_norm)]
    ).ratio()
    if ratio >= 0.85:
        remaining = ''.join(lines[last_i + 1:]).lstrip()
        if remaining.strip():
            return remaining
    return text


def strip_boilerplate(text: str, title: str = '') -> str:
    """Apply ten passes to remove web-scraping artifacts from extracted text."""
    if not text:
        return text

    # Pass 1: doc-end markers and everything after
    text = DOC_END_RE.sub('', text).rstrip()

    # Pass 1b: inline [End of Document] at text end (Albright era, no preceding newline)
    text = DOC_END_INLINE_RE.sub('', text).rstrip()

    # Pass 1c: inline spaced-hash separator (Clinton: # # # PRN: YYYY/NNN inline)
    text = SPACED_HASH_INLINE_RE.sub('', text).rstrip()

    # Pass 2: "Return to Home Page" navigation — cut at the phrase, preserve content before it
    text = strip_return_home(text).strip()

    # Pass 3: leading press-release headers
    text = PRESS_HEADER_RE.sub('', text).lstrip()

    # Pass 4: leading title duplication
    if title:
        text = strip_leading_title(text, title)

    # Pass 5: navigation sidebar blocks (In This Section, Related Topics, etc.)
    text = NAV_BLOCK_RE.sub('', text).strip()

    # Pass 6: standalone click/learn-more anchor lines
    text = STANDALONE_NAV_RE.sub('', text).strip()

    # Pass 7: trailing bare URLs
    text = TRAILING_URL_RE.sub('', text).rstrip()

    # Pass 8: 2009-2017 archive notice
    m = ARCHIVE_2009_RE.search(text)
    if m and m.start() > 40:
        text = text[:m.start()].rstrip()

    # Pass 9: state.gov navigation sidebar footer
    text = strip_state_nav_footer(text)

    # Pass 10: trailing bare press-release number (YYYY/NNN)
    m = DOC_NUMBER_RE.search(text)
    if m and m.start() > 40:
        text = text[:m.start()].rstrip()

    return text


def strip_translation_links(text: str) -> str:
    """Remove a translation-link header at the start of the text (v2026.10.02 patch)."""
    if not text:
        return text
    stripped = _LEADING_TRANSLATION_LINKS_RE.sub('', text, count=1)
    return stripped if stripped.strip() else text


def strip_updates_nav(text: str) -> str:
    """Truncate at the Powell-era 'Updates | Frequent Questions | Contact Us' nav footer."""
    m = _UPDATES_NAV_RE.search(text)
    if m:
        text = text[:m.start()].rstrip()
    return text


def strip_released_on(text: str) -> str:
    """Remove inline 'Released on [Month DD, YYYY]' press-release date stamps."""
    return _RELEASED_ON_RE.sub('', text).strip()


def strip_boundary_doc_codes(text: str) -> str:
    """Remove YYYY/NNN doc reference codes in the first/last 200 characters only."""
    if not text:
        return text
    if len(text) <= 400:
        return _BOUNDARY_DOC_CODE_RE.sub('', text).strip()
    head = _BOUNDARY_DOC_CODE_RE.sub('', text[:200]).lstrip()
    tail = _BOUNDARY_DOC_CODE_RE.sub('', text[-200:]).rstrip()
    return head + text[200:-200] + tail


def strip_stage_directions(text: str, rubio_path: bool = False) -> str:
    """Strip audience reactions, inaudible markers, delivery mode annotations, and
    ceremony stage directions from SOS Text only.

    rubio_path=True also strips "(Via translation)"-type markers (audit X-09); the
    default leaves every other secretary's output unchanged.

    Guard: if stripping would leave fewer than 5 words, return original text unchanged.
    This protects rows like Row 4833 where the stage direction IS the entire content
    (e.g., '(Speaking in Korean.)' is the only published Secretary utterance)."""
    if not text:
        return text
    result = text
    pats = (_STAGE_DIR_AUDIENCE_RE, _STAGE_DIR_INAUDIBLE_RE,
            _STAGE_DIR_LANGUAGE_RE, _STAGE_DIR_TRANSCRIPT_RE,
            _STAGE_DIR_CEREMONY_RE)
    if rubio_path:
        pats += (_STAGE_DIR_VIA_TRANSLATION_RE,)
    for pat in pats:
        result = pat.sub('', result)
    result = re.sub(r'[ \t]{2,}', ' ', result).strip()
    if len(result.split()) < 5:
        return text
    return result


def clean_text_fields(row: dict[str, str]) -> tuple[bool, str, str, list[dict]]:
    pre_speech  = row.get('SOS Text', '') or ''
    pre_speaker = row.get('Natural Text', '') or ''
    secretary = row.get('Secretary', '') or ''
    title = row.get('Title', '') or ''
    rid = row.get('Row Id', '')

    # Encoding normalization — runs before all other passes
    enc_audit: list[dict] = []
    old_speech  = normalize_encoding(pre_speech)
    old_speaker = normalize_encoding(pre_speaker)
    if old_speech != pre_speech:
        enc_audit.append({'Row Id': rid, 'Secretary': secretary, 'field': 'SOS Text',
                          'sample_before': pre_speech[:200].replace('\n', ' | '),
                          'sample_after':  old_speech[:200].replace('\n', ' | ')})
    if old_speaker != pre_speaker:
        enc_audit.append({'Row Id': rid, 'Secretary': secretary, 'field': 'Natural Text',
                          'sample_before': pre_speaker[:200].replace('\n', ' | '),
                          'sample_after':  old_speaker[:200].replace('\n', ' | ')})
    row['SOS Text']     = old_speech
    row['Natural Text'] = old_speaker

    # Normalize garbled "S ECRETARY" → "SECRETARY"
    old_speech  = _GARBLED_SECRETARY_RE.sub('SECRETARY', old_speech)
    old_speaker = _GARBLED_SECRETARY_RE.sub('SECRETARY', old_speaker)

    speaker = strip_footer(strip_leading_headers(old_speaker))
    speech = strip_footer(strip_leading_headers(old_speech))

    if SPEAKER_LABEL_RE.search(speech) or speech.lstrip().upper().startswith('QUESTION:') or _BROAD_LABEL_RE.search(speech):
        extracted = secretary_turns_from_transcript(speaker or speech, secretary)
        if extracted:
            speech = extracted
        else:
            speech = re.sub(r'(?i)(?<![A-Za-z])SECRETARY(?:\s+[A-Z .\'\-]+)?\s*\|?\s*:\s*', '', speech).strip()

    speech = strip_footer(speech)
    speaker = strip_footer(speaker)
    speech = strip_translation_links(speech)
    speaker = strip_translation_links(speaker)

    # Apply seven boilerplate stripping passes
    speech = strip_boilerplate(speech, title)
    speaker = strip_boilerplate(speaker, title)

    # Passes 11–13: additional artifact stripping
    speech = strip_updates_nav(speech)
    speaker = strip_updates_nav(speaker)
    speech = strip_released_on(speech)
    speaker = strip_released_on(speaker)
    speech = strip_boundary_doc_codes(speech)
    speaker = strip_boundary_doc_codes(speaker)

    # Pass 14: stage direction stripping — SOS Text only, not Natural Text
    speech = strip_stage_directions(speech)

    # Natural Text recovery: detect rows where cleaning left Natural Text as
    # boilerplate while SOS Text has real content (nat_wc < sos_wc × 0.5 and
    # nat_wc < 50).  Repopulate Natural Text from Raw Text in these cases.
    nat_wc = len(speaker.split()) if speaker else 0
    sos_wc = len(speech.split()) if speech else 0
    if nat_wc < 50 and sos_wc > 0 and nat_wc < sos_wc * 0.5:
        raw = (row.get('Raw Text', '') or '').strip()
        if raw and len(raw.split()) > nat_wc * 2:
            recovered = strip_boilerplate(
                strip_footer(strip_leading_headers(normalize_encoding(raw))),
                title,
            )
            if len(recovered.split()) > nat_wc and not _RECOVERY_NAV_GUARD_RE.search(recovered[:400]):
                enc_audit.append({
                    'Row Id': rid,
                    'Secretary': secretary,
                    'field': 'Natural Text',
                    'sample_before': speaker[:200].replace('\n', ' | '),
                    'sample_after':  recovered[:200].replace('\n', ' | '),
                })
                speaker = recovered

    changed = (speech != pre_speech) or (speaker != pre_speaker)
    row['SOS Text'] = speech
    row['Natural Text'] = speaker
    return changed, pre_speech[:300].replace('\n', ' | '), speech[:300].replace('\n', ' | '), enc_audit


def make_coverage_audit(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    by_sec: dict[str, list[tuple[date, str, str]]] = defaultdict(list)
    for r in rows:
        try:
            by_sec[r['Secretary']].append((date.fromisoformat(r['Date']), r['Row Id'], r['Title']))
        except Exception:
            pass
    out: list[dict[str, str]] = []
    for sec, vals in sorted(by_sec.items()):
        vals.sort()
        by_date = defaultdict(list)
        for d, rid, title in vals:
            by_date[d].append((rid, title))
        dates = sorted(by_date)
        for a, b in zip(dates, dates[1:]):
            gap = (b - a).days
            if gap >= 30:
                out.append({
                    'Secretary': sec,
                    'gap_days': str(gap),
                    'gap_start_date': a.isoformat(),
                    'gap_end_date': b.isoformat(),
                    'row_ids_before': ';'.join(rid for rid, _ in by_date[a][:5]),
                    'titles_before': ' | '.join(t for _, t in by_date[a][:3]),
                    'row_ids_after': ';'.join(rid for rid, _ in by_date[b][:5]),
                    'titles_after': ' | '.join(t for _, t in by_date[b][:3]),
                })
    return out


def _is_no_secretary_turn(row: dict[str, str]) -> bool:
    """True when SOS Text still opens with a non-secretary speaker label after
    clean_text_fields() ran and secretary_turns_from_transcript() found nothing."""
    speech = row.get('SOS Text', '') or ''
    secretary = row.get('Secretary', '') or ''
    lname = LASTNAME.get(secretary, '').upper()

    m = SPEAKER_LABEL_RE.match(speech.lstrip())
    if not m:
        m = _BROAD_LABEL_RE.match(speech.lstrip())
        if not m:
            return False

    label = m.group(0).upper()
    if 'SECRETARY' in label and (not lname or lname in label or
            re.match(r'SECRETARY\s+OF\s+STATE\s*:', label.strip()) is not None):
        return False
    turns = secretary_turns_from_transcript(
        row.get('Natural Text', '') or speech, secretary
    )
    return not turns.strip()


def check_field_coherence(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Flag rows where SOS Text and Natural Text are suspiciously misaligned.
    Rows in COHERENCE_WHITELIST are confirmed-legitimate short utterances and are skipped."""
    flags = []
    for r in rows:
        if r.get('Row Id', '') in COHERENCE_WHITELIST:
            continue
        sos    = set((r.get('SOS Text', '') or '').lower().split())
        nat    = set((r.get('Natural Text', '') or '').lower().split())
        sos_wc = len(sos)
        jaccard = len(sos & nat) / len(sos | nat) if (sos | nat) else 0.0
        if sos_wc < 30 or jaccard < 0.1:
            flags.append({
                'Row Id':         r['Row Id'],
                'Secretary':      r['Secretary'],
                'Speech Type':    r['Speech Type'],
                'sos_word_count': sos_wc,
                'nat_word_count': len(nat),
                'jaccard':        round(jaccard, 4),
                'flag':           'short_sos' if sos_wc < 30 else 'low_overlap',
                'Title':          r.get('Title', ''),
            })
    return flags


def _shingles(text: str, n: int = 3) -> list[int]:
    words = (text or '').lower().split()
    return [hash(tuple(words[i:i + n])) for i in range(max(0, len(words) - n + 1))]


def _minhash(shingle_hashes: list[int]) -> list[int]:
    if not shingle_hashes:
        return [_LARGE_PRIME] * 64
    sig = []
    for a, b in _MH_PARAMS:
        sig.append(min((a * h + b) % _LARGE_PRIME for h in shingle_hashes))
    return sig


def _jaccard_exact(text_a: str, text_b: str) -> float:
    a = set(_shingles(text_a))
    b = set(_shingles(text_b))
    if not (a | b):
        return 0.0
    return len(a & b) / len(a | b)


def find_near_duplicates(
    rows: list[dict[str, str]], threshold: float = 0.85
) -> list[dict[str, str]]:
    """LSH near-duplicate detection on SOS Text, grouped by Secretary."""
    results = []
    by_sec: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_sec[r['Secretary']].append(r)

    for sec, grp in by_sec.items():
        sigs: dict[str, list[int]] = {}
        for r in grp:
            sh = _shingles(r.get('SOS Text', '') or '')
            sigs[r['Row Id']] = _minhash(sh)

        buckets: dict[tuple, list[str]] = defaultdict(list)
        for rid, sig in sigs.items():
            for band in range(_MH_BANDS):
                key = (band, tuple(sig[band * _MH_ROWS:(band + 1) * _MH_ROWS]))
                buckets[key].append(rid)

        candidates: set[tuple[str, str]] = set()
        for bucket in buckets.values():
            if len(bucket) > 1:
                for i in range(len(bucket)):
                    for j in range(i + 1, len(bucket)):
                        a_id, b_id = bucket[i], bucket[j]
                        pair = (a_id, b_id) if int(a_id) < int(b_id) else (b_id, a_id)
                        candidates.add(pair)

        row_by_id = {r['Row Id']: r for r in grp}
        for rid_a, rid_b in candidates:
            ra, rb = row_by_id[rid_a], row_by_id[rid_b]
            jac = _jaccard_exact(ra.get('SOS Text', ''), rb.get('SOS Text', ''))
            if jac >= threshold:
                results.append({
                    'Secretary': sec,
                    'Row Id A':  rid_a,
                    'Row Id B':  rid_b,
                    'Jaccard':   round(jac, 4),
                    'Title A':   ra.get('Title', ''),
                    'Title B':   rb.get('Title', ''),
                    'URL A':     ra.get('URL', ''),
                    'URL B':     rb.get('URL', ''),
                })
    results.sort(key=lambda r: -r['Jaccard'])
    return results


def check_length_outliers(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Flag rows with suspiciously short or extreme-length text fields."""
    groups: dict[tuple, list[int]] = defaultdict(list)
    wc_cache: dict[str, tuple[int, int]] = {}
    for r in rows:
        sos_wc = len((r.get('SOS Text', '') or '').split())
        nat_wc = len((r.get('Natural Text', '') or '').split())
        wc_cache[r['Row Id']] = (sos_wc, nat_wc)
        groups[(r['Secretary'], r['Speech Type'])].append(sos_wc)

    p95: dict[tuple, float] = {}
    for key, wcs in groups.items():
        sorted_wcs = sorted(wcs)
        idx = int(0.95 * len(sorted_wcs))
        p95[key] = sorted_wcs[min(idx, len(sorted_wcs) - 1)]

    flags = []
    for r in rows:
        sos_wc, nat_wc = wc_cache[r['Row Id']]
        key = (r['Secretary'], r['Speech Type'])
        reasons = []
        if sos_wc < 50:
            reasons.append(f'sos_wc={sos_wc}<50')
        if nat_wc < 50:
            reasons.append(f'nat_wc={nat_wc}<50')
        if sos_wc > p95.get(key, float('inf')):
            reasons.append(f'sos_wc={sos_wc}>P95({p95[key]:.0f})')
        if reasons:
            flags.append({
                'Row Id':      r['Row Id'],
                'Secretary':   r['Secretary'],
                'Speech Type': r['Speech Type'],
                'sos_wc':      sos_wc,
                'nat_wc':      nat_wc,
                'p95_sos_wc':  round(p95.get(key, 0)),
                'flags':       '; '.join(reasons),
                'Title':       r.get('Title', ''),
                'URL':         r.get('URL', ''),
            })
    return flags


def write_distribution_report(
    prefix: Path,
    counters: dict[str, Counter],
    year_counter: Counter,
    wc_lists: dict[str, list[int]],
) -> None:
    """Write column-by-column value counts and text stats for manual QA and regression."""
    from datetime import date as _date_cls

    def _pct_stats(values: list[int]) -> dict:
        if not values:
            return {}
        s = sorted(values)
        n = len(s)
        def _p(p: float) -> int:
            return s[max(0, int(n * p) - 1)]
        total = sum(s)
        return {
            'count': n, 'min': s[0], 'p5': _p(0.05), 'p25': _p(0.25),
            'median': s[n // 2], 'p75': _p(0.75), 'p95': _p(0.95),
            'max': s[-1], 'mean': round(total / n, 1),
        }

    text_stats = {col: _pct_stats(wc_lists[col]) for col in wc_lists}
    n_total = sum(counters['Secretary'].values())

    payload = {
        'meta': {
            'version':   prefix.name,
            'generated': str(_date_cls.today()),
            'row_count': n_total,
        },
        **counters,
        'Date_by_year': year_counter,
        **{col + '_word_counts': text_stats[col] for col in text_stats},
    }
    pkl_path = prefix.with_name(prefix.name + '_distribution.pkl')
    with open(pkl_path, 'wb') as fh:
        pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)

    txt_path = prefix.with_name(prefix.name + '_distribution.txt')
    sep = '-' * 60
    lines: list[str] = [
        f'=== Corpus Distribution Report: {prefix.name}  ({_date_cls.today()}) ===',
        f'Total rows: {n_total:,}',
        '',
    ]

    for col in ['Secretary', 'Speech Type', 'Location']:
        cnt = counters[col]
        lines += [sep, f'{col}  ({len(cnt)} unique values)', '']
        for val, c in cnt.most_common():
            pct = 100.0 * c / n_total if n_total else 0
            lines.append(f'  {c:6,}  {pct:5.1f}%   {val}')
        lines.append('')

    lines += [sep, 'Date by Year', '']
    for yr, c in sorted(year_counter.items()):
        lines.append(f'  {yr}   {c:,}')
    lines.append('')

    for col in wc_lists:
        s = text_stats[col]
        lines += [sep, f'{col} — word counts', '']
        for label, key in [
            ('count', 'count'), ('min', 'min'), ('p5', 'p5'), ('p25', 'p25'),
            ('median', 'median'), ('p75', 'p75'), ('p95', 'p95'), ('max', 'max'), ('mean', 'mean'),
        ]:
            lines.append(f'  {label:<8}  {s.get(key, "n/a")}')
        lines.append('')

    with open(txt_path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines))
    print(f'Distribution report: {txt_path}')
    print(f'Distribution pickle: {pkl_path}')


def main() -> None:
    default_input = BASE_DIR / 'secretaryofstate_speeches_final.fused.v44.patched.enriched.postprocessed.v4.29.csv'
    default_output = BASE_DIR / 'secretaryofstate_speeches_final.fused.v44.patched.enriched.postprocessed.v4.30.csv'
    ap = argparse.ArgumentParser(description='Research-grade v4.30 finalization')
    ap.add_argument('--input', default=str(default_input))
    ap.add_argument('--output', default=str(default_output))
    ap.add_argument('--audit-prefix', default=str(BASE_DIR / 'v4_30'))
    args = ap.parse_args()

    rows = read_csv(Path(args.input))

    # Rename column keys from v4.13 → v4.14 schema before all processing
    rename_columns(rows)

    date_audit: list[dict[str, str]] = []
    text_audit: list[dict[str, str]] = []
    type_audit: list[dict[str, str]] = []
    sec_audit: list[dict[str, str]] = []
    encoding_audit: list[dict[str, str]] = []
    no_turn_rows: list[dict[str, str]] = []
    rows_to_exclude: set[str] = set()

    # Pre-pass A: re-fetch alert-shell rows
    try:
        import sys
        sys.path.insert(0, str(BASE_DIR))
        from rubio_backfill import fetch_rubio_page, extract_row as _extract_rubio_row
        _session = requests.Session()
        for row in rows:
            if row.get('Row Id') not in ALERT_SHELL_ROWS:
                continue
            page = fetch_rubio_page(row['URL'], _session)
            if page is None:
                print(f"  WARN: re-fetch failed for alert-shell row {row['Row Id']}")
                continue
            extracted = _extract_rubio_row(row['URL'], page)
            if extracted is None:
                print(f"  WARN: extraction failed for alert-shell row {row['Row Id']}")
                continue
            row['Raw Text']    = page['raw_text']
            row['SOS Text']    = extracted['speech_text']
            row['Natural Text'] = extracted['speaker_text']
            print(f"  [alert-shell refetch] row {row['Row Id']}: {row.get('Title', '')[:60]}")
    except Exception as exc:
        print(f"  WARN: alert-shell refetch unavailable ({exc})")

    # Pre-pass A-bis: re-fetch Clinton rows with corrected URLs via Wayback Machine
    try:
        import sys
        sys.path.insert(0, str(BASE_DIR))
        from corpus_refetch_blinken_wayback import _find_wayback_snapshot, _fetch_wayback
        from corpus_fusion import extract_state_speech_body
        from hybrid_corpus_recovery import extract_secretary_only_text as _extract_sec_only
        _wb_session = requests.Session()
        for row in rows:
            if row.get('Row Id') not in CLINTON_WAYBACK_REFETCH:
                continue
            correct_url = CLINTON_WAYBACK_REFETCH[row['Row Id']]
            row['URL'] = correct_url
            ts = _find_wayback_snapshot(_wb_session, correct_url)
            if ts is None:
                print(f"  WARN: no Wayback snapshot for row {row['Row Id']}")
                continue
            html_content, status = _fetch_wayback(_wb_session, correct_url, ts)
            if not html_content:
                print(f"  WARN: Wayback fetch failed (HTTP {status}) for row {row['Row Id']}")
                continue
            body = extract_state_speech_body(html_content, correct_url, row['Secretary'])
            speaker_text = body.get('raw_text', '') or ''
            # Strip archive navigation header before the first speaker label
            _first = CONTENT_LABEL_RE.search(speaker_text)
            if _first and _first.start() < 2000:
                speaker_text = speaker_text[_first.start():]
            speech_text, _ = _extract_sec_only(speaker_text, row['Secretary'])
            row['Raw Text']      = speaker_text
            row['Natural Text']  = speaker_text
            row['SOS Text']      = speech_text or speaker_text
            print(f"  [wayback refetch] row {row['Row Id']}: {row.get('Title', '')[:60]}")
    except Exception as exc:
        print(f"  WARN: Clinton Wayback refetch unavailable ({exc})")

    # Pre-pass B: drop confirmed duplicate rows
    rows = [r for r in rows if r.get('Row Id', '') not in ROWS_TO_DROP]

    # Pre-pass C: ingest fresh Rubio backfill rows (one-time merge)
    rubio_merge_count = 0
    if RUBIO_BACKFILL_CSV and Path(RUBIO_BACKFILL_CSV).exists():
        existing_urls = {_canonical_url_key(r.get('URL', '')) for r in rows}
        max_rid = max(int(r.get('Row Id', 0)) for r in rows)
        new_rubio: list[dict[str, str]] = []
        with open(RUBIO_BACKFILL_CSV, newline='', encoding='utf-8') as _bf:
            for rec in csv.DictReader(_bf):
                ckey = _canonical_url_key(rec.get('url', ''))
                if not ckey or ckey in existing_urls:
                    continue
                existing_urls.add(ckey)
                max_rid += 1
                st = SPEECH_TYPE_TITLECASE.get(
                    (rec.get('speech_type', '') or 'remarks').strip().lower(),
                    'Remarks',
                )
                new_rubio.append({
                    'Row Id':       str(max_rid),
                    'Secretary':    'Rubio',
                    'Date':         rec.get('Date', ''),
                    'Title':        rec.get('Title', ''),
                    'Speech Type':  st,
                    'Location':     rec.get('Location', 'Washington, D.C.') or 'Washington, D.C.',
                    'URL':          rec.get('url', ''),
                    'SOS Text':     rec.get('speech_text', ''),
                    'Natural Text': rec.get('speaker_text', ''),
                    'Raw Text':     rec.get('raw_text', ''),
                })
        rows.extend(new_rubio)
        rubio_merge_count = len(new_rubio)
        print(f'Pre-pass C: merged {rubio_merge_count:,} new Rubio rows (backfill)')
    elif RUBIO_BACKFILL_CSV:
        print(f'  WARN: RUBIO_BACKFILL_CSV not found: {RUBIO_BACKFILL_CSV}')

    for row in rows:
        rid = row.get('Row Id', '')

        if rid in TITLE_PATCHES:
            row['Title'] = TITLE_PATCHES[rid]

        if rid in LOCATION_PATCHES:
            row['Location'] = LOCATION_PATCHES[rid]

        row['Title']    = ftfy.fix_text(row['Title'])    if row.get('Title')    else row.get('Title', '')
        row['Location'] = ftfy.fix_text(row['Location']) if row.get('Location') else row.get('Location', '')

        if rid in COPY_NAT_TO_SOS:
            row['SOS Text'] = row.get('Natural Text', '') or ''

        if rid in SOS_TEXT_PATCHES:
            row['SOS Text'] = SOS_TEXT_PATCHES[rid]

        if rid in COPY_SOS_TO_NAT:
            row['Natural Text'] = row.get('SOS Text', '') or ''

        if rid in SECRETARY_PATCHES:
            row['Secretary'] = SECRETARY_PATCHES[rid]

        old_sec = row.get('Secretary', '')
        canon = SECRETARY_CANON.get(old_sec.strip().lower(), old_sec.strip())
        if canon != old_sec:
            row['Secretary'] = canon
            sec_audit.append({'Row Id': rid, 'old_secretary': old_sec, 'new_secretary': canon})

        old_title = row.get('Title', '')
        row['Title'] = normalize_title(old_title)

        recovered_date, evidence = recover_date_from_raw(row)
        if recovered_date and recovered_date != row.get('Date', ''):
            old_date = row.get('Date', '')
            row['Date'] = recovered_date
            date_audit.append({
                'Row Id': rid,
                'old_date': old_date,
                'new_date': recovered_date,
                'URL': row.get('URL', ''),
                'Title': row.get('Title', ''),
                'evidence': evidence,
            })

        old_type_raw = row.get('Speech Type', '')
        new_type_lower = infer_speech_type(row)
        new_type = SPEECH_TYPE_TITLECASE.get(new_type_lower, new_type_lower.title())
        row['Speech Type'] = new_type
        if rid in SPEECH_TYPE_PATCHES:
            row['Speech Type'] = SPEECH_TYPE_PATCHES[rid]
        if row['Speech Type'] != old_type_raw:
            type_audit.append({
                'Row Id': rid,
                'old_speech_type': old_type_raw,
                'new_speech_type': new_type,
                'Title': row.get('Title', ''),
                'URL': row.get('URL', ''),
            })

        changed, before, after, enc_entries = clean_text_fields(row)
        encoding_audit.extend(enc_entries)
        if changed:
            text_audit.append({
                'Row Id': rid,
                'Secretary': row.get('Secretary', ''),
                'Title': row.get('Title', ''),
                'before_start': before,
                'after_start': after,
            })

        if _is_no_secretary_turn(row):
            no_turn_rows.append({
                'Row Id': rid,
                'Secretary': row.get('Secretary', ''),
                'Title': row.get('Title', ''),
                'URL': row.get('URL', ''),
                'sos_text_start': row.get('SOS Text', '')[:300].replace('\n', ' | '),
            })
            rows_to_exclude.add(rid)

    output_rows = [r for r in rows if r.get('Row Id', '') not in rows_to_exclude]
    for i, r in enumerate(output_rows):
        r['Row Id'] = str(i)

    write_csv(Path(args.output), output_rows)

    # Distribution accumulators — built from output_rows (post-filter, post-renumber)
    _DIST_CAT_COLS = ['Secretary', 'Speech Type', 'Location']
    dist_counters: dict[str, Counter] = {col: Counter() for col in _DIST_CAT_COLS}
    dist_year: Counter = Counter()
    dist_sos_wc: list[int] = []
    dist_nat_wc: list[int] = []
    for _row in output_rows:
        for _col in _DIST_CAT_COLS:
            dist_counters[_col][_row.get(_col, '')] += 1
        dist_year[_row.get('Date', '')[:4]] += 1
        dist_sos_wc.append(len((_row.get('SOS Text') or '').split()))
        dist_nat_wc.append(len((_row.get('Natural Text') or '').split()))

    # Post-processing diagnostics (audit-only, no rows dropped)
    print('Running post-processing diagnostics ...')
    coherence_flags = check_field_coherence(output_rows)
    dup_pairs       = find_near_duplicates(output_rows)
    length_flags    = check_length_outliers(output_rows)

    prefix = Path(args.audit_prefix)
    write_audit(prefix.with_name(prefix.name + '_secretary_audit.csv'), sec_audit,
                ['Row Id', 'old_secretary', 'new_secretary'])
    write_audit(prefix.with_name(prefix.name + '_date_patch_audit.csv'), date_audit,
                ['Row Id', 'old_date', 'new_date', 'URL', 'Title', 'evidence'])
    write_audit(prefix.with_name(prefix.name + '_text_cleanup_audit.csv'), text_audit,
                ['Row Id', 'Secretary', 'Title', 'before_start', 'after_start'])
    write_audit(prefix.with_name(prefix.name + '_speech_type_audit.csv'), type_audit,
                ['Row Id', 'old_speech_type', 'new_speech_type', 'Title', 'URL'])
    write_audit(prefix.with_name(prefix.name + '_coverage_gap_audit.csv'), make_coverage_audit(output_rows),
                ['Secretary', 'gap_days', 'gap_start_date', 'gap_end_date', 'row_ids_before', 'titles_before', 'row_ids_after', 'titles_after'])
    write_audit(prefix.with_name(prefix.name + '_no_turn_audit.csv'), no_turn_rows,
                ['Row Id', 'Secretary', 'Title', 'URL', 'sos_text_start'])
    write_audit(prefix.with_name(prefix.name + '_encoding_audit.csv'), encoding_audit,
                ['Row Id', 'Secretary', 'field', 'sample_before', 'sample_after'])
    write_audit(prefix.with_name(prefix.name + '_coherence_audit.csv'), coherence_flags,
                ['Row Id', 'Secretary', 'Speech Type', 'sos_word_count', 'nat_word_count', 'jaccard', 'flag', 'Title'])
    write_audit(prefix.with_name(prefix.name + '_duplicate_audit.csv'), dup_pairs,
                ['Secretary', 'Row Id A', 'Row Id B', 'Jaccard', 'Title A', 'Title B', 'URL A', 'URL B'])
    write_audit(prefix.with_name(prefix.name + '_length_audit.csv'), length_flags,
                ['Row Id', 'Secretary', 'Speech Type', 'sos_wc', 'nat_wc', 'p95_sos_wc', 'flags', 'Title', 'URL'])

    write_distribution_report(
        prefix,
        dist_counters,
        dist_year,
        {'SOS Text': dist_sos_wc, 'Natural Text': dist_nat_wc},
    )

    print(f'Read {len(rows) + len(ROWS_TO_DROP) - rubio_merge_count:,} rows (+ {rubio_merge_count:,} new Rubio backfill)')
    print(f'Dropped duplicates:    {len(ROWS_TO_DROP):,}')
    print(f'No-secretary-turn:     {len(no_turn_rows):,} (excluded)')
    print(f'Output rows:           {len(output_rows):,}')
    print(f'Wrote {args.output}')
    print(f'Secretary corrections: {len(sec_audit):,}')
    print(f'Date corrections:      {len(date_audit):,}')
    print(f'Text cleanups:         {len(text_audit):,}')
    print(f'Speech type changes:   {len(type_audit):,}')
    print(f'Encoding normalizations: {len(encoding_audit):,}')
    print(f'Coherence flags:         {len(coherence_flags):,}')
    print(f'Near-duplicate pairs:    {len(dup_pairs):,}')
    print(f'Length outliers:         {len(length_flags):,}')


if __name__ == '__main__':
    main()
