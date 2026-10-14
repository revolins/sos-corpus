from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

try:
    from tqdm import tqdm
except Exception:  # pragma: no cover
    def tqdm(iterable=None, **kwargs):
        return iterable

# Reuse existing appointment windows + metadata date parsing logic.
try:
    from filter_certain_secstate_speakers import APPOINTMENT_WINDOWS, extract_metadata_best_date, parse_date_inferred_value
except ModuleNotFoundError:  # pragma: no cover
    sys.path.append(str(Path(__file__).resolve().parent))
    from filter_certain_secstate_speakers import APPOINTMENT_WINDOWS, extract_metadata_best_date, parse_date_inferred_value


# Working directory for the one-off corpus build's intermediate files.
# Override with SOS_WORK_DIR; every path below can also be set by its CLI flag.
BASE_DIR = Path(os.environ.get('SOS_WORK_DIR', 'work'))

DEFAULT_SMALL_CORPUS = BASE_DIR / 'secstate_naive_corpus.with_exact_date.csv'
DEFAULT_LARGE_CORPUS = BASE_DIR / 'secstate_naive_corpus.csv'
DEFAULT_INPUT_FINAL = BASE_DIR / 'secretaryofstate_speeches_final.with_urls.seqmatcher_only.csv'
DEFAULT_INPUT_AUDIT = BASE_DIR / 'url_match_audit.seqmatcher_only.csv'

DEFAULT_DB = BASE_DIR / 'hybrid_corpus_recovery.state.sqlite'
DEFAULT_RECOVERY_RESULTS = BASE_DIR / 'url_match_recovery_candidates.hybrid.csv'
DEFAULT_RECOVERY_RESULTS_V2 = BASE_DIR / 'url_match_recovery_candidates.hybrid.v2.csv'
DEFAULT_TRIAGE_OUT_V2 = BASE_DIR / 'url_match_triage.hybrid.v2.csv'
DEFAULT_MINED = BASE_DIR / 'secretaryofstate_speeches_mined.hybrid.csv'
DEFAULT_FINAL_OUT = BASE_DIR / 'secretaryofstate_speeches_final.with_urls.hybrid.csv'
DEFAULT_AUDIT_OUT = BASE_DIR / 'url_match_audit.hybrid.csv'

KNOWN_SECRETARIES = tuple(s.lower() for s in APPOINTMENT_WINDOWS.keys())
SECRETARY_TOKEN_MAP = {s.lower(): s.lower() for s in APPOINTMENT_WINDOWS.keys()}

LOCKED_STATUSES = {'matched', 'forced_match'}
UNRESOLVED_STATUSES = {'unmatched', 'ambiguous_review', 'ambigous_review'}

SPEECH_TYPE_KEYWORDS = (
    ('press statement', 'press statement'),
    ('remarks to the press', 'remarks to the press'),
    ('press conference', 'press conference'),
    ('press briefing', 'press briefing'),
    ('press availability', 'press availability'),
    ('special briefing', 'special briefing'),
    ('briefing', 'briefing'),
    ('interview', 'interview'),
    ('testimony', 'testimony'),
    ('speech', 'speech'),
    ('address', 'address'),
    ('statement', 'statement'),
    ('remarks', 'remarks'),
)

BOILERPLATE_CUES = re.compile(
    r'(?i)(skip to main content|an official website|toggle topbar menu|privacy|cookie|bureaus\s*&\s*offices|'
    r'department\s+of\s+state\s+by\s+state|contact us|site map|download adobe)'
)
QUESTION_OR_SPEAKER_CUE = re.compile(r'(?im)^\s*(?:question|q\.|q:|moderator|host|reporter|mr\.|ms\.|mrs\.|dr\.|participant)\b')
UPPER_SPEAKER_LINE_RE = re.compile(r'(?m)^\s*[A-Z][A-Z .\'\-]{2,40}:\s*')
DATE_FROM_URL_PATTERNS = (
    re.compile(r'(?<!\d)(20\d{2})[/-](\d{1,2})[/-](\d{1,2})(?!\d)'),
    re.compile(r'(?<!\d)(19\d{2})[/-](\d{1,2})[/-](\d{1,2})(?!\d)'),
    re.compile(r'(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)'),
    re.compile(r'(?<!\d)(19\d{2})(\d{2})(\d{2})(?!\d)'),
)

QUEUE_Q1 = 'Q1_near_miss'
QUEUE_Q2 = 'Q2_ambiguous_collision'
QUEUE_Q3 = 'Q3_temporal_fallback'
QUEUE_Q4 = 'Q4_low_evidence'


@dataclass
class RecoveryTargetProfile:
    recover_secretaries: set[str]
    unresolved_dates_by_secretary: Dict[str, List[date]]
    unresolved_years_by_secretary: Dict[str, set[int]]
    missing_secretaries: set[str]
    rows_total: int
    rows_locked: int
    rows_recoverable: int


@dataclass
class CandidateRecord:
    candidate_id: int
    corpus_source: str
    row_number: int
    secretary: str
    date: str
    year: int
    url: str
    url_key: str
    speech_text_clean: str
    speech_text_norm: str
    speech_text_hash: str
    speech_type_guess: str
    location_best: str
    title: str
    final_url: str
    original_url: str
    canonical_url: str
    discovered_from: str
    listing_anchor_text: str
    listing_anchor_date: str
    text_len: int
    raw_text_len: int
    extraction_mode: str


@dataclass
class MatchResult:
    action: str
    row_id: str
    old_url: str
    new_url: str
    old_speech_text: str
    new_speech_text: str
    speech_type_new: str
    location_new: str
    date_new: str
    secretary_new: str
    stage_used: str
    candidate_id: str
    candidate_source: str
    score: float
    margin: float
    reason: str


def setup_field_limit() -> int:
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return limit
        except OverflowError:
            limit //= 10


def safe_lower(v: object) -> str:
    return (str(v) if v is not None else '').strip().lower()


def safe_int(v: object, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except Exception:
        return default


def parse_float(v: object, default: float = 0.0) -> float:
    try:
        return float(str(v).strip())
    except Exception:
        return default


def parse_iso_date(value: str) -> Optional[date]:
    s = (value or '').strip()
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def date_to_iso(dt: Optional[date]) -> str:
    return dt.isoformat() if dt else ''


def normalize_url(url: str) -> str:
    raw = (url or '').strip()
    if not raw:
        return ''
    try:
        parts = urlsplit(raw)
    except Exception:
        parts = None

    if parts is None:
        u = raw.split('#', 1)[0].strip()
        if u and u != '/':
            u = u.rstrip('/')
        return u

    scheme = (parts.scheme or '').lower()
    netloc = (parts.netloc or '').lower()
    path = parts.path or ''
    query = parts.query or ''
    if path and path != '/':
        path = path.rstrip('/')
    if not path and netloc:
        path = '/'
    return urlunsplit((scheme, netloc, path, query, ''))


def canonical_url_key(url: str) -> str:
    u = normalize_url(url)
    if not u:
        return ''
    if u.startswith('https://'):
        u = u[len('https://'):]
    elif u.startswith('http://'):
        u = u[len('http://'):]
    return u


# ── URL quality gate ──────────────────────────────────────────────────────────
# Press-briefing URLs are never valid secretary-speech candidates (the secretary
# is not the active speaker).  Clinton/Kerry additionally require the URL to sit
# on the official secretary-remarks path; any other 2009-2017.state.gov path
# (bureau dispatches, press notes, etc.) is also erroneous.
_URL_BLOCKLIST = ('/r/pa/dpb/', '/r/pa/prs/')
_URL_ALLOWLIST_SECRETARIES = frozenset({'clinton', 'kerry'})
_URL_ALLOWLIST_PATTERNS = ('/20092013clinton/rm/', '/secretary/remarks/')


def is_valid_candidate_url(url: str, secretary: str) -> bool:
    """Return False if url is an inadmissible candidate for this secretary.

    Applies a global blocklist (daily press briefings, press notes) and an
    allowlist restricted to clinton/kerry.  Blank urls pass through so existing
    downstream guards handle them as before.
    """
    if not url:
        return True
    lower = url.lower()
    for bad in _URL_BLOCKLIST:
        if bad in lower:
            return False
    sec = safe_lower(secretary)
    if sec in _URL_ALLOWLIST_SECRETARIES:
        return any(good in lower for good in _URL_ALLOWLIST_PATTERNS)
    return True


def infer_secretary_from_context(*values: str) -> str:
    blob = ' '.join(str(v or '') for v in values).lower()
    for token in SECRETARY_TOKEN_MAP:
        if token in blob:
            return token
    return ''


def normalize_secretary(row: dict) -> str:
    for key in ('certain_speaker_secretary', 'secretary_inferred'):
        value = safe_lower(row.get(key) or '')
        if value and value not in {'unknown', 'none', 'nan'}:
            for token in SECRETARY_TOKEN_MAP:
                if token in value:
                    return token
            return value
    inferred = infer_secretary_from_context(
        row.get('url', ''),
        row.get('final_url', ''),
        row.get('original_url', ''),
        row.get('canonical_url', ''),
        row.get('discovered_from', ''),
        row.get('listing_anchor_text', ''),
        row.get('title', ''),
    )
    return inferred


def best_date_from_row(row: dict) -> Optional[date]:
    exact = parse_iso_date(row.get('exact_date', '') or '')
    if exact:
        return exact

    inferred = parse_date_inferred_value(row.get('date_inferred', '') or '')
    if inferred:
        return inferred

    md_dt, _ = extract_metadata_best_date(row)
    if md_dt:
        return md_dt

    listing = parse_date_inferred_value(row.get('listing_anchor_date', '') or '')
    if listing:
        return listing

    return None


def normalize_whitespace(text: str) -> str:
    return re.sub(r'\s+', ' ', text or '').strip()


def guess_speech_type(row: dict) -> str:
    doc = safe_lower(row.get('article_meta_doctype') or '')
    title = safe_lower(row.get('title') or '')
    blob = f'{doc} | {title}'
    for k, out in SPEECH_TYPE_KEYWORDS:
        if k in blob:
            return out
    return 'unknown'


def guess_location(row: dict, speech_text: str = '') -> str:
    location_fields = [
        'event_location_best',
        'article_meta_location',
        'jsonld_location_name',
        'jsonld_location_locality',
        'jsonld_location_region',
        'raw_dateline_location',
        'listing_anchor_location',
        'meta_geo_placename',
    ]
    for fld in location_fields:
        v = normalize_whitespace(row.get(fld) or '')
        if v:
            cleaned = _clean_location(v)
            if cleaned:
                return cleaned
    return _extract_location_from_speech_text(speech_text) if speech_text else ''


# ── Location cleaning helpers ──────────────────────────────────────────────────

_DC_VARIANTS_RE = re.compile(r'(?i)^washington[\s,]*d[\.\s]?c[\.]?$')
_NYC_VARIANTS_RE = re.compile(
    r'(?i)^new\s+york(?:\s*(?:city)?(?:,?\s*(?:ny|new\s+york))?)?$'
)
_NULL_SENTINELS = frozenset({'na', 'n/a', 'none', 'nan', 'unknown'})
_TITLE_PREFIX_RE = re.compile(
    r'(?i)^(?:thank|yes|okay|ok|welcome|madame|madam|mr|mrs|ms|dr|'
    r'your|good\s+morning|good\s+afternoon|good\s+evening|'
    r'hi,|hello,|glad|great|nice|very\s+good|very\s+glad)\b'
)
_EVENT_PREFIX_RE = re.compile(
    r'(?i)(?:(?:solo|quartet|joint|combined)\s+)?'
    r'(?:press\s+(?:availability|gaggle)|briefing)\s+'
    r'(?:en\s+route\s+to|in)\s+(.+)'
)
_EN_ROUTE_RE      = re.compile(r'(?i)en\s+route\s+to\s+(.+)')
_EN_ROUTE_NO_TO_RE = re.compile(r'(?i)^en\s+route\s+(?!to\b)(.+)')
_ERT_RE            = re.compile(r'(?i)^\bert\s+(.+)')
_ABBREVS = frozenset({'D.C.', 'NY', 'UK', 'UAE', 'UN', 'EU', 'U.S.', 'DC', 'USA'})
_MONTH_PAT = re.compile(
    r'^(?:January|February|March|April|May|June|July|August|'
    r'September|October|November|December)\s+\d{1,2},?\s+\d{4}',
    re.IGNORECASE,
)
_LOCATION_LINE_PAT = re.compile(
    r'^([A-Z][A-Za-z\s\.\-]+(?:,\s*[A-Z][A-Za-z\s\.\-]+)?)$'
)

# Teleconference / telephone → 'Recorded'
_TELECONF_RE = re.compile(
    r'(?i)^(?:via\s+(?:tele(?:conference|phone)|digital\s+videoconference)|teleconference)\b'
)

# Aboard plane → 'On Travel'
_ON_BOARD_RE = re.compile(r'(?i)^(?:on\s+(?:board|plane)\b|aboard\b)')

# Newspaper / media outlet names → 'Op-Ed'
_NEWSPAPER_RE = re.compile(
    r'(?i)(?:wall\s+street\s+journal|new\s+york\s+times|washington\s+post|'
    r'houston\s+chronicle|los\s+angeles\s+times|usa\s+today|usa\s+weekend|'
    r'miami\s+herald|atlanta\s+journal|hindustan\s+times|financial\s+times|'
    r'international\s+herald|sacramento\s+bee|dallas\s+morning\s+news|'
    r'knight.ridder|frankfurter\s+allgemain|russian\s+newspaper|izvestiya)'
)

# State Dept. named rooms (always in D.C.) → 'Washington, D.C.'
_DC_ROOM_RE = re.compile(
    r'(?i)^(?:benjamin\s+franklin\s+room|thomas\s+jefferson\s+room|'
    r'george\s+marshall\s+room|loy\s+henderson\s+auditorium|'
    r'ben\s+franklin\s+room|treaty\s+room|'
    r'harry\s+s\.?\s+truman\s+building|press\s+briefing\s+room)'
)

# Policy-topic extraction artifacts (not place names)
_POLICY_ARTIFACTS: frozenset = frozenset({
    'significant corruption', 'gross violations', 'unjust sentencing',
    'response', 'persons', 'memorandum', 'connection', 'attempts',
    'obstructing', 'efforts', 'corruption', 'elections', 'invoking',
    'human rights abuses', 'coercive influence activities',
    'chemical weapons activities', 'anti-semitic bds activities',
    'intimidation must not be rewarded', 'hong kong under',
    'belarus should be released', 'our elections', 'afghanistan today',
    'afghanistan peace negotiations', 'paris terrorist plot',
    'continued global covid-', 'regime corruption', 'corrupt adoption scheme',
    'transactions related', 'global health assistance policy',
    'additional u.s. foreign assistance', 'p.s', 'feed', 'top', 'close',
    'advance', 'risk', 'new support', 'commemoration', 'conference',
    'swearing-in ceremony', 'national day', 'special meeting', 'trade policy',
    'obstructing', 'efforts to undermine belarusian democracy',
    'u.s.- china diplomatic relations',
})

# City-only → 'City, Country'
_CITY_COUNTRY_MAP: dict = {
    'jerusalem': 'Jerusalem, Israel',
    'tel aviv': 'Tel Aviv, Israel',
    'ramallah': 'Ramallah, West Bank',
    'jericho': 'Jericho, West Bank',
    'hong kong': 'Hong Kong, China',
    'kabul': 'Kabul, Afghanistan',
    'kandahar': 'Kandahar, Afghanistan',
    'tehran': 'Tehran, Iran',
    'beirut': 'Beirut, Lebanon',
    'idlib': 'Idlib, Syria',
    'mosul': 'Mosul, Iraq',
    'erbil': 'Erbil, Iraq',
    'irbil': 'Erbil, Iraq',
    'xinjiang': 'Xinjiang, China',
    'hudaydah': 'Hudaydah, Yemen',
}

# US state abbreviation → full name
_US_STATE_ABBREVS: dict = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
    'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho',
    'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
    'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi',
    'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada',
    'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico', 'NY': 'New York',
    'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma',
    'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina',
    'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah',
    'VT': 'Vermont', 'VA': 'Virginia', 'WI': 'Wisconsin', 'WY': 'Wyoming',
}

# Canonical spelling/variant corrections (keyed by lower-cased input)
_LOCATION_CANONICAL: dict = {
    # Sharm el-Sheikh variants
    'sharm el sheikh, egypt':   'Sharm el-Sheikh, Egypt',
    'sharm el-sheik, egypt':    'Sharm el-Sheikh, Egypt',
    'sharm el sheik, egypt':    'Sharm el-Sheikh, Egypt',
    'sharm el-sheik':           'Sharm el-Sheikh, Egypt',
    'sharm el sheikh':          'Sharm el-Sheikh, Egypt',
    # Marrakech variants
    'marrakesh, morocco':       'Marrakech, Morocco',
    'marakech, morocco':        'Marrakech, Morocco',
    # Dar es Salaam
    'dar es-salaam, tanzania':  'Dar es Salaam, Tanzania',
    'dar es-salaam':            'Dar es Salaam, Tanzania',
    # Typos
    'berliln, germany':         'Berlin, Germany',
    'sanitago, chile':          'Santiago, Chile',
    'toyko, japan':             'Tokyo, Japan',
    'kabul, afganistan':        'Kabul, Afghanistan',
    'st petersburg, russia':    'St. Petersburg, Russia',
    'welington, new zealand':   'Wellington, New Zealand',
    'sco paulo, brazil':        'São Paulo, Brazil',
    'rio de janeiro, brazil':   'Rio de Janeiro, Brazil',
    # England → United Kingdom for major UK cities
    'london, england':          'London, United Kingdom',
    'liverpool, england':       'Liverpool, United Kingdom',
    'blackburn, england':       'Blackburn, United Kingdom',
    # Duplicate country-name forms
    "beijing, people's republic of china": 'Beijing, China',
    "beijing, peoples republic of china":  'Beijing, China',
    "shanghai, people's republic of china": 'Shanghai, China',
    'hong kong, china':         'Hong Kong, China',
    'republic of korea':        'South Korea',
    'singapore, singapore':     'Singapore',
    "manila, the philippines":  'Manila, Philippines',
    'sarajevo, bosnia-herzegovina': 'Sarajevo, Bosnia and Herzegovina',
    'prague, czechia':          'Prague, Czech Republic',
    'skopje, republic of macedonia': 'Skopje, North Macedonia',
    'skopje, macedonia':        'Skopje, North Macedonia',
    # Türkiye → Turkey (standardise to English name)
    'ankara, türkiye':          'Ankara, Turkey',
    'antalya, türkiye':         'Antalya, Turkey',
    'türkiye':                  'Turkey',
    # NYC edge case
    'new york, n.y.':           'New York City, New York',
    'new york city, dc':        'New York City, New York',
    # Washington D.C. typo variant
    'washingon, d.c.':          'Washington, D.C.',
}

# URL slug patterns for blank-row imputation
_OPEND_URL_RE    = re.compile(r'op.?ed', re.I)
_RECORDED_URL_RE = re.compile(r'video[_-](?:message|remarks)|recorded[_-]remarks|teleconference', re.I)
_TRAVEL_URL_RE   = re.compile(
    r'secretary-\w+(?:-\w+)*-travel-to-\w|(?<![a-z])/en-route-to-\w', re.I
)


def _clean_location(text: str, speech_text: str = '') -> str:
    def _fallback() -> str:
        return _extract_location_from_speech_text(speech_text) if speech_text else ''

    # Strip trailing latin1-reencoding artifact (Â from double-encoded UTF-8)
    t = (text or '').strip().rstrip('\xc2').strip()

    # Pass A — reject / reclassify non-geographic values
    if len(t) < 2:
        return _fallback()
    if t.lower() in _NULL_SENTINELS:
        return _fallback()
    tl = t.lower()
    if 'javascript' in tl or '/countries-areas/' in tl:
        return _fallback()
    # UN sorted-list form "Country, The" is a nav sidebar artifact, never a dateline
    if tl.endswith(', the'):
        return _fallback()
    if '?' in t or '!' in t:
        return _fallback()
    if 'for more information' in tl:
        return _fallback()
    if 'recorded on' in tl:
        return 'Recorded'
    if len(t) > 120:
        return _fallback()
    if ', secretary of' in tl:
        return _fallback()
    if _TITLE_PREFIX_RE.match(t):
        return _fallback()
    # Teleconference / telephone → Recorded
    if _TELECONF_RE.match(t):
        return 'Recorded'
    # Aboard / on board plane → On Travel
    if _ON_BOARD_RE.match(t):
        return 'On Travel'
    # Newspaper / media outlet → Op-Ed
    if _NEWSPAPER_RE.search(t):
        return 'Op-Ed'
    # Named State Dept. rooms (all in D.C.)
    if _DC_ROOM_RE.match(t):
        return 'Washington, D.C.'
    # Policy-topic extraction artifacts
    if tl in _POLICY_ARTIFACTS:
        return _fallback()

    # Pass B — extract geographic part from prefixed strings
    for rx in (_EVENT_PREFIX_RE, _EN_ROUTE_RE, _EN_ROUTE_NO_TO_RE, _ERT_RE):
        m = rx.match(t)
        if m:
            t = m.group(1).strip()
            break

    # Pass C — normalize valid geographic strings
    t = normalize_whitespace(t).rstrip('\xc2').strip()
    # Fix common mojibake from latin1/utf-8 double-encoding
    t = (t.replace('\xc3\xa1', 'á').replace('\xc3\xbc', 'ü')
          .replace('\xc3\xa5', 'å').replace('\xc3\xb4', 'ô')
          .replace('\xc3\xa9', 'é').replace('\xc3\xb3', 'ó')
          .replace('\xc3\x94', 'Ô').replace('\xc3\x9c', 'Ü')
          .replace('\xe2\x80\x99', '’').replace('â\x99', "'"))
    # ALL-CAPS → title-case (preserve abbreviations)
    if t.isupper():
        parts = t.split()
        t = ' '.join(p if p in _ABBREVS else p.title() for p in parts)
    # Washington D.C. canonical
    if _DC_VARIANTS_RE.match(t):
        return 'Washington, D.C.'
    # NYC canonical
    if _NYC_VARIANTS_RE.match(t):
        return 'New York City, New York'
    # Typo / variant corrections
    canonical = _LOCATION_CANONICAL.get(t.lower())
    if canonical:
        return canonical
    # City-only → City, Country
    city_mapped = _CITY_COUNTRY_MAP.get(t.lower())
    if city_mapped:
        return city_mapped
    # City, STATE_ABBREV → City, StateName  (e.g. Houston, TX → Houston, Texas)
    m2 = re.match(r'^(.+),\s+([A-Z]{2})$', t)
    if m2:
        city, abbrev = m2.group(1), m2.group(2)
        if abbrev in _US_STATE_ABBREVS:
            return f'{city}, {_US_STATE_ABBREVS[abbrev]}'

    return t


def _impute_location_from_url(url: str) -> str:
    """Return a location label for rows where _clean_location returned empty."""
    if not url:
        return 'Washington, D.C.'
    u = url.lower()
    if _OPEND_URL_RE.search(u):
        return 'Op-Ed'
    if _RECORDED_URL_RE.search(u):
        return 'Recorded'
    if _TRAVEL_URL_RE.search(u):
        return 'On Travel'
    return 'Washington, D.C.'


def _extract_location_from_speech_text(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines()][:60]
    for i, line in enumerate(lines):
        if _MONTH_PAT.match(line) and i > 0:
            candidate = lines[i - 1]
            if _LOCATION_LINE_PAT.match(candidate):
                return _clean_location(candidate)
    for line in lines:
        m = re.match(r'(?i)^location\s*:\s*(.+)', line)
        if m:
            return _clean_location(m.group(1).strip())
    return ''


def secretary_anchor_regex(secretary: str) -> Optional[re.Pattern]:
    sec = safe_lower(secretary)
    if not sec:
        return None
    name = re.escape(sec.upper())
    return re.compile(rf'(?im)^\s*(?:THE\s+)?SECRETARY(?:\s+OF\s+STATE)?\s+(?:[A-Z.\']+\s+)*{name}\b[:\- ]*')


def extract_secretary_only_text(raw_text: str, secretary: str) -> Tuple[str, str]:
    text = raw_text or ''
    if not text:
        return '', 'empty'

    rx = secretary_anchor_regex(secretary)
    if rx:
        m = rx.search(text)
        if m:
            collected: List[str] = []
            cursor = m.start()
            text_len = len(text)
            while True:
                cur = rx.search(text, pos=cursor)
                if not cur:
                    break
                after = cur.end()
                q = QUESTION_OR_SPEAKER_CUE.search(text, pos=after)
                s = UPPER_SPEAKER_LINE_RE.search(text, pos=after)
                end_candidates = [mm.start() for mm in (q, s) if mm]
                end = min(end_candidates) if end_candidates else text_len
                chunk = normalize_whitespace(text[cur.end():end])
                if chunk and not BOILERPLATE_CUES.search(chunk):
                    collected.append(chunk)
                cursor = max(end, cur.start() + 1)
                if cursor >= text_len:
                    break
            joined = '\n\n'.join(collected).strip()
            if joined:
                return joined, 'secretary_anchor'

    # Fallback: keep long natural paragraphs and strip obvious nav boilerplate.
    paragraphs = []
    for block in re.split(r'\n\s*\n+', text):
        p = normalize_whitespace(block)
        if len(p) < 80:
            continue
        if BOILERPLATE_CUES.search(p):
            continue
        paragraphs.append(p)
    if not paragraphs:
        p = normalize_whitespace(text)
        return p[:120000], 'fallback_full'
    return '\n\n'.join(paragraphs)[:120000], 'paragraph_filter'


def normalize_for_match(text: str) -> str:
    t = (text or '').lower()
    t = re.sub(r'[^a-z0-9 ]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t[:120000]


def token_set(text: str, max_tokens: int = 120) -> set[str]:
    toks = []
    for w in normalize_for_match(text).split():
        if len(w) <= 2:
            continue
        toks.append(w)
        if len(toks) >= max_tokens:
            break
    return set(toks)


def overlap_score(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a.intersection(b))
    denom = max(1, min(len(a), len(b)))
    return inter / denom


def score_candidate(source_text: str, source_date: Optional[date], candidate: CandidateRecord) -> Tuple[float, float, float, float]:
    src_norm = normalize_for_match(source_text)
    cand_norm = candidate.speech_text_norm
    seq = SequenceMatcher(None, src_norm, cand_norm).ratio() if src_norm and cand_norm else 0.0
    lex = overlap_score(token_set(src_norm), token_set(cand_norm))

    d_bonus = 0.0
    if source_date and candidate.date:
        cand_dt = parse_iso_date(candidate.date)
        if cand_dt:
            delta = abs((cand_dt - source_date).days)
            if delta == 0:
                d_bonus = 0.03
            elif delta <= 1:
                d_bonus = 0.02
            elif delta <= 7:
                d_bonus = 0.01

    score = min(1.0, 0.82 * seq + 0.16 * lex + d_bonus)
    return score, seq, lex, d_bonus


def required_margin(candidate_count: int, base: float = 0.06) -> float:
    if candidate_count > 800:
        return base + 0.03
    if candidate_count > 300:
        return base + 0.02
    if candidate_count > 100:
        return base + 0.01
    return base


def classify_unresolved_queue(
    stage_used: str,
    score: float,
    margin: float,
    q1_min_score: float,
    q1_max_margin: float,
    q2_min_score: float,
    q2_max_margin: float,
) -> str:
    if stage_used == 'exact_date' and score >= q1_min_score and margin < q1_max_margin:
        return QUEUE_Q1
    if stage_used == 'exact_date' and score >= q2_min_score and margin < q2_max_margin:
        return QUEUE_Q2
    if stage_used in {'plus_minus_1d', 'plus_minus_7d', 'plus_minus_30d', 'url_date_hint', 'secretary_year_fallback'}:
        return QUEUE_Q3
    return QUEUE_Q4


def parse_date_from_url(url: str) -> Optional[date]:
    raw = normalize_url(url)
    if not raw:
        return None
    for rx in DATE_FROM_URL_PATTERNS:
        m = rx.search(raw)
        if not m:
            continue
        try:
            y = int(m.group(1))
            mo = int(m.group(2))
            d = int(m.group(3))
            return date(y, mo, d)
        except Exception:
            continue
    return None


def speech_type_match(row_speech_type: str, candidate_speech_type: str) -> bool:
    a = safe_lower(row_speech_type or '')
    b = safe_lower(candidate_speech_type or '')
    if not a or a in {'unknown', 'na', 'n/a'}:
        return False
    if not b or b in {'unknown', 'na', 'n/a'}:
        return False
    return a == b


def location_overlap(row_location: str, candidate_location: str) -> float:
    a = token_set(row_location or '', max_tokens=30)
    b = token_set(candidate_location or '', max_tokens=30)
    return overlap_score(a, b)


def score_candidates_for_row(
    source_text: str,
    source_date: Optional[date],
    candidates: Sequence[CandidateRecord],
) -> List[Tuple[float, float, float, float, CandidateRecord]]:
    scored = []
    for cand in candidates:
        score, seq, lex, d_bonus = score_candidate(source_text, source_date, cand)
        scored.append((score, seq, lex, d_bonus, cand))
    scored.sort(key=lambda t: (t[0], t[1], t[2], t[4].url_key), reverse=True)
    return scored


def cluster_scored_candidates(
    scored: Sequence[Tuple[float, float, float, float, CandidateRecord]]
) -> List[Tuple[float, float, float, float, int, CandidateRecord]]:
    clusters: Dict[Tuple[str, str], List[Tuple[float, float, float, float, CandidateRecord]]] = defaultdict(list)
    for item in scored:
        cand = item[4]
        key = (cand.url_key or '', cand.speech_text_hash or '')
        clusters[key].append(item)

    collapsed: List[Tuple[float, float, float, float, int, CandidateRecord]] = []
    for items in clusters.values():
        best = max(items, key=lambda t: (t[0], t[1], t[2]))
        collapsed.append((best[0], best[1], best[2], best[3], len(items), best[4]))

    collapsed.sort(key=lambda t: (t[0], t[4], t[1], t[2], t[5].url_key), reverse=True)
    return collapsed


def build_recovery_row(
    *,
    row_id: str,
    action: str,
    reason: str,
    stage_used: str,
    candidate_id: str,
    candidate_source: str,
    score: str,
    margin: str,
    old_url: str,
    new_url: str,
    old_speech_text: str,
    new_speech_text: str,
    speech_type_new: str,
    location_new: str,
    date_new: str,
    secretary_new: str,
    changed_url: str,
    changed_text: str,
    queue_id: str = '',
    queue_outcome: str = '',
    queue_rule: str = '',
) -> dict:
    return {
        'row_id': row_id,
        'action': action,
        'reason': reason,
        'stage_used': stage_used,
        'candidate_id': candidate_id,
        'candidate_source': candidate_source,
        'score': score,
        'margin': margin,
        'old_url': old_url,
        'new_url': new_url,
        'old_speech_text': old_speech_text,
        'new_speech_text': new_speech_text,
        'speech_type_new': speech_type_new,
        'location_new': location_new,
        'date_new': date_new,
        'secretary_new': secretary_new,
        'changed_url': changed_url,
        'changed_text': changed_text,
        'queue_id': queue_id,
        'queue_outcome': queue_outcome,
        'queue_rule': queue_rule,
    }


def recommended_action_for_queue(queue_id: str) -> str:
    if queue_id == QUEUE_Q1:
        return 'manual_accept_if_top_url_repeats_or_is_known_good'
    if queue_id == QUEUE_Q2:
        return 'manual_compare_top_candidates_and_metadata'
    if queue_id == QUEUE_Q3:
        return 'verify_date_alignment_and_term_window'
    return 'defer_or_require_external_source_enrichment'


def top_candidates_json(
    scored: Sequence[Tuple[float, float, float, float, CandidateRecord]],
    limit: int = 3,
) -> str:
    data = []
    for score, _seq, _lex, _db, cand in list(scored)[:max(1, limit)]:
        data.append(
            {
                'candidate_id': cand.candidate_id,
                'source': cand.corpus_source,
                'url': cand.url,
                'date': cand.date,
                'score': round(float(score), 6),
            }
        )
    return json.dumps(data, ensure_ascii=True)


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        '''
        CREATE TABLE IF NOT EXISTS candidates (
            candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
            corpus_source TEXT NOT NULL,
            row_number INTEGER NOT NULL,
            secretary TEXT NOT NULL,
            date TEXT NOT NULL,
            year INTEGER NOT NULL,
            url TEXT NOT NULL,
            url_key TEXT NOT NULL,
            speech_text_clean TEXT NOT NULL,
            speech_text_norm TEXT NOT NULL,
            speech_text_hash TEXT NOT NULL,
            speech_type_guess TEXT NOT NULL,
            location_best TEXT NOT NULL,
            title TEXT NOT NULL,
            final_url TEXT NOT NULL,
            original_url TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            discovered_from TEXT NOT NULL,
            listing_anchor_text TEXT NOT NULL,
            listing_anchor_date TEXT NOT NULL,
            text_len INTEGER NOT NULL,
            raw_text_len INTEGER NOT NULL,
            extraction_mode TEXT NOT NULL,
            UNIQUE(corpus_source, row_number)
        );

        CREATE INDEX IF NOT EXISTS idx_candidates_secretary_date ON candidates(secretary, date);
        CREATE INDEX IF NOT EXISTS idx_candidates_secretary_year ON candidates(secretary, year);
        CREATE INDEX IF NOT EXISTS idx_candidates_url_key ON candidates(url_key);

        CREATE TABLE IF NOT EXISTS index_checkpoint (
            source_name TEXT PRIMARY KEY,
            rows_read INTEGER NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS index_stats (
            source_name TEXT PRIMARY KEY,
            rows_seen INTEGER NOT NULL DEFAULT 0,
            rows_kept INTEGER NOT NULL DEFAULT 0,
            rows_skipped INTEGER NOT NULL DEFAULT 0,
            giant_rows INTEGER NOT NULL DEFAULT 0,
            parse_errors INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        '''
    )
    conn.commit()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        '''
        INSERT INTO meta(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        ''',
        (key, value),
    )


def get_checkpoint(conn: sqlite3.Connection, source_name: str) -> Tuple[int, int]:
    row = conn.execute(
        'SELECT rows_read, completed FROM index_checkpoint WHERE source_name = ?',
        (source_name,),
    ).fetchone()
    if not row:
        return 0, 0
    return int(row[0]), int(row[1])


def set_checkpoint(conn: sqlite3.Connection, source_name: str, rows_read: int, completed: int) -> None:
    now = datetime.utcnow().isoformat(timespec='seconds')
    conn.execute(
        '''
        INSERT INTO index_checkpoint(source_name, rows_read, completed, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(source_name) DO UPDATE SET
            rows_read = excluded.rows_read,
            completed = excluded.completed,
            updated_at = excluded.updated_at
        ''',
        (source_name, rows_read, completed, now),
    )


def set_index_stats(
    conn: sqlite3.Connection,
    source_name: str,
    rows_seen: int,
    rows_kept: int,
    rows_skipped: int,
    giant_rows: int,
    parse_errors: int,
) -> None:
    now = datetime.utcnow().isoformat(timespec='seconds')
    conn.execute(
        '''
        INSERT INTO index_stats(source_name, rows_seen, rows_kept, rows_skipped, giant_rows, parse_errors, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_name) DO UPDATE SET
            rows_seen = excluded.rows_seen,
            rows_kept = excluded.rows_kept,
            rows_skipped = excluded.rows_skipped,
            giant_rows = excluded.giant_rows,
            parse_errors = excluded.parse_errors,
            updated_at = excluded.updated_at
        ''',
        (source_name, rows_seen, rows_kept, rows_skipped, giant_rows, parse_errors, now),
    )


def reset_source_index(conn: sqlite3.Connection, source_name: str) -> None:
    conn.execute('DELETE FROM candidates WHERE corpus_source = ?', (source_name,))
    conn.execute('DELETE FROM index_checkpoint WHERE source_name = ?', (source_name,))
    conn.execute('DELETE FROM index_stats WHERE source_name = ?', (source_name,))
    conn.commit()


def load_recovery_target_profile(audit_csv: Path, lock_score: float = 0.6) -> RecoveryTargetProfile:
    recover_secretaries: set[str] = set()
    unresolved_dates: Dict[str, set[date]] = defaultdict(set)
    unresolved_years: Dict[str, set[int]] = defaultdict(set)
    source_secretaries: set[str] = set()

    rows_total = 0
    rows_locked = 0
    rows_recoverable = 0

    with audit_csv.open('r', encoding='utf-8', errors='replace', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows_total += 1
            sec = safe_lower(row.get('secretary') or '')
            if sec:
                source_secretaries.add(sec)

            status = safe_lower(row.get('status') or '')
            score = parse_float(row.get('top1_score') or 0)

            is_locked = status in LOCKED_STATUSES and score > lock_score
            if is_locked:
                rows_locked += 1
                continue

            rows_recoverable += 1
            if sec:
                recover_secretaries.add(sec)
                dt = parse_iso_date(row.get('Date', '') or '')
                if dt:
                    unresolved_dates[sec].add(dt)
                    unresolved_years[sec].add(dt.year)

    missing_secretaries = {s.lower() for s in APPOINTMENT_WINDOWS.keys()} - source_secretaries

    return RecoveryTargetProfile(
        recover_secretaries=recover_secretaries,
        unresolved_dates_by_secretary={k: sorted(v) for k, v in unresolved_dates.items()},
        unresolved_years_by_secretary=dict(unresolved_years),
        missing_secretaries=missing_secretaries,
        rows_total=rows_total,
        rows_locked=rows_locked,
        rows_recoverable=rows_recoverable,
    )


def date_in_appointment_window(secretary: str, dt: Optional[date]) -> bool:
    if not dt:
        return False
    window = APPOINTMENT_WINDOWS.get(secretary.upper())
    if not window:
        return False
    start, end = window
    return start <= dt <= end


def should_keep_candidate(
    secretary: str,
    dt: Optional[date],
    profile: RecoveryTargetProfile,
    date_window_days: int,
) -> bool:
    if not secretary or not dt:
        return False

    # Missing-term expansion coverage (e.g., Rubio).
    if secretary in profile.missing_secretaries and date_in_appointment_window(secretary, dt):
        return True

    if secretary not in profile.recover_secretaries:
        return False

    # Date-focused prefilter for recoverable rows.
    target_dates = profile.unresolved_dates_by_secretary.get(secretary, [])
    if target_dates:
        for td in target_dates:
            if abs((dt - td).days) <= date_window_days:
                return True

    years = profile.unresolved_years_by_secretary.get(secretary, set())
    if years and dt.year in years:
        return True

    return False


def candidate_from_row(row: dict, source_name: str, row_number: int) -> Optional[dict]:
    sec = normalize_secretary(row)
    if not sec:
        return None

    dt = best_date_from_row(row)
    if not dt:
        return None

    # Appointment window enforcement for high-quality secretary-term purity.
    if not date_in_appointment_window(sec, dt):
        return None

    url = normalize_url(
        row.get('url')
        or row.get('final_url')
        or row.get('original_url')
        or row.get('scraped_url')
        or ''
    )
    if not url:
        return None

    raw_text = row.get('raw_text', '') or ''
    clean_text, mode = extract_secretary_only_text(raw_text, sec)
    clean_text = normalize_whitespace(clean_text)
    if len(clean_text) < 60:
        return None

    speech_type = guess_speech_type(row)
    location_best = guess_location(row, speech_text=clean_text)
    title = normalize_whitespace(row.get('title') or '')

    speech_norm = normalize_for_match(clean_text)
    if not speech_norm:
        return None

    text_hash = hashlib.sha1(speech_norm.encode('utf-8')).hexdigest()

    return {
        'corpus_source': source_name,
        'row_number': row_number,
        'secretary': sec,
        'date': dt.isoformat(),
        'year': dt.year,
        'url': url,
        'url_key': canonical_url_key(url),
        'speech_text_clean': clean_text,
        'speech_text_norm': speech_norm,
        'speech_text_hash': text_hash,
        'speech_type_guess': speech_type,
        'location_best': location_best,
        'title': title,
        'final_url': normalize_url(row.get('final_url') or ''),
        'original_url': normalize_url(row.get('original_url') or ''),
        'canonical_url': normalize_url(row.get('canonical_url') or ''),
        'discovered_from': normalize_whitespace(row.get('discovered_from') or ''),
        'listing_anchor_text': normalize_whitespace(row.get('listing_anchor_text') or ''),
        'listing_anchor_date': date_to_iso(parse_date_inferred_value(row.get('listing_anchor_date') or '')),
        'text_len': safe_int(row.get('text_len') or 0),
        'raw_text_len': len(raw_text),
        'extraction_mode': mode,
    }


def flush_candidates(conn: sqlite3.Connection, pending: List[dict]) -> None:
    if not pending:
        return
    conn.executemany(
        '''
        INSERT OR REPLACE INTO candidates(
            corpus_source, row_number, secretary, date, year, url, url_key,
            speech_text_clean, speech_text_norm, speech_text_hash,
            speech_type_guess, location_best, title,
            final_url, original_url, canonical_url,
            discovered_from, listing_anchor_text, listing_anchor_date,
            text_len, raw_text_len, extraction_mode
        ) VALUES (
            :corpus_source, :row_number, :secretary, :date, :year, :url, :url_key,
            :speech_text_clean, :speech_text_norm, :speech_text_hash,
            :speech_type_guess, :location_best, :title,
            :final_url, :original_url, :canonical_url,
            :discovered_from, :listing_anchor_text, :listing_anchor_date,
            :text_len, :raw_text_len, :extraction_mode
        )
        ''',
        pending,
    )
    pending.clear()


def index_one_corpus(
    conn: sqlite3.Connection,
    source_name: str,
    corpus_csv: Path,
    profile: RecoveryTargetProfile,
    resume: bool,
    checkpoint_every: int,
    date_window_days: int,
    giant_row_threshold: int,
    large_max_rows: int,
    show_progress: bool,
) -> None:
    if not corpus_csv.exists():
        raise FileNotFoundError(f'Corpus not found: {corpus_csv}')

    if not resume:
        reset_source_index(conn, source_name)

    rows_done, completed = get_checkpoint(conn, source_name)
    if resume and completed == 1:
        return

    pending: List[dict] = []
    rows_seen = rows_done
    rows_kept = 0
    rows_skipped = 0
    giant_rows = 0
    parse_errors = 0

    with corpus_csv.open('r', encoding='utf-8', errors='replace', newline='') as f:
        reader = csv.DictReader(f)

        for _ in range(rows_done):
            try:
                next(reader)
            except StopIteration:
                break

        iterator = reader
        if show_progress:
            iterator = tqdm(reader, desc=f'Index {source_name}', unit='row', disable=False)

        for row in iterator:
            rows_seen += 1
            try:
                raw_len = len(row.get('raw_text', '') or '')
                if raw_len >= giant_row_threshold:
                    giant_rows += 1

                cand = candidate_from_row(row, source_name=source_name, row_number=rows_seen)
                if cand is None:
                    rows_skipped += 1
                else:
                    dt = parse_iso_date(cand['date'])
                    if should_keep_candidate(cand['secretary'], dt, profile, date_window_days=date_window_days):
                        pending.append(cand)
                        rows_kept += 1
                    else:
                        rows_skipped += 1
            except Exception:
                parse_errors += 1
                rows_skipped += 1

            if large_max_rows > 0 and source_name == 'large' and (rows_seen - rows_done) >= large_max_rows:
                flush_candidates(conn, pending)
                set_checkpoint(conn, source_name, rows_seen, completed=0)
                set_index_stats(conn, source_name, rows_seen, rows_kept, rows_skipped, giant_rows, parse_errors)
                conn.commit()
                return

            if checkpoint_every > 0 and rows_seen % checkpoint_every == 0:
                flush_candidates(conn, pending)
                set_checkpoint(conn, source_name, rows_seen, completed=0)
                set_index_stats(conn, source_name, rows_seen, rows_kept, rows_skipped, giant_rows, parse_errors)
                conn.commit()

    flush_candidates(conn, pending)
    set_checkpoint(conn, source_name, rows_seen, completed=1)
    set_index_stats(conn, source_name, rows_seen, rows_kept, rows_skipped, giant_rows, parse_errors)
    conn.commit()


def load_candidates_for_row(
    conn: sqlite3.Connection,
    secretary: str,
    row_date: date,
    year_fallback_cap: int,
) -> Tuple[str, List[CandidateRecord]]:
    sec = safe_lower(secretary)
    if not sec:
        return 'none', []

    def to_records(rows: Sequence[sqlite3.Row]) -> List[CandidateRecord]:
        out: List[CandidateRecord] = []
        for r in rows:
            out.append(
                CandidateRecord(
                    candidate_id=int(r['candidate_id']),
                    corpus_source=r['corpus_source'],
                    row_number=int(r['row_number']),
                    secretary=r['secretary'],
                    date=r['date'],
                    year=int(r['year']),
                    url=r['url'],
                    url_key=r['url_key'],
                    speech_text_clean=r['speech_text_clean'],
                    speech_text_norm=r['speech_text_norm'],
                    speech_text_hash=r['speech_text_hash'],
                    speech_type_guess=r['speech_type_guess'],
                    location_best=r['location_best'],
                    title=r['title'],
                    final_url=r['final_url'],
                    original_url=r['original_url'],
                    canonical_url=r['canonical_url'],
                    discovered_from=r['discovered_from'],
                    listing_anchor_text=r['listing_anchor_text'],
                    listing_anchor_date=r['listing_anchor_date'],
                    text_len=int(r['text_len']),
                    raw_text_len=int(r['raw_text_len']),
                    extraction_mode=r['extraction_mode'],
                )
            )
        return out

    exact_rows = conn.execute(
        '''
        SELECT * FROM candidates
        WHERE secretary = ? AND date = ?
        ORDER BY corpus_source ASC, candidate_id ASC
        LIMIT 1200
        ''',
        (sec, row_date.isoformat()),
    ).fetchall()
    if exact_rows:
        return 'exact_date', to_records(exact_rows)

    plus1_rows = conn.execute(
        '''
        SELECT * FROM candidates
        WHERE secretary = ? AND date BETWEEN ? AND ?
        ORDER BY corpus_source ASC, candidate_id ASC
        LIMIT 1500
        ''',
        (sec, (row_date - timedelta(days=1)).isoformat(), (row_date + timedelta(days=1)).isoformat()),
    ).fetchall()
    if plus1_rows:
        return 'plus_minus_1d', to_records(plus1_rows)

    plus7_rows = conn.execute(
        '''
        SELECT * FROM candidates
        WHERE secretary = ? AND date BETWEEN ? AND ?
        ORDER BY corpus_source ASC, candidate_id ASC
        LIMIT 1800
        ''',
        (sec, (row_date - timedelta(days=7)).isoformat(), (row_date + timedelta(days=7)).isoformat()),
    ).fetchall()
    if plus7_rows:
        return 'plus_minus_7d', to_records(plus7_rows)

    fallback_rows = conn.execute(
        '''
        SELECT * FROM candidates
        WHERE secretary = ? AND year = ?
        ORDER BY corpus_source ASC, candidate_id ASC
        LIMIT ?
        ''',
        (sec, row_date.year, max(1, year_fallback_cap)),
    ).fetchall()
    if fallback_rows:
        return 'secretary_year_fallback', to_records(fallback_rows)

    return 'none', []


def load_candidates_for_row_v2(
    conn: sqlite3.Connection,
    secretary: str,
    row_date: date,
    year_fallback_cap: int,
    old_url: str,
) -> Tuple[str, List[CandidateRecord]]:
    sec = safe_lower(secretary)
    if not sec:
        return 'none', []

    def to_records(rows: Sequence[sqlite3.Row]) -> List[CandidateRecord]:
        out: List[CandidateRecord] = []
        for r in rows:
            out.append(
                CandidateRecord(
                    candidate_id=int(r['candidate_id']),
                    corpus_source=r['corpus_source'],
                    row_number=int(r['row_number']),
                    secretary=r['secretary'],
                    date=r['date'],
                    year=int(r['year']),
                    url=r['url'],
                    url_key=r['url_key'],
                    speech_text_clean=r['speech_text_clean'],
                    speech_text_norm=r['speech_text_norm'],
                    speech_text_hash=r['speech_text_hash'],
                    speech_type_guess=r['speech_type_guess'],
                    location_best=r['location_best'],
                    title=r['title'],
                    final_url=r['final_url'],
                    original_url=r['original_url'],
                    canonical_url=r['canonical_url'],
                    discovered_from=r['discovered_from'],
                    listing_anchor_text=r['listing_anchor_text'],
                    listing_anchor_date=r['listing_anchor_date'],
                    text_len=int(r['text_len']),
                    raw_text_len=int(r['raw_text_len']),
                    extraction_mode=r['extraction_mode'],
                )
            )
        return out

    stages = [
        ('exact_date', row_date, row_date, 1200),
        ('plus_minus_1d', row_date - timedelta(days=1), row_date + timedelta(days=1), 1500),
        ('plus_minus_7d', row_date - timedelta(days=7), row_date + timedelta(days=7), 1800),
        ('plus_minus_30d', row_date - timedelta(days=30), row_date + timedelta(days=30), 2200),
    ]

    for stage_name, start_dt, end_dt, lim in stages:
        rows = conn.execute(
            '''
            SELECT * FROM candidates
            WHERE secretary = ? AND date BETWEEN ? AND ?
            ORDER BY corpus_source ASC, candidate_id ASC
            LIMIT ?
            ''',
            (sec, start_dt.isoformat(), end_dt.isoformat(), lim),
        ).fetchall()
        if rows:
            return stage_name, to_records(rows)

    hinted = parse_date_from_url(old_url)
    if hinted and date_in_appointment_window(sec, hinted):
        rows = conn.execute(
            '''
            SELECT * FROM candidates
            WHERE secretary = ? AND date BETWEEN ? AND ?
            ORDER BY corpus_source ASC, candidate_id ASC
            LIMIT 1000
            ''',
            (sec, (hinted - timedelta(days=1)).isoformat(), (hinted + timedelta(days=1)).isoformat()),
        ).fetchall()
        if rows:
            return 'url_date_hint', to_records(rows)

    fallback_rows = conn.execute(
        '''
        SELECT * FROM candidates
        WHERE secretary = ? AND year = ?
        ORDER BY corpus_source ASC, candidate_id ASC
        LIMIT ?
        ''',
        (sec, row_date.year, max(1, year_fallback_cap)),
    ).fetchall()
    if fallback_rows:
        return 'secretary_year_fallback', to_records(fallback_rows)

    return 'none', []


def load_audit_by_row_id(audit_csv: Path) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    with audit_csv.open('r', encoding='utf-8', errors='replace', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rid = (row.get('source_row_id') or row.get('row_id') or '').strip()
            if rid:
                out[rid] = row
    return out


def load_final_rows(final_csv: Path) -> Tuple[List[dict], List[str]]:
    with final_csv.open('r', encoding='utf-8', errors='replace', newline='') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames


def lock_row(audit_row: Optional[dict], lock_score: float) -> bool:
    if not audit_row:
        return False
    status = safe_lower(audit_row.get('status') or audit_row.get('url_match_status') or '')
    score = parse_float(audit_row.get('top1_score') or audit_row.get('url_match_score') or 0)
    return status in LOCKED_STATUSES and score > lock_score


def run_recover(
    conn: sqlite3.Connection,
    input_final: Path,
    input_audit: Path,
    output_recovery_results: Path,
    min_score: float,
    lock_score: float,
    year_fallback_cap: int,
    show_progress: bool,
) -> None:
    audit_map = load_audit_by_row_id(input_audit)
    final_rows, _fields = load_final_rows(input_final)

    recovery_rows: List[dict] = []

    iterator = final_rows
    if show_progress:
        iterator = tqdm(final_rows, desc='Recover rows', unit='row', disable=False)

    for row in iterator:
        row_id = (row.get('row_id') or '').strip()
        old_url = (row.get('url') or '').strip()
        old_text = row.get('speech_text') or ''

        secretary = safe_lower(row.get('secretary') or '')

        audit_row = audit_map.get(row_id)
        if not old_url and audit_row:
            old_url = normalize_url(
                (audit_row.get('old_url') or '')
                or (audit_row.get('new_url') or '')
            )
        if not is_valid_candidate_url(old_url, secretary):
            old_text = ''
        if lock_row(audit_row, lock_score=lock_score) and is_valid_candidate_url(old_url, secretary):
            recovery_rows.append(
                {
                    'row_id': row_id,
                    'action': 'locked',
                    'reason': 'locked_high_quality',
                    'stage_used': 'locked',
                    'candidate_id': '',
                    'candidate_source': '',
                    'score': audit_row.get('top1_score', ''),
                    'margin': audit_row.get('margin', ''),
                    'old_url': old_url,
                    'new_url': old_url,
                    'old_speech_text': old_text,
                    'new_speech_text': old_text,
                    'speech_type_new': row.get('speech_type', ''),
                    'location_new': row.get('Location', ''),
                    'date_new': row.get('Date', ''),
                    'secretary_new': row.get('secretary', ''),
                    'changed_url': '0',
                    'changed_text': '0',
                }
            )
            continue

        row_date = parse_iso_date(row.get('Date', '') or '')
        if not secretary or not row_date:
            recovery_rows.append(
                {
                    'row_id': row_id,
                    'action': 'unchanged_unresolved',
                    'reason': 'missing_secretary_or_date',
                    'stage_used': 'none',
                    'candidate_id': '',
                    'candidate_source': '',
                    'score': '0.000000',
                    'margin': '0.000000',
                    'old_url': old_url,
                    'new_url': old_url,
                    'old_speech_text': old_text,
                    'new_speech_text': old_text,
                    'speech_type_new': row.get('speech_type', ''),
                    'location_new': row.get('Location', ''),
                    'date_new': row.get('Date', ''),
                    'secretary_new': row.get('secretary', ''),
                    'changed_url': '0',
                    'changed_text': '0',
                }
            )
            continue

        stage, candidates = load_candidates_for_row(
            conn,
            secretary=secretary,
            row_date=row_date,
            year_fallback_cap=year_fallback_cap,
        )
        candidates = [c for c in candidates if is_valid_candidate_url(c.url, secretary)]

        if not candidates:
            recovery_rows.append(
                {
                    'row_id': row_id,
                    'action': 'unchanged_unresolved',
                    'reason': 'no_candidates',
                    'stage_used': stage,
                    'candidate_id': '',
                    'candidate_source': '',
                    'score': '0.000000',
                    'margin': '0.000000',
                    'old_url': old_url,
                    'new_url': old_url,
                    'old_speech_text': old_text,
                    'new_speech_text': old_text,
                    'speech_type_new': row.get('speech_type', ''),
                    'location_new': row.get('Location', ''),
                    'date_new': row.get('Date', ''),
                    'secretary_new': row.get('secretary', ''),
                    'changed_url': '0',
                    'changed_text': '0',
                }
            )
            continue

        scored = []
        for cand in candidates:
            score, seq, lex, d_bonus = score_candidate(old_text, row_date, cand)
            scored.append((score, seq, lex, d_bonus, cand))
        scored.sort(key=lambda t: (t[0], t[1], t[2], t[4].url_key), reverse=True)

        top1_score, _top1_seq, _top1_lex, _top1_db, top1 = scored[0]
        top2_score = scored[1][0] if len(scored) > 1 else 0.0
        margin = top1_score - top2_score
        accept = top1_score >= min_score and margin >= required_margin(len(candidates), base=0.06)

        if accept:
            new_text = top1.speech_text_clean or old_text
            speech_type_new = row.get('speech_type', '')
            if top1.speech_type_guess and safe_lower(speech_type_new) in {'', 'unknown', 'na'}:
                speech_type_new = top1.speech_type_guess

            location_new = row.get('Location', '')
            if not normalize_whitespace(location_new) and top1.location_best:
                location_new = top1.location_best

            new_url = top1.url or old_url
            recovery_rows.append(
                {
                    'row_id': row_id,
                    'action': 'recovered',
                    'reason': 'accepted_thresholds',
                    'stage_used': stage,
                    'candidate_id': str(top1.candidate_id),
                    'candidate_source': top1.corpus_source,
                    'score': f'{top1_score:.6f}',
                    'margin': f'{margin:.6f}',
                    'old_url': old_url,
                    'new_url': new_url,
                    'old_speech_text': old_text,
                    'new_speech_text': new_text,
                    'speech_type_new': speech_type_new,
                    'location_new': location_new,
                    'date_new': row.get('Date', ''),
                    'secretary_new': row.get('secretary', ''),
                    'changed_url': '1' if new_url != old_url else '0',
                    'changed_text': '1' if normalize_whitespace(new_text) != normalize_whitespace(old_text) else '0',
                }
            )
        else:
            recovery_rows.append(
                {
                    'row_id': row_id,
                    'action': 'unchanged_unresolved',
                    'reason': 'below_threshold_or_margin',
                    'stage_used': stage,
                    'candidate_id': str(top1.candidate_id),
                    'candidate_source': top1.corpus_source,
                    'score': f'{top1_score:.6f}',
                    'margin': f'{margin:.6f}',
                    'old_url': old_url,
                    'new_url': old_url,
                    'old_speech_text': old_text,
                    'new_speech_text': old_text,
                    'speech_type_new': row.get('speech_type', ''),
                    'location_new': row.get('Location', ''),
                    'date_new': row.get('Date', ''),
                    'secretary_new': row.get('secretary', ''),
                    'changed_url': '0',
                    'changed_text': '0',
                }
            )

    fields = [
        'row_id', 'action', 'reason', 'stage_used', 'candidate_id', 'candidate_source',
        'score', 'margin',
        'old_url', 'new_url',
        'old_speech_text', 'new_speech_text',
        'speech_type_new', 'location_new', 'date_new', 'secretary_new',
        'changed_url', 'changed_text',
    ]
    with output_recovery_results.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(recovery_rows)


def run_recover_v2(
    conn: sqlite3.Connection,
    input_final: Path,
    input_audit: Path,
    output_recovery_results: Path,
    output_triage_csv: Path,
    min_score: float,
    lock_score: float,
    year_fallback_cap: int,
    show_progress: bool,
    q1_min_score: float,
    q1_max_margin: float,
    q1_cluster_margin: float,
    q2_min_score: float,
    q2_max_margin: float,
    q2_accept_score: float,
    q2_accept_margin: float,
    q3_min_score: float,
    q3_min_margin: float,
    q3_url_hint_min_score: float,
    q3_url_hint_min_margin: float,
    q4_direct_url_min_score: float,
) -> None:
    audit_map = load_audit_by_row_id(input_audit)
    final_rows, _fields = load_final_rows(input_final)

    recovery_rows: List[dict] = []
    triage_rows: List[dict] = []

    iterator = final_rows
    if show_progress:
        iterator = tqdm(final_rows, desc='Recover rows v2', unit='row', disable=False)

    for row in iterator:
        row_id = (row.get('row_id') or '').strip()
        old_url = normalize_url(row.get('url') or '')
        old_text = row.get('speech_text') or ''
        row_speech_type = row.get('speech_type') or ''
        row_location = row.get('Location') or ''

        secretary = safe_lower(row.get('secretary') or '')

        audit_row = audit_map.get(row_id)
        if not old_url and audit_row:
            old_url = normalize_url(
                (audit_row.get('old_url') or '')
                or (audit_row.get('new_url') or '')
            )
        if not is_valid_candidate_url(old_url, secretary):
            old_text = ''
        if lock_row(audit_row, lock_score=lock_score) and is_valid_candidate_url(old_url, secretary):
            recovery_rows.append(
                build_recovery_row(
                    row_id=row_id,
                    action='locked',
                    reason='locked_high_quality',
                    stage_used='locked',
                    candidate_id='',
                    candidate_source='',
                    score=str(audit_row.get('top1_score', '')),
                    margin=str(audit_row.get('margin', '')),
                    old_url=old_url,
                    new_url=old_url,
                    old_speech_text=old_text,
                    new_speech_text=old_text,
                    speech_type_new=row.get('speech_type', ''),
                    location_new=row.get('Location', ''),
                    date_new=row.get('Date', ''),
                    secretary_new=row.get('secretary', ''),
                    changed_url='0',
                    changed_text='0',
                    queue_id='',
                    queue_outcome='locked',
                    queue_rule='locked_high_quality',
                )
            )
            continue

        row_date = parse_iso_date(row.get('Date', '') or '')
        if not secretary or not row_date:
            queue_id = QUEUE_Q4
            recovery_rows.append(
                build_recovery_row(
                    row_id=row_id,
                    action='unchanged_unresolved',
                    reason='missing_secretary_or_date',
                    stage_used='none',
                    candidate_id='',
                    candidate_source='',
                    score='0.000000',
                    margin='0.000000',
                    old_url=old_url,
                    new_url=old_url,
                    old_speech_text=old_text,
                    new_speech_text=old_text,
                    speech_type_new=row.get('speech_type', ''),
                    location_new=row.get('Location', ''),
                    date_new=row.get('Date', ''),
                    secretary_new=row.get('secretary', ''),
                    changed_url='0',
                    changed_text='0',
                    queue_id=queue_id,
                    queue_outcome='deferred',
                    queue_rule='missing_required_metadata',
                )
            )
            triage_rows.append(
                {
                    'row_id': row_id,
                    'queue_id': queue_id,
                    'stage_used': 'none',
                    'reason': 'missing_secretary_or_date',
                    'score': '0.000000',
                    'margin': '0.000000',
                    'candidate_id': '',
                    'candidate_source': '',
                    'recommended_next_action': recommended_action_for_queue(queue_id),
                    'secretary': secretary,
                    'date': row.get('Date', ''),
                    'old_url': old_url,
                    'top_candidates_json': '[]',
                }
            )
            continue

        stage, candidates = load_candidates_for_row_v2(
            conn,
            secretary=secretary,
            row_date=row_date,
            year_fallback_cap=year_fallback_cap,
            old_url=old_url,
        )
        candidates = [c for c in candidates if is_valid_candidate_url(c.url, secretary)]

        if not candidates:
            queue_id = QUEUE_Q4
            recovery_rows.append(
                build_recovery_row(
                    row_id=row_id,
                    action='unchanged_unresolved',
                    reason='no_candidates',
                    stage_used=stage,
                    candidate_id='',
                    candidate_source='',
                    score='0.000000',
                    margin='0.000000',
                    old_url=old_url,
                    new_url=old_url,
                    old_speech_text=old_text,
                    new_speech_text=old_text,
                    speech_type_new=row.get('speech_type', ''),
                    location_new=row.get('Location', ''),
                    date_new=row.get('Date', ''),
                    secretary_new=row.get('secretary', ''),
                    changed_url='0',
                    changed_text='0',
                    queue_id=queue_id,
                    queue_outcome='deferred',
                    queue_rule='no_candidates',
                )
            )
            triage_rows.append(
                {
                    'row_id': row_id,
                    'queue_id': queue_id,
                    'stage_used': stage,
                    'reason': 'no_candidates',
                    'score': '0.000000',
                    'margin': '0.000000',
                    'candidate_id': '',
                    'candidate_source': '',
                    'recommended_next_action': recommended_action_for_queue(queue_id),
                    'secretary': secretary,
                    'date': row.get('Date', ''),
                    'old_url': old_url,
                    'top_candidates_json': '[]',
                }
            )
            continue

        scored = score_candidates_for_row(old_text, row_date, candidates)
        top1_score, _top1_seq, _top1_lex, _top1_db, top1 = scored[0]
        top2_score = scored[1][0] if len(scored) > 1 else 0.0
        margin = top1_score - top2_score

        # Backward-compatible baseline acceptance gate first.
        accept = top1_score >= min_score and margin >= required_margin(len(candidates), base=0.06)
        chosen = top1
        reason = 'accepted_thresholds' if accept else 'below_threshold_or_margin'
        queue_id = ''
        queue_outcome = 'accepted' if accept else 'deferred'
        queue_rule = 'baseline_accept' if accept else 'baseline_reject'

        if not accept:
            queue_id = classify_unresolved_queue(
                stage_used=stage,
                score=top1_score,
                margin=margin,
                q1_min_score=q1_min_score,
                q1_max_margin=q1_max_margin,
                q2_min_score=q2_min_score,
                q2_max_margin=q2_max_margin,
            )

            if queue_id == QUEUE_Q1:
                collapsed = cluster_scored_candidates(scored)
                c_top = collapsed[0]
                c_top2 = collapsed[1][0] if len(collapsed) > 1 else 0.0
                c_margin = c_top[0] - c_top2
                chosen = c_top[5]
                top1_score = c_top[0]
                margin = c_margin
                if top1_score >= q1_min_score and (c_margin >= q1_cluster_margin or c_top[4] >= 2):
                    accept = True
                    reason = 'accepted_q1_cluster_consensus'
                    queue_outcome = 'accepted'
                    queue_rule = 'q1_cluster_consensus'
                else:
                    reason = 'below_threshold_or_margin'
                    queue_outcome = 'deferred'
                    queue_rule = 'q1_not_decisive_after_clustering'

            elif queue_id == QUEUE_Q2:
                old_key = canonical_url_key(old_url)
                reranked: List[Tuple[float, float, float, float, CandidateRecord]] = []
                for score, seq, lex, d_bonus, cand in scored:
                    boost = 0.0
                    if speech_type_match(row_speech_type, cand.speech_type_guess):
                        boost += 0.025
                    if location_overlap(row_location, cand.location_best) >= 0.5:
                        boost += 0.020
                    if old_key and cand.url_key == old_key:
                        boost += 0.015
                    reranked.append((min(1.0, score + boost), seq, lex, d_bonus, cand))
                reranked.sort(key=lambda t: (t[0], t[1], t[2], t[4].url_key), reverse=True)
                top1_score, _seq2, _lex2, _db2, chosen = reranked[0]
                top2_score = reranked[1][0] if len(reranked) > 1 else 0.0
                margin = top1_score - top2_score
                if top1_score >= q2_accept_score and margin >= q2_accept_margin:
                    accept = True
                    reason = 'accepted_q2_rerank'
                    queue_outcome = 'accepted'
                    queue_rule = 'q2_metadata_rerank'
                    scored = reranked
                else:
                    reason = 'q2_manual_review_needed'
                    queue_outcome = 'deferred'
                    queue_rule = 'q2_rerank_not_decisive'
                    scored = reranked

            elif queue_id == QUEUE_Q3:
                chosen = top1
                if stage == 'url_date_hint':
                    if top1_score >= q3_url_hint_min_score and margin >= q3_url_hint_min_margin:
                        accept = True
                        reason = 'accepted_q3_url_date_hint'
                        queue_outcome = 'accepted'
                        queue_rule = 'q3_url_date_hint'
                    else:
                        reason = 'q3_manual_review_needed'
                        queue_outcome = 'deferred'
                        queue_rule = 'q3_url_date_hint_not_decisive'
                else:
                    if top1_score >= q3_min_score and margin >= q3_min_margin:
                        accept = True
                        reason = 'accepted_q3_temporal'
                        queue_outcome = 'accepted'
                        queue_rule = 'q3_temporal_accept'
                    else:
                        reason = 'below_threshold_or_margin'
                        queue_outcome = 'deferred'
                        queue_rule = 'q3_temporal_not_decisive'

            else:
                chosen = top1
                old_key = canonical_url_key(old_url)
                if old_key and chosen.url_key == old_key and top1_score >= q4_direct_url_min_score:
                    accept = True
                    reason = 'accepted_q4_url_direct'
                    queue_outcome = 'accepted'
                    queue_rule = 'q4_direct_url_rescue'
                else:
                    reason = 'low_evidence_deferred'
                    queue_outcome = 'deferred'
                    queue_rule = 'q4_defer'

        if accept:
            new_text = chosen.speech_text_clean or old_text
            speech_type_new = row.get('speech_type', '')
            if chosen.speech_type_guess and safe_lower(speech_type_new) in {'', 'unknown', 'na'}:
                speech_type_new = chosen.speech_type_guess

            location_new = row.get('Location', '')
            if not normalize_whitespace(location_new) and chosen.location_best:
                location_new = chosen.location_best

            new_url = chosen.url or old_url
            recovery_rows.append(
                build_recovery_row(
                    row_id=row_id,
                    action='recovered',
                    reason=reason,
                    stage_used=stage,
                    candidate_id=str(chosen.candidate_id),
                    candidate_source=chosen.corpus_source,
                    score=f'{top1_score:.6f}',
                    margin=f'{margin:.6f}',
                    old_url=old_url,
                    new_url=new_url,
                    old_speech_text=old_text,
                    new_speech_text=new_text,
                    speech_type_new=speech_type_new,
                    location_new=location_new,
                    date_new=row.get('Date', ''),
                    secretary_new=row.get('secretary', ''),
                    changed_url='1' if new_url != old_url else '0',
                    changed_text='1' if normalize_whitespace(new_text) != normalize_whitespace(old_text) else '0',
                    queue_id=queue_id,
                    queue_outcome=queue_outcome,
                    queue_rule=queue_rule,
                )
            )
        else:
            recovery_rows.append(
                build_recovery_row(
                    row_id=row_id,
                    action='unchanged_unresolved',
                    reason=reason,
                    stage_used=stage,
                    candidate_id=str(chosen.candidate_id) if chosen else '',
                    candidate_source=chosen.corpus_source if chosen else '',
                    score=f'{top1_score:.6f}',
                    margin=f'{margin:.6f}',
                    old_url=old_url,
                    new_url=old_url,
                    old_speech_text=old_text,
                    new_speech_text=old_text,
                    speech_type_new=row.get('speech_type', ''),
                    location_new=row.get('Location', ''),
                    date_new=row.get('Date', ''),
                    secretary_new=row.get('secretary', ''),
                    changed_url='0',
                    changed_text='0',
                    queue_id=queue_id,
                    queue_outcome=queue_outcome,
                    queue_rule=queue_rule,
                )
            )
            triage_rows.append(
                {
                    'row_id': row_id,
                    'queue_id': queue_id,
                    'stage_used': stage,
                    'reason': reason,
                    'score': f'{top1_score:.6f}',
                    'margin': f'{margin:.6f}',
                    'candidate_id': str(chosen.candidate_id) if chosen else '',
                    'candidate_source': chosen.corpus_source if chosen else '',
                    'recommended_next_action': recommended_action_for_queue(queue_id),
                    'secretary': secretary,
                    'date': row.get('Date', ''),
                    'old_url': old_url,
                    'top_candidates_json': top_candidates_json(scored, limit=3),
                }
            )

    fields = [
        'row_id', 'action', 'reason', 'stage_used', 'candidate_id', 'candidate_source',
        'score', 'margin',
        'old_url', 'new_url',
        'old_speech_text', 'new_speech_text',
        'speech_type_new', 'location_new', 'date_new', 'secretary_new',
        'changed_url', 'changed_text',
        'queue_id', 'queue_outcome', 'queue_rule',
    ]
    with output_recovery_results.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(recovery_rows)

    triage_fields = [
        'row_id', 'queue_id', 'stage_used', 'reason', 'score', 'margin',
        'candidate_id', 'candidate_source', 'recommended_next_action',
        'secretary', 'date', 'old_url', 'top_candidates_json',
    ]
    with output_triage_csv.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=triage_fields)
        w.writeheader()
        w.writerows(triage_rows)


def max_numeric_row_id(rows: Sequence[dict]) -> int:
    max_id = 0
    for r in rows:
        rid = (r.get('row_id') or '').strip()
        if rid.isdigit():
            max_id = max(max_id, int(rid))
    return max_id


def run_expand(
    conn: sqlite3.Connection,
    input_final: Path,
    output_mined: Path,
    term_policy: str,
    per_secretary_cap: int,
) -> None:
    rows, fields = load_final_rows(input_final)
    present_secretaries = {safe_lower(r.get('secretary') or '') for r in rows if safe_lower(r.get('secretary') or '')}

    missing = [s.lower() for s in APPOINTMENT_WINDOWS.keys() if s.lower() not in present_secretaries]

    start_row_id = max_numeric_row_id(rows) + 1
    next_row_id = start_row_id

    mined_rows: List[dict] = []
    seen_keys: set[tuple[str, str]] = set()

    for sec in missing:
        window = APPOINTMENT_WINDOWS.get(sec.upper())
        if not window:
            continue
        start, end = window

        cand_rows = conn.execute(
            '''
            SELECT * FROM candidates
            WHERE secretary = ? AND date BETWEEN ? AND ?
            ORDER BY date ASC, candidate_id ASC
            ''',
            (sec, start.isoformat(), end.isoformat()),
        ).fetchall()

        count = 0
        for r in cand_rows:
            if count >= per_secretary_cap:
                break

            speech = r['speech_text_clean'] or ''
            if len(speech) < 140:
                continue

            url = r['url'] or ''
            url_key = r['url_key'] or canonical_url_key(url)
            text_hash = r['speech_text_hash'] or hashlib.sha1(normalize_for_match(speech).encode('utf-8')).hexdigest()
            dedup_key = (url_key, text_hash)
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            mined_rows.append(
                {
                    'speech_type': r['speech_type_guess'] or 'remarks',
                    'Location': r['location_best'] or '',
                    'Date': r['date'],
                    'secretary': sec,
                    'speech_text': speech,
                    'row_id': str(next_row_id),
                    'url': url,
                    'url_match_score': '0.990000',
                    'url_match_method': 'hybrid_mined',
                    'url_match_status': 'matched',
                    'match_pass': 'hybrid_expand',
                    'cleaning_profile_used': 'hybrid_candidate',
                    'pass3_stage': '',
                    'pass3_profile_used': '',
                    'mined_source': r['corpus_source'],
                    'candidate_id': str(r['candidate_id']),
                    'candidate_url_key': url_key,
                    'candidate_text_hash': text_hash,
                }
            )
            next_row_id += 1
            count += 1

    fields_out = [
        'speech_type', 'Location', 'Date', 'secretary', 'speech_text', 'row_id', 'url',
        'url_match_score', 'url_match_method', 'url_match_status', 'match_pass',
        'cleaning_profile_used', 'pass3_stage', 'pass3_profile_used',
        'mined_source', 'candidate_id', 'candidate_url_key', 'candidate_text_hash',
    ]
    with output_mined.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields_out)
        w.writeheader()
        w.writerows(mined_rows)


def run_finalize(
    input_final: Path,
    recovery_results: Path,
    mined_rows_csv: Path,
    output_final: Path,
    output_audit: Path,
) -> None:
    rows, fields = load_final_rows(input_final)
    by_row_id = {(r.get('row_id') or '').strip(): r for r in rows}

    recovery_map: Dict[str, dict] = {}
    with recovery_results.open('r', encoding='utf-8', errors='replace', newline='') as f:
        rr = csv.DictReader(f)
        for row in rr:
            rid = (row.get('row_id') or '').strip()
            if rid:
                recovery_map[rid] = row

    final_rows: List[dict] = []
    audit_rows: List[dict] = []

    existing_dedup: set[tuple[str, str]] = set()

    for row in rows:
        rid = (row.get('row_id') or '').strip()
        rec = recovery_map.get(rid)
        out = dict(row)

        if rec and rec.get('action') == 'recovered':
            out['url'] = rec.get('new_url', out.get('url', ''))
            out['speech_text'] = rec.get('new_speech_text', out.get('speech_text', ''))
            out['speech_type'] = rec.get('speech_type_new', out.get('speech_type', ''))
            out['Location'] = rec.get('location_new', out.get('Location', ''))
            out['Date'] = rec.get('date_new', out.get('Date', ''))
            out['secretary'] = rec.get('secretary_new', out.get('secretary', ''))

        url_key = canonical_url_key(out.get('url') or '')
        text_hash = hashlib.sha1(normalize_for_match(out.get('speech_text') or '').encode('utf-8')).hexdigest()
        existing_dedup.add((url_key, text_hash))

        final_rows.append(out)

        if rec:
            audit_rows.append(
                {
                    'row_id': rid,
                    'action': rec.get('action', ''),
                    'reason': rec.get('reason', ''),
                    'old_url': rec.get('old_url', ''),
                    'new_url': rec.get('new_url', ''),
                    'score': rec.get('score', ''),
                    'margin': rec.get('margin', ''),
                    'stage_used': rec.get('stage_used', ''),
                    'candidate_id': rec.get('candidate_id', ''),
                    'candidate_source': rec.get('candidate_source', ''),
                    'changed_url': rec.get('changed_url', ''),
                    'changed_text': rec.get('changed_text', ''),
                    'queue_id': rec.get('queue_id', ''),
                    'queue_outcome': rec.get('queue_outcome', ''),
                    'queue_rule': rec.get('queue_rule', ''),
                    'is_appended_mined': '0',
                }
            )
        else:
            audit_rows.append(
                {
                    'row_id': rid,
                    'action': 'unchanged_no_recovery_record',
                    'reason': '',
                    'old_url': row.get('url', ''),
                    'new_url': row.get('url', ''),
                    'score': '',
                    'margin': '',
                    'stage_used': '',
                    'candidate_id': '',
                    'candidate_source': '',
                    'changed_url': '0',
                    'changed_text': '0',
                    'queue_id': '',
                    'queue_outcome': '',
                    'queue_rule': '',
                    'is_appended_mined': '0',
                }
            )

    with mined_rows_csv.open('r', encoding='utf-8', errors='replace', newline='') as f:
        mr = list(csv.DictReader(f))

    for row in mr:
        url_key = canonical_url_key(row.get('url') or '')
        text_hash = hashlib.sha1(normalize_for_match(row.get('speech_text') or '').encode('utf-8')).hexdigest()
        key = (url_key, text_hash)
        if key in existing_dedup:
            continue
        existing_dedup.add(key)

        appended = {k: row.get(k, '') for k in fields}
        final_rows.append(appended)
        audit_rows.append(
            {
                'row_id': row.get('row_id', ''),
                'action': 'appended_mined',
                'reason': 'missing_term_expansion',
                'old_url': '',
                'new_url': row.get('url', ''),
                'score': row.get('url_match_score', ''),
                'margin': '',
                'stage_used': 'expand',
                'candidate_id': row.get('candidate_id', ''),
                'candidate_source': row.get('mined_source', ''),
                'changed_url': '1',
                'changed_text': '1',
                'queue_id': '',
                'queue_outcome': 'appended_mined',
                'queue_rule': 'expand_missing_term',
                'is_appended_mined': '1',
            }
        )

    with output_final.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(final_rows)

    audit_fields = [
        'row_id', 'action', 'reason', 'old_url', 'new_url', 'score', 'margin', 'stage_used',
        'candidate_id', 'candidate_source', 'changed_url', 'changed_text',
        'queue_id', 'queue_outcome', 'queue_rule',
        'is_appended_mined',
    ]
    with output_audit.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=audit_fields)
        w.writeheader()
        w.writerows(audit_rows)


def connect_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA temp_store=MEMORY')
    conn.execute('PRAGMA cache_size=-50000')
    return conn


def add_common_cli_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument('--checkpoint-db', type=Path, default=DEFAULT_DB)
    p.add_argument('--no-progress', action='store_true')


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description='Hybrid Small+Large corpus recovery pipeline for secretary speech outputs')
    sub = p.add_subparsers(dest='cmd', required=True)

    # index
    p_index = sub.add_parser('index', help='Build hybrid candidate store from small+large corpora')
    add_common_cli_flags(p_index)
    p_index.add_argument('--small-corpus', type=Path, default=DEFAULT_SMALL_CORPUS)
    p_index.add_argument('--large-corpus', type=Path, default=DEFAULT_LARGE_CORPUS)
    p_index.add_argument('--audit', type=Path, default=DEFAULT_INPUT_AUDIT)
    p_index.add_argument('--resume', action='store_true')
    p_index.add_argument('--workers', type=int, default=1, help='Reserved for future parallel indexing; currently sequential streaming')
    p_index.add_argument('--checkpoint-every', type=int, default=2000)
    p_index.add_argument('--date-window-days', type=int, default=7)
    p_index.add_argument('--large-max-rows', type=int, default=0, help='Optional bounded pass for large corpus')
    p_index.add_argument('--giant-row-threshold', type=int, default=50_000_000)
    p_index.add_argument('--lock-score', type=float, default=0.6)

    # recover
    p_recover = sub.add_parser('recover', help='Recover unresolved/low-score rows using hybrid candidate store')
    add_common_cli_flags(p_recover)
    p_recover.add_argument('--input-final', type=Path, default=DEFAULT_INPUT_FINAL)
    p_recover.add_argument('--input-audit', type=Path, default=DEFAULT_INPUT_AUDIT)
    p_recover.add_argument('--output-recovery-results', type=Path, default=DEFAULT_RECOVERY_RESULTS)
    p_recover.add_argument('--min-score', type=float, default=0.72)
    p_recover.add_argument('--lock-score', type=float, default=0.6)
    p_recover.add_argument('--year-fallback-cap', type=int, default=1500)

    # recover_v2
    p_recover_v2 = sub.add_parser('recover_v2', help='Queue-aware recover path (backward-compatible with recover output usage)')
    add_common_cli_flags(p_recover_v2)
    p_recover_v2.add_argument('--input-final', type=Path, default=DEFAULT_INPUT_FINAL)
    p_recover_v2.add_argument('--input-audit', type=Path, default=DEFAULT_INPUT_AUDIT)
    p_recover_v2.add_argument('--output-recovery-results', type=Path, default=DEFAULT_RECOVERY_RESULTS_V2)
    p_recover_v2.add_argument('--output-triage-csv', type=Path, default=DEFAULT_TRIAGE_OUT_V2)
    p_recover_v2.add_argument('--min-score', type=float, default=0.72)
    p_recover_v2.add_argument('--lock-score', type=float, default=0.6)
    p_recover_v2.add_argument('--year-fallback-cap', type=int, default=1500)
    p_recover_v2.add_argument('--q1-min-score', type=float, default=0.72)
    p_recover_v2.add_argument('--q1-max-margin', type=float, default=0.06)
    p_recover_v2.add_argument('--q1-cluster-margin', type=float, default=0.025)
    p_recover_v2.add_argument('--q2-min-score', type=float, default=0.60)
    p_recover_v2.add_argument('--q2-max-margin', type=float, default=0.03)
    p_recover_v2.add_argument('--q2-accept-score', type=float, default=0.74)
    p_recover_v2.add_argument('--q2-accept-margin', type=float, default=0.03)
    p_recover_v2.add_argument('--q3-min-score', type=float, default=0.70)
    p_recover_v2.add_argument('--q3-min-margin', type=float, default=0.08)
    p_recover_v2.add_argument('--q3-url-hint-min-score', type=float, default=0.66)
    p_recover_v2.add_argument('--q3-url-hint-min-margin', type=float, default=0.05)
    p_recover_v2.add_argument('--q4-direct-url-min-score', type=float, default=0.52)

    # expand
    p_expand = sub.add_parser('expand', help='Mine missing secretary-term rows (e.g., Rubio) from indexed candidates')
    add_common_cli_flags(p_expand)
    p_expand.add_argument('--input-final', type=Path, default=DEFAULT_INPUT_FINAL)
    p_expand.add_argument('--output-mined', type=Path, default=DEFAULT_MINED)
    p_expand.add_argument('--term-policy', choices=['appointment_windows'], default='appointment_windows')
    p_expand.add_argument('--per-secretary-cap', type=int, default=400)

    # finalize
    p_finalize = sub.add_parser('finalize', help='Merge recovered rows and mined rows into expanded final CSV + hybrid audit')
    add_common_cli_flags(p_finalize)
    p_finalize.add_argument('--input-final', type=Path, default=DEFAULT_INPUT_FINAL)
    p_finalize.add_argument('--recovery-results', type=Path, default=DEFAULT_RECOVERY_RESULTS)
    p_finalize.add_argument('--mined-rows', type=Path, default=DEFAULT_MINED)
    p_finalize.add_argument('--output-final', type=Path, default=DEFAULT_FINAL_OUT)
    p_finalize.add_argument('--output-audit', type=Path, default=DEFAULT_AUDIT_OUT)

    return p


def main() -> None:
    setup_field_limit()
    args = build_parser().parse_args()
    show_progress = not args.no_progress

    if args.cmd == 'index':
        conn = connect_db(args.checkpoint_db)
        init_db(conn)

        profile = load_recovery_target_profile(args.audit, lock_score=args.lock_score)
        set_meta(conn, 'profile_rows_total', str(profile.rows_total))
        set_meta(conn, 'profile_rows_locked', str(profile.rows_locked))
        set_meta(conn, 'profile_rows_recoverable', str(profile.rows_recoverable))
        set_meta(conn, 'profile_recover_secretaries', ','.join(sorted(profile.recover_secretaries)))
        set_meta(conn, 'profile_missing_secretaries', ','.join(sorted(profile.missing_secretaries)))
        conn.commit()

        index_one_corpus(
            conn,
            source_name='small',
            corpus_csv=args.small_corpus,
            profile=profile,
            resume=args.resume,
            checkpoint_every=max(100, int(args.checkpoint_every)),
            date_window_days=max(0, int(args.date_window_days)),
            giant_row_threshold=max(100000, int(args.giant_row_threshold)),
            large_max_rows=0,
            show_progress=show_progress,
        )
        index_one_corpus(
            conn,
            source_name='large',
            corpus_csv=args.large_corpus,
            profile=profile,
            resume=args.resume,
            checkpoint_every=max(100, int(args.checkpoint_every)),
            date_window_days=max(0, int(args.date_window_days)),
            giant_row_threshold=max(100000, int(args.giant_row_threshold)),
            large_max_rows=max(0, int(args.large_max_rows)),
            show_progress=show_progress,
        )

        stats = conn.execute('SELECT source_name, rows_seen, rows_kept, rows_skipped, giant_rows, parse_errors FROM index_stats ORDER BY source_name').fetchall()
        for s in stats:
            print(
                f"[index:{s['source_name']}] rows_seen={s['rows_seen']} rows_kept={s['rows_kept']} "
                f"rows_skipped={s['rows_skipped']} giant_rows={s['giant_rows']} parse_errors={s['parse_errors']}"
            )

        total_cands = conn.execute('SELECT COUNT(*) FROM candidates').fetchone()[0]
        print(f'[index] candidates_total={total_cands}')
        print(f'[index] db={args.checkpoint_db}')
        conn.close()
        return

    if args.cmd == 'recover':
        conn = connect_db(args.checkpoint_db)
        init_db(conn)
        run_recover(
            conn,
            input_final=args.input_final,
            input_audit=args.input_audit,
            output_recovery_results=args.output_recovery_results,
            min_score=float(args.min_score),
            lock_score=float(args.lock_score),
            year_fallback_cap=max(200, int(args.year_fallback_cap)),
            show_progress=show_progress,
        )
        conn.close()
        print(f'[recover] wrote {args.output_recovery_results}')
        return

    if args.cmd == 'recover_v2':
        conn = connect_db(args.checkpoint_db)
        init_db(conn)
        run_recover_v2(
            conn,
            input_final=args.input_final,
            input_audit=args.input_audit,
            output_recovery_results=args.output_recovery_results,
            output_triage_csv=args.output_triage_csv,
            min_score=float(args.min_score),
            lock_score=float(args.lock_score),
            year_fallback_cap=max(200, int(args.year_fallback_cap)),
            show_progress=show_progress,
            q1_min_score=float(args.q1_min_score),
            q1_max_margin=float(args.q1_max_margin),
            q1_cluster_margin=float(args.q1_cluster_margin),
            q2_min_score=float(args.q2_min_score),
            q2_max_margin=float(args.q2_max_margin),
            q2_accept_score=float(args.q2_accept_score),
            q2_accept_margin=float(args.q2_accept_margin),
            q3_min_score=float(args.q3_min_score),
            q3_min_margin=float(args.q3_min_margin),
            q3_url_hint_min_score=float(args.q3_url_hint_min_score),
            q3_url_hint_min_margin=float(args.q3_url_hint_min_margin),
            q4_direct_url_min_score=float(args.q4_direct_url_min_score),
        )
        conn.close()
        print(f'[recover_v2] wrote {args.output_recovery_results}')
        print(f'[recover_v2] wrote {args.output_triage_csv}')
        return

    if args.cmd == 'expand':
        conn = connect_db(args.checkpoint_db)
        init_db(conn)
        run_expand(
            conn,
            input_final=args.input_final,
            output_mined=args.output_mined,
            term_policy=args.term_policy,
            per_secretary_cap=max(1, int(args.per_secretary_cap)),
        )
        conn.close()
        print(f'[expand] wrote {args.output_mined}')
        return

    if args.cmd == 'finalize':
        run_finalize(
            input_final=args.input_final,
            recovery_results=args.recovery_results,
            mined_rows_csv=args.mined_rows,
            output_final=args.output_final,
            output_audit=args.output_audit,
        )
        print(f'[finalize] wrote {args.output_final}')
        print(f'[finalize] wrote {args.output_audit}')
        return


if __name__ == '__main__':
    main()
