import argparse
import csv
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from tqdm import tqdm


csv.field_size_limit(sys.maxsize)

INPUT_CSV = Path('secstate_naive_corpus.csv')
OUTPUT_CSV = Path('secstate_naive_corpus_certain_secstate_speakers.csv')
SUMMARY_TXT = Path('secstate_certain_speakers_summary.txt')
QUALITY_AUDIT_TXT = Path('secstate_quality_audit_summary.txt')


SECRETARY_VARIANTS = {
    'ALBRIGHT': [r'ALBRIGHT', r'MADELEINE\s+(?:K\.?\s+)?ALBRIGHT'],
    'BLINKEN': [r'BLINKEN', r'ANTONY\s+(?:J\.?\s+)?BLINKEN'],
    'POMPEO': [r'POMPEO', r'MI(?:KE|CHAEL)\s+(?:R\.?\s+)?POMPEO'],
    'RICE': [r'RICE', r'CONDOLEEZZA\s+RICE'],
    'KERRY': [r'KERRY', r'JOHN\s+KERRY'],
    'TILLERSON': [r'TILLERSON', r'REX\s+(?:W\.?\s+)?TILLERSON'],
    'CLINTON': [r'CLINTON', r'HILLARY\s+(?:RODHAM\s+)?CLINTON'],
    'RUBIO': [r'RUBIO', r'MARCO\s+RUBIO'],
    'POWELL': [r'POWELL', r'COLIN\s+(?:L\.?\s+)?POWELL'],
}
SECRETARY_ORDER = tuple(SECRETARY_VARIANTS.keys())

DEFAULT_METADATA_LISTING_SLUG = 'remarks-former-secretary-of-state-{last}'

MODERN_LISTING_SLUGS = {
    'TILLERSON': 'remarks-former-secretary-of-state-tillerson',
    'POMPEO': 'remarks-secretary-pompeo',
    'BLINKEN': 'remarks-secretary-blinken',
    'RUBIO': 'remarks-secretary-rubio',
}

SECRETARY_LISTING_PAGE_CAPS = {
    'TILLERSON': 49,
}


RULES = [
    {
        'rule': 'strict_named_colon_label',
        'confidence': 'high',
        'pattern_template': r"(?im)^\s*(?:THE\s+)?SECRETARY(?:\s+OF\s+STATE)?\s+__NAME_ALT__\s*:\s*",
        'needs_natural_after': True,
    },
    {
        'rule': 'named_dash_label',
        'confidence': 'medium',
        'pattern_template': r"(?im)^\s*(?:THE\s+)?SECRETARY(?:\s+OF\s+STATE)?\s+__NAME_ALT__\s*[—–-]\s*",
        'needs_natural_after': True,
    },
    {
        'rule': 'named_comma_label',
        'confidence': 'medium',
        'pattern_template': r"(?im)^\s*(?:THE\s+)?SECRETARY(?:\s+OF\s+STATE)?\s+__NAME_ALT__\s*,\s*",
        'needs_natural_after': True,
    },
    {
        'rule': 'lastname_only_colon_label',
        'confidence': 'medium',
        'pattern_template': r"(?im)^\s*__LAST_NAME__\s*:\s*",
        'needs_natural_after': True,
    },
    {
        'rule': 'remarks_by_with_secretary_header',
        'confidence': 'low',
        'pattern_template': (
            r"(?im)^\s*(?:REMARKS|INTERVIEW|PRESS\s+AVAILABILITY|ON[-\s]THE[-\s]RECORD\s+BRIEFING|"
            r"BRIEFING|STATEMENT|ADDRESS)[^\n]{0,200}\b(?:BY|WITH)\b[^\n]{0,120}"
            r"\b(?:SECRETARY(?:\s+OF\s+STATE)?\s+)?__NAME_ALT__\b"
        ),
        'needs_natural_after': True,
    },
    {
        'rule': 'title_only_colon_with_metadata_support',
        'confidence': 'low',
        'pattern_template': r"(?im)^\s*(?:THE\s+)?SECRETARY(?:\s+OF\s+STATE)?\s*:\s*",
        'needs_natural_after': True,
        'needs_metadata_support': True,
    },
]


CONF_LEVEL = {'low': 0, 'medium': 1, 'high': 2}
METADATA_LISTING_REGEX_CACHE = {}
WORKER_CONFIG = None
WORKER_COMPILED_RULES = None
WORKER_ENABLED_SECRETARIES = None

APPOINTMENT_WINDOWS = {
    'ALBRIGHT': (date(1997, 1, 23), date(2001, 1, 20)),
    'POWELL': (date(2001, 1, 20), date(2005, 1, 26)),
    'RICE': (date(2005, 1, 26), date(2009, 1, 20)),
    'CLINTON': (date(2009, 1, 21), date(2013, 2, 1)),
    'KERRY': (date(2013, 2, 1), date(2017, 1, 20)),
    'TILLERSON': (date(2017, 2, 1), date(2018, 3, 31)),
    'POMPEO': (date(2018, 4, 26), date(2021, 1, 20)),
    'BLINKEN': (date(2021, 1, 26), date(2025, 1, 20)),
    'RUBIO': (date(2025, 1, 21), date.max),
}

QUESTION_OR_TRANSCRIPT_CUE = re.compile(r'(?im)^\s*(?:QUESTION|Q|MODERATOR|MR\.|MS\.|HOST|ANNOUNCER)\s*:')
GENERIC_SPEAKER_CUE = re.compile(r'(?im)^\s*(?:THE\s+)?SECRETARY(?:\s+OF\s+STATE)?\s*:')
SENTENCE_AFTER_LABEL = re.compile(r'(?s)^\s*["“”\']?[A-Z][^\n]{8,340}[\.!?]')
ALPHA_WORD = re.compile(r'[A-Za-z]+')


# Broad allowlist for speech/statement-like documents.
POSITIVE_CONTENT_TYPE = re.compile(
    r'''(?ix)
    \b(
        remarks?|opening\s+remarks?|closing\s+remarks?|prepared\s+remarks?|
        statement|statements|press\s+statement|joint\s+statement|readout|read\s*out|
        speech|speeches|address|addresses|commencement\s+address|keynote|keynote\s+address|
        interview|interviews|conversation|fireside\s+chat|town\s+hall|panel\s+discussion|
        briefing|briefings|press\s+briefing|special\s+briefing|background\s+briefing|on[-\s]?the[-\s]?record\s+briefing|
        press\s+availability|joint\s+press\s+availability|doorstep|gaggle|press\s+conference|joint\s+press\s+conference|
        testimony|hearing\s+testimony|appearance\s+before|appearance\s+at|
        message|video\s+message|recorded\s+message|virtual\s+remarks?|delivered\s+remarks?|
        transcript|transcript\s+of\s+remarks?|transcript\s+of\s+briefing|transcript\s+of\s+interview|
        media\s+availability|stakeout|roundtable|plenary\s+remarks?|
        toast|tribute\s+remarks?|memorial\s+remarks?
    )\b
    '''
)

# Guardrail for non-speech doc types that often mention names but are not secretary speech text.
NEGATIVE_CONTENT_TYPE = re.compile(
    r'''(?ix)
    \b(
        fact\s+sheet|travel\s+advisory|security\s+alert|media\s+note|notice\s+to\s+the\s+press|
        biography|bio\b|press\s+release|readout\s+office\s+of\s+the\s+spokesperson|
        sanctions?\s+(?:announcement|designation|update)|treaty\s+action|country\s+reports?|
        human\s+rights\s+report|investment\s+climate\s+statement|report\s+to\s+congress|
        vacancy\s+announcement|job\s+posting|funding\s+opportunity|procurement\b|
        policy\s+paper|strategy\s+document|guidance\s+document|background\s+note
    )\b
    '''
)

MONTH_RX = r'(?:JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUN(?:E)?|JUL(?:Y)?|AUG(?:UST)?|SEP(?:T(?:EMBER)?)?|OCT(?:OBER)?|NOV(?:EMBER)?|DEC(?:EMBER)?)'
URL_YMD = re.compile(r'\b((?:19|20)\d{2})[\-/](\d{2})[\-/](\d{2})\b')
URL_COMPACT_YMD = re.compile(r'\b((?:19|20)\d{2})(\d{2})(\d{2})\b')
URL_PATH_YMD = re.compile(r'/(19\d{2}|20\d{2})/(\d{2})/(\d{2})(?:/|\b)')
LEGACY_STMT_YYMMDD = re.compile(r'(?i)/briefings/statements/(\d{6})[a-z]?\.html?$')
# Legacy Albright-era briefing pages, e.g. /briefings/0004/000424db.html
LEGACY_BRIEFING_YYMMDD = re.compile(r'(?i)/briefings/\d{4}/(\d{6})[a-z]{0,4}\.html?$')
# Legacy Albright-era URL filename token dates, e.g. ps990309a.html, 980123_topic.html, 000914a.html
LEGACY_FILENAME_YYMMDD = re.compile(r'(?i)(?:^|[^\d])(?:[a-z]{0,3})?(\d{6})(?:[a-z]{0,2})(?:[^\d]|$)')
TEXT_MONTH_DY = re.compile(rf'\b({MONTH_RX})\.?\s+(\d{{1,2}}),\s*((?:19|20)\d{{2}})\b', re.I)
TEXT_MONTH_DY_NOCOMMA = re.compile(rf'\b({MONTH_RX})\.?\s+(\d{{1,2}})\s+((?:19|20)\d{{2}})\b', re.I)
TEXT_D_MONTH_Y = re.compile(rf'\b(\d{{1,2}})\s+({MONTH_RX})\.?\s*,?\s*((?:19|20)\d{{2}})\b', re.I)
TEXT_YMD = re.compile(r'\b((?:19|20)\d{2})[\-/](\d{1,2})[\-/](\d{1,2})\b')
TEXT_SLASH_DATE = re.compile(r'\b(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})\b')
RAW_MONTH_DY = re.compile(rf'(?im)^\s*(?:[A-Za-z\s\.]+,\s*)?({MONTH_RX})\.?\s+(\d{{1,2}}),\s*((?:19|20)\d{{2}})\b')
HEADER_ZONE_LABELLED_DATE = re.compile(
    r'(?im)^\s*(?:DATE|FOR\s+IMMEDIATE\s+RELEASE|RELEASED\s+ON|ISSUED(?:\s+ON)?|UPDATED(?:\s+ON)?)\s*[:\-]\s*([^\n]{3,120})$'
)
EVENT_CONTENT_CUE = re.compile(
    r'(?i)\b(remarks?|statement|press\s+availability|briefing|interview|speech|address|on[-\s]the[-\s]record)\b'
)
EVENT_HEADER_LINE_CUE = re.compile(
    r'(?i)^\s*(remarks?|statement|press\s+availability|briefing|interview|speech|address|on[-\s]the[-\s]record)\b'
)
LOCATION_BAD_CUES = re.compile(
    r'(?i)\b(remarks?|statement|briefing|interview|secretary|department|media\s+note|for\s+immediate\s+release|'
    r'fact\s*sheet|report|press\s+release|home|search|contact|archive|menu|navigation|privacy|terms|copyright)\b'
)
LOCATION_STOPWORDS = re.compile(
    r'(?i)\b(with|will|would|could|should|today|tomorrow|yesterday|meeting|remarks?|statement|briefing|press|'
    r'announce|announced|address|interview|question|answer|department|office|committee|council|forum|conference)\b'
)
MONTH_OR_DAY_WORD = re.compile(
    r'(?i)^(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|'
    r'sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|'
    r'monday|tuesday|wednesday|thursday|friday|saturday|sunday)$'
)
LOCATION_CITY_COMMA_REGION = re.compile(
    r"(?i)\b([A-Z][A-Za-z\.'\-]+(?:\s+[A-Z][A-Za-z\.'\-]+){0,4}),\s*"
    r"([A-Z]{2}|[A-Z][A-Za-z\.'\-]+(?:\s+[A-Z][A-Za-z\.'\-]+){0,3})\b"
)
LOCATION_DATELINE_DASH = re.compile(
    r"(?i)\b([A-Z][A-Za-z\.'\-]+(?:\s+[A-Z][A-Za-z\.'\-]+){0,4}),\s*"
    r"([A-Z]{2}|[A-Z][A-Za-z\.'\-]+(?:\s+[A-Z][A-Za-z\.'\-]+){0,3})\s*[—–-]"
)
US_STATE_ABBREVS = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DC', 'DE', 'FL', 'GA', 'HI', 'IA', 'ID', 'IL', 'IN', 'KS', 'KY',
    'LA', 'MA', 'MD', 'ME', 'MI', 'MN', 'MO', 'MS', 'MT', 'NC', 'ND', 'NE', 'NH', 'NJ', 'NM', 'NV', 'NY', 'OH',
    'OK', 'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VA', 'VT', 'WA', 'WI', 'WV', 'WY'
}

REVIEW_TARGET_REASONS = {'missing_date', 'missing_location', 'both_missing'}
REVIEW_TOKEN_STOPWORDS = {
    'secretary', 'state', 'remarks', 'remark', 'statement', 'statements', 'briefing', 'briefings',
    'press', 'interview', 'speech', 'address', 'department', 'office', 'from', 'with', 'that', 'this',
    'about', 'there', 'their', 'would', 'could', 'should', 'have', 'will', 'were', 'been',
}
PA_URL_CUE = re.compile(r'(?i)/r/pa/')
PA_EVENT_CUE = re.compile(r'(?i)\b(daily\s+press\s+briefing|press\s+briefing|press\s+availability|briefing|readout|remarks?|statement)\b')
NAV_BOILERPLATE_CUE = re.compile(
    r'(?i)(toggle\s+topbar\s+menu|bureaus\s*&\s*offices|skip\s+to\s+content|an\s+official\s+website|'
    r'department\s+of\s+state\s+by\s+state|rules\s+and\s+info\s+collection|partner\s+with\s+state)'
)
NON_EVENT_URL_PATH_CUE = re.compile(
    r'(?i)/(?:bureaus?-offices|careers?|blogs?|faq\.html|feedback\d*\.html|agency-strategic-plan)(?:/|$)'
)
NON_PHYSICAL_LOCATION_CUE = re.compile(
    r'(?i)\b(via\s+teleconference|teleconference|virtual|video\s+conference|videoconference|phone|telephone|off\s+camera|on\s+background)\b'
)
LOCATION_CITY_MONTH_DATE = re.compile(
    rf"(?i)\b([A-Z][A-Za-z\.'\-]+(?:\s+[A-Z][A-Za-z\.'\-]+){{0,4}})\s+({MONTH_RX})\.?\s+\d{{1,2}},\s*(?:19|20)\d{{2}}\b"
)
LOCATION_CITY_REGION_MONTH_DATE = re.compile(
    rf"(?i)\b([A-Z][A-Za-z\.'\-]+(?:\s+[A-Z][A-Za-z\.'\-]+){{0,4}}),\s*([A-Z][A-Za-z\.'\-]+(?:\s+[A-Z][A-Za-z\.'\-]+){{0,3}})\s+({MONTH_RX})\.?\s+\d{{1,2}},\s*(?:19|20)\d{{2}}\b"
)
LOCATION_VENUE_CITY_COUNTRY_MONTH_DATE = re.compile(
    rf"(?i)\b([A-Z][A-Za-z\.'\-]+(?:\s+[A-Z][A-Za-z\.'\-]+){{0,5}}),\s*([A-Z][A-Za-z\.'\-]+(?:\s+[A-Z][A-Za-z\.'\-]+){{0,4}}),\s*([A-Z][A-Za-z\.'\-]+(?:\s+[A-Z][A-Za-z\.'\-]+){{0,3}})\s*,\s*({MONTH_RX})\.?\s+\d{{1,2}},\s*(?:19|20)\d{{2}}\b"
)


def clean_ws(text: str) -> str:
    return re.sub(r'\s+', ' ', str(text or '')).strip()


def normalize_location_candidate(text: str) -> str:
    s = clean_ws(text)
    if not s:
        return ''
    s = s.strip(' \t\r\n-—–:;,.')
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'\bD\.\s*C\.\b', 'DC', s, flags=re.I)
    return s.strip()


def looks_like_reasonable_location(text: str) -> bool:
    s = clean_ws(text)
    if not s:
        return False
    if len(s) < 3 or len(s) > 90:
        return False
    if ',' not in s:
        return False
    if any(ch.isdigit() for ch in s):
        return False
    if 'http' in s.lower() or '@' in s:
        return False
    if LOCATION_BAD_CUES.search(s):
        return False

    left, right = [x.strip() for x in s.split(',', 1)]
    if not left or not right:
        return False

    left_tokens = [t for t in re.split(r'\s+', left) if t]
    right_tokens = [t for t in re.split(r'\s+', right) if t]
    if not (1 <= len(left_tokens) <= 4):
        return False
    if not (1 <= len(right_tokens) <= 4):
        return False

    if LOCATION_STOPWORDS.search(left) or LOCATION_STOPWORDS.search(right):
        return False

    right_clean = re.sub(r'[^A-Za-z\s\.]', '', right).strip()
    right_compact = right_clean.replace('.', '').strip()
    if not right_compact:
        return False

    # Reject month/day tails like "..., January".
    if MONTH_OR_DAY_WORD.match(right_compact):
        return False

    # Require geographic-looking suffix: state abbrev or proper-case place phrase.
    right_upper = right_compact.upper()
    if right_upper in US_STATE_ABBREVS:
        return True

    if re.match(r'^[A-Z][A-Za-z\-]+(?:\s+[A-Z][A-Za-z\-]+){0,3}$', right_compact):
        return True

    return True


def extract_location_from_line(line: str) -> str:
    s = clean_ws(line)
    if not s:
        return ''

    # LOCATION: City, Country
    m = re.search(r'(?i)\bLOCATION\s*:\s*([^|\n]{3,120})', s)
    if m:
        cand = normalize_location_candidate(m.group(1))
        if looks_like_reasonable_location(cand):
            return cand

    # Venue, City, Country, Month DD, YYYY
    m = LOCATION_VENUE_CITY_COUNTRY_MONTH_DATE.search(s)
    if m:
        # Prefer City, Country for normalized geo consistency.
        cand = normalize_location_candidate(f'{m.group(2)}, {m.group(3)}')
        if looks_like_reasonable_location(cand):
            return cand

    # City, Region style, with optional trailing punctuation.
    m = LOCATION_CITY_REGION_MONTH_DATE.search(s)
    if m:
        cand = normalize_location_candidate(f'{m.group(1)}, {m.group(2)}')
        if looks_like_reasonable_location(cand):
            return cand

    # City-only dateline style: "New York City May 8, 2006"
    m = LOCATION_CITY_MONTH_DATE.search(s)
    if m:
        city = normalize_location_candidate(m.group(1))
        if city and len(city) <= 60 and not LOCATION_BAD_CUES.search(city) and not any(ch.isdigit() for ch in city):
            return city

    # City, Region style, with optional trailing punctuation.
    m = LOCATION_CITY_COMMA_REGION.search(s)
    if m:
        cand = normalize_location_candidate(f'{m.group(1)}, {m.group(2)}')
        if looks_like_reasonable_location(cand):
            return cand

    # Dateline style: "WASHINGTON, DC —"
    m = LOCATION_DATELINE_DASH.search(s)
    if m:
        cand = normalize_location_candidate(f'{m.group(1)}, {m.group(2)}')
        if looks_like_reasonable_location(cand):
            return cand

    return ''


def fallback_extract_location_from_raw_text(row: dict, allow_relaxed: bool = False) -> str:
    raw = (row.get('raw_text') or '')
    if not raw:
        return ''

    title = row.get('title', '') or ''
    meta = row.get('meta_description', '') or ''
    head = raw[:7000]
    lines = [clean_ws(x) for x in head.splitlines() if clean_ws(x)]
    if not lines:
        return ''

    # Prefer locations near event headers.
    header_span = 10 if allow_relaxed else 7
    for i, line in enumerate(lines):
        if not EVENT_HEADER_LINE_CUE.search(line):
            continue
        start = max(0, i - 3)
        end = min(len(lines), i + header_span)
        for j in range(start, end):
            cand = extract_location_from_line(lines[j])
            if cand:
                return cand

    # Then scan top section conservatively.
    event_context = bool(EVENT_CONTENT_CUE.search(' | '.join([title, meta, head[:1800]])))
    if not event_context:
        return ''

    top_scan = 80 if allow_relaxed else 45
    for line in lines[:top_scan]:
        cand = extract_location_from_line(line)
        if cand:
            return cand

    return ''


def looks_like_natural_speech(after_text: str) -> bool:
    """Heuristic check that real spoken text follows the match."""
    if not after_text:
        return False

    window = after_text[:900]
    if SENTENCE_AFTER_LABEL.search(window):
        return True

    first_line = window.splitlines()[0] if window.splitlines() else window
    words = ALPHA_WORD.findall(first_line)
    if len(words) >= 10 and (QUESTION_OR_TRANSCRIPT_CUE.search(window) or GENERIC_SPEAKER_CUE.search(window)):
        return True

    if QUESTION_OR_TRANSCRIPT_CUE.search(window):
        return True

    return False


def content_type_gate(row: dict, raw_text: str):
    """Require speech-like event/type cues and avoid obvious non-speech docs."""
    head = (raw_text or '')[:7000]
    haystack = ' | '.join([
        row.get('title', '') or '',
        row.get('meta_description', '') or '',
        row.get('url', '') or '',
        row.get('final_url', '') or '',
        head,
    ])
    pos = POSITIVE_CONTENT_TYPE.search(haystack)
    neg = NEGATIVE_CONTENT_TYPE.search(haystack)

    if not pos:
        return False, '', ''
    if neg:
        return False, pos.group(0), neg.group(0)
    return True, pos.group(0), ''


def safe_int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


def normalize_url_for_shape(url: str) -> str:
    u = (url or '').strip().lower()
    if not u:
        return ''
    u = u.split('#', 1)[0].split('?', 1)[0]
    return u


def bucketize_url_path(url: str) -> str:
    u = normalize_url_for_shape(url)
    if not u:
        return 'none'
    m = re.match(r'(?i)^https?://([^/]+)(/.*)?$', u)
    if not m:
        return 'none'
    host = (m.group(1) or '').lower()
    path = m.group(2) or '/'
    segs = [s for s in path.split('/') if s]
    if not segs:
        return f'{host}/'
    return f'{host}/' + '/'.join(segs[:2])


def extract_review_signal_tokens(row: dict):
    text = ' '.join([
        row.get('title', '') or '',
        row.get('meta_description', '') or '',
        row.get('listing_anchor_text', '') or '',
    ]).lower()
    toks = re.findall(r'[a-z]{4,}', text)
    return [t for t in toks if t not in REVIEW_TOKEN_STOPWORDS]


def infer_washington_dc_for_pa_row(row: dict) -> str:
    """Conservative location recovery for legacy /r/pa/ event pages with strong DC cues."""
    url = (row.get('final_url') or row.get('url') or '')
    if not PA_URL_CUE.search(url):
        return ''

    title = row.get('title', '') or ''
    meta = row.get('meta_description', '') or ''
    listing = row.get('listing_anchor_text', '') or ''
    head = (row.get('raw_text') or '')[:4000]
    hay = ' | '.join([title, meta, listing, head])

    if not EVENT_CONTENT_CUE.search(hay):
        return ''
    if not PA_EVENT_CUE.search(hay):
        return ''

    # Prefer explicit Washington/DC markers.
    if re.search(r'(?i)\bwashington(?:\s*,\s*d\.?c\.?|\s+d\.?c\.?)?\b', hay):
        return 'Washington, DC'

    # Backup cue pattern used on many PA pages.
    if re.search(r'(?i)\bdepartment\s+of\s+state\b', head):
        return 'Washington, DC'

    return ''


def infer_location_from_article_meta(row: dict) -> tuple[str, str]:
    """Use modern article-meta fields (location/doctype/author) before broad fallbacks."""
    loc = normalize_location_candidate(row.get('article_meta_location', '') or '')
    doc_type = clean_ws(row.get('article_meta_doctype', '') or '')
    author = clean_ws(row.get('article_meta_author', '') or '')

    if loc and not NON_PHYSICAL_LOCATION_CUE.search(loc):
        loc = re.sub(r'(?i)\bWashington\s*,\s*D\.?C\.?\b', 'Washington, DC', loc)
        return loc, 'filter_article_meta_location'

    # For non-physical or missing meta locations, use conservative DC fallback for secretary-authored event docs.
    is_secretary_authored = bool(re.search(r'(?i)\bsecretary\s+of\s+state\b', author))
    doc_eventy = bool(re.search(r'(?i)\b(press\s+statement|statement|interview|remarks?|remarks\s+to\s+the\s+press|press\s+availability)\b', doc_type))
    if is_secretary_authored and doc_eventy:
        return 'Washington, DC', 'filter_article_meta_fallback_washington_dc'

    return '', ''


def listing_page_cap_for_secretary(secretary: str, max_page: int) -> int:
    cap = SECRETARY_LISTING_PAGE_CAPS.get((secretary or '').upper())
    if cap is None:
        return max_page
    return min(max_page, cap)


def explicit_token_has_local_event_context(text: str, start: int, end: int) -> bool:
    local = text[max(0, start - 260): min(len(text), end + 360)]
    if QUESTION_OR_TRANSCRIPT_CUE.search(local):
        return True
    if GENERIC_SPEAKER_CUE.search(local):
        return True
    if EVENT_CONTENT_CUE.search(local):
        return True
    return False


def is_probable_non_event_template_page(row: dict) -> bool:
    url = normalize_url_for_shape((row.get('final_url') or row.get('url') or ''))
    if not url:
        return False
    if NON_EVENT_URL_PATH_CUE.search(url):
        return True

    title = (row.get('title') or '')
    meta = (row.get('meta_description') or '')
    head = (row.get('raw_text') or '')[:1800]
    hay = ' | '.join([title, meta, head])

    if NAV_BOILERPLATE_CUE.search(hay) and not EVENT_HEADER_LINE_CUE.search(head) and not QUESTION_OR_TRANSCRIPT_CUE.search(head):
        return True

    return False


def match_listing_lineage(secretary: str, value: str, max_page: int):
    v = (value or '').strip()
    if not v:
        return None
    effective_max_page = listing_page_cap_for_secretary(secretary, max_page)
    for rx in metadata_listing_regexes(secretary):
        m = rx.match(v)
        if not m:
            continue
        page = int(m.group(1)) if m.groups() and m.group(1) else 1
        if page <= effective_max_page:
            return {
                'url': v,
                'page': page,
                'rule_hit': rx.pattern,
            }
    return None


def classify_lineage_support(row: dict, secretary: str, max_page: int):
    discovered_from = (row.get('discovered_from') or '').strip()
    if discovered_from:
        m = match_listing_lineage(secretary, discovered_from, max_page)
        if m:
            return 'strong', f'discovered_from_listing_p{m["page"]}', discovered_from, m['rule_hit']

    for fld in ('url', 'final_url', 'original_url'):
        v = (row.get(fld) or '').strip()
        m = match_listing_lineage(secretary, v, max_page)
        if m:
            return 'medium', f'{fld}_listing_p{m["page"]}', discovered_from, m['rule_hit']

    if discovered_from and re.search(r'(?i)https?://(?:www\.)?(?:1997-2001|2001-2009|2009-2017|2017-2021|2021-2025|state)\.state\.gov', discovered_from):
        return 'weak', 'discovered_from_state_domain', discovered_from, 'state_domain_parent'

    return 'none', 'no_lineage_match', discovered_from, 'none'


def is_probable_listing_or_hub_page(row: dict) -> bool:
    """Conservative guard against index/listing/hub pages being treated as speech rows."""
    u = normalize_url_for_shape((row.get('final_url') or row.get('url') or ''))
    if not u:
        return False

    title = (row.get('title') or '')
    meta = (row.get('meta_description') or '')
    head = (row.get('raw_text') or '')[:1800]
    text_len = safe_int(row.get('text_len', 0), 0)

    event_context = bool(EVENT_CONTENT_CUE.search(' | '.join([title, meta, head])))

    # Strong listing/hub signatures.
    if re.search(r'(?i)/(?:tag|category|search)/', u):
        return True
    if re.search(r'(?i)/page/\d+/?$', u):
        return True

    # Index pages are common archives; treat as hub unless they look event-specific with substantial text.
    if u.endswith('/index.htm') or u.endswith('/index.html'):
        if not event_context:
            return True
        if text_len < 5000:
            return True

    # Site roots/home-like pages.
    if re.search(r'(?i)^https?://(?:www\.)?state\.gov/?$', u):
        return True
    if re.search(r'(?i)^https?://(?:1997-2001|2001-2009|2009-2017|2017-2021|2021-2025)\.state\.gov/?$', u):
        return True

    return False


def has_minimal_event_evidence_for_metadata_supported(row: dict) -> bool:
    """Conservative event-evidence requirement used only for metadata-supported token fallback rows."""
    title = row.get('title', '') or ''
    meta = row.get('meta_description', '') or ''
    listing_anchor = row.get('listing_anchor_text', '') or ''
    discovered_from = row.get('discovered_from', '') or ''
    url = row.get('url', '') or ''
    final_url = row.get('final_url', '') or ''
    head = (row.get('raw_text') or '')[:3500]

    event_haystack = ' | '.join([title, meta, listing_anchor, discovered_from, url, final_url, head])
    if EVENT_CONTENT_CUE.search(event_haystack):
        return True

    # Transcript-style cues can still indicate true event pages even when event keywording is weak.
    if QUESTION_OR_TRANSCRIPT_CUE.search(head) or GENERIC_SPEAKER_CUE.search(head):
        return True

    return False


def explicit_token_metadata_supports(secretary: str, row: dict, max_page: int = 9999) -> bool:
    """Fallback support when token hit is real but transcript cues are weak/noisy."""
    title = row.get('title', '') or ''
    meta = row.get('meta_description', '') or ''
    sec_inf = row.get('secretary_inferred', '') or ''
    discovered_from = row.get('discovered_from', '') or ''
    url = row.get('url', '') or ''
    final_url = row.get('final_url', '') or ''
    original_url = row.get('original_url', '') or ''

    # Strongest: secretary-specific listing lineage.
    lineage_tier, _, _, _ = classify_lineage_support(row, secretary, max_page)
    if lineage_tier == 'strong':
        return True

    # Secondary: secretary name + event cue in title/meta context.
    name_supported = (
        secretary_name_in_text(secretary, title)
        or secretary_name_in_text(secretary, meta)
        or secretary_name_in_text(secretary, sec_inf)
    )
    event_supported = bool(EVENT_CONTENT_CUE.search(' | '.join([title, meta, discovered_from, url, final_url])))

    if lineage_tier == 'medium' and event_supported:
        return True
    return bool(name_supported and event_supported)


def find_explicit_secretary_token(row: dict, enabled_secretaries):
    """Explicit token path, e.g., SECRETARY_BLINKEN or SECRETARY_OF_STATE_POMPEO."""
    text = (row.get('raw_text', '') or '')[:18000]
    if not text:
        return None

    for sec in enabled_secretaries:
        name_alt = '(?:' + '|'.join(SECRETARY_VARIANTS[sec]) + ')'
        token_pattern = re.compile(
            rf'(?im)\bSECRETARY(?:[_\s-]+OF[_\s-]+STATE)?[_\s-]+{name_alt}\b\s*(?::|[—–-])?'
        )
        m = token_pattern.search(text)
        if not m:
            continue
        after = text[m.end(): m.end() + 900]
        # Require natural-language transcript cues after token to avoid navbar/header false positives.
        natural_after = looks_like_natural_speech(after)
        local_event_ctx = explicit_token_has_local_event_context(text, m.start(), m.end())
        local_nav_ctx = NAV_BOILERPLATE_CUE.search(text[max(0, m.start() - 260): min(len(text), m.end() + 360)])
        if natural_after and local_nav_ctx and not local_event_ctx:
            natural_after = False
        if not natural_after and not explicit_token_metadata_supports(sec, row):
            continue

        excerpt = text[m.start(): m.end() + 220].replace('\n', ' ').strip()
        return {
            'secretary': sec,
            'match_type': 'explicit_secretary_token' if natural_after else 'explicit_secretary_token_metadata_supported',
            'match_confidence': 'high' if natural_after else 'medium',
            'source': 'explicit_token_rule',
            'speaker_excerpt': excerpt[:320],
            'acceptance_path': 'explicit_secretary_token',
        }

    return None


def secretary_supported_by_metadata(sec: str, row: dict) -> bool:
    for field in ('secretary_inferred', 'title', 'meta_description', 'final_url', 'url'):
        val = (row.get(field) or '')
        if re.search(rf'(?i)\b{re.escape(sec)}\b', val):
            return True
    return False


def build_compiled_rules(enabled_secretaries):
    compiled = {}
    for sec, variants in SECRETARY_VARIANTS.items():
        if sec not in enabled_secretaries:
            continue
        name_alt = '(?:' + '|'.join(variants) + ')'
        last_name = variants[0]
        sec_rules = []
        for spec in RULES:
            pattern = spec['pattern_template'].replace('__NAME_ALT__', name_alt).replace('__LAST_NAME__', last_name)
            sec_rules.append({
                'rule': spec['rule'],
                'confidence': spec['confidence'],
                'needs_natural_after': spec.get('needs_natural_after', False),
                'needs_metadata_support': spec.get('needs_metadata_support', False),
                'regex': re.compile(pattern),
            })
        compiled[sec] = sec_rules
    return compiled


def find_secretary_speaker(row: dict, compiled_rules):
    text = row.get('raw_text', '') or ''
    if not text:
        return None

    head = text[:18000]

    for sec, rules in compiled_rules.items():
        for rule in rules:
            match = rule['regex'].search(head)
            if not match:
                continue

            if rule['needs_metadata_support'] and not secretary_supported_by_metadata(sec, row):
                continue

            after = head[match.end(): match.end() + 1000]
            if rule['needs_natural_after'] and not looks_like_natural_speech(after):
                continue

            excerpt = head[match.start(): match.end() + 220].replace('\n', ' ').strip()
            return {
                'secretary': sec,
                'match_type': rule['rule'],
                'match_confidence': rule['confidence'],
                'source': 'transcript_rule',
                'speaker_excerpt': excerpt[:320],
            }

    return None


def confidence_allowed(found_conf: str, min_conf: str) -> bool:
    return CONF_LEVEL[found_conf] >= CONF_LEVEL[min_conf]


def parse_args():
    parser = argparse.ArgumentParser(description='Filter rows where secretary is likely speaker.')
    parser.add_argument('--min-confidence', choices=['low', 'medium', 'high'], default='low')
    parser.add_argument(
        '--date-window-policy',
        choices=['off', 'drop_mismatch', 'require_known_valid'],
        default='drop_mismatch',
        help='off=skip date-window check; drop_mismatch=keep unknown but drop mismatches; require_known_valid=keep only known-valid',
    )
    parser.add_argument(
        '--include-metadata-candidates',
        action='store_true',
        help='Also include metadata-derived secretary candidates (title/url/discovered_from).',
    )
    parser.add_argument(
        '--metadata-max-page',
        type=int,
        default=49,
        help='Max paginated listing page number to include for metadata listing URL matches.',
    )
    parser.add_argument(
        '--metadata-secretaries',
        default='ALL',
        help='Comma list of secretaries for metadata matching, or ALL.',
    )
    parser.add_argument(
        '--metadata-require-discovered-from',
        action='store_true',
        help='Require discovered_from to match a secretary listing URL family for metadata-derived matches.',
    )
    parser.add_argument(
        '--workers',
        type=int,
        default=max(1, (os.cpu_count()) - 1),
        help='Number of worker processes for row filtering. Use 1 for single-process mode.',
    )
    parser.add_argument(
        '--chunk-size',
        type=int,
        default=50,
        help='Number of rows per task chunk when --workers > 1.',
    )
    return parser.parse_args()


def parse_secretary_selection(selection: str):
    if not selection or selection.strip().upper() == 'ALL':
        return SECRETARY_ORDER
    chosen = {x.strip().upper() for x in selection.split(',') if x.strip()}
    unknown = sorted(chosen - set(SECRETARY_VARIANTS.keys()))
    if unknown:
        raise ValueError(f'Unknown secretary names in --metadata-secretaries: {unknown}')
    return tuple(sec for sec in SECRETARY_ORDER if sec in chosen)


def metadata_listing_regexes(secretary: str):
    sec = secretary.upper()
    if sec in METADATA_LISTING_REGEX_CACHE:
        return METADATA_LISTING_REGEX_CACHE[sec]

    patterns = []

    # Modern listing families with pagination, aligned to scraper seed URLs.
    if sec in MODERN_LISTING_SLUGS:
        slug = MODERN_LISTING_SLUGS[sec]
        domain = r'https?://(?:2017-2021|2021-2025)\.state\.gov' if sec in {'TILLERSON', 'POMPEO', 'BLINKEN'} else r'https?://www\.state\.gov'
        patterns.append(re.compile(rf'{domain}/{re.escape(slug)}(?:/page/(\d+))?/?$', re.I))

    # Legacy listing families by archive era.
    if sec == 'ALBRIGHT':
        patterns.append(re.compile(r'https?://1997-2001\.state\.gov/statements/(?:index\.html|\d{4}_index\.html)$', re.I))
    elif sec == 'POWELL':
        patterns.append(re.compile(r'https?://2001-2009\.state\.gov/secretary/former/powell/remarks/(?:index\.htm|\d{4}/index\.htm)$', re.I))
    elif sec == 'RICE':
        patterns.append(re.compile(r'https?://2001-2009\.state\.gov/secretary/rm/(?:index\.htm|\d{4}/index\.htm)$', re.I))
    elif sec == 'CLINTON':
        patterns.append(re.compile(
            r'https?://2009-2017\.state\.gov/secretary/20092013clinton/rm/'
            r'(?:index\.htm|\d{4}[a-z]?/index\.htm|\d{4}/\d{2}/index\.htm)$',
            re.I,
        ))
    elif sec == 'KERRY':
        patterns.append(re.compile(
            r'https?://2009-2017\.state\.gov/secretary/remarks/'
            r'(?:index\.htm|\d{4}[a-z]?/index\.htm|\d{4}/\d{2}/index\.htm)$',
            re.I,
        ))

    METADATA_LISTING_REGEX_CACHE[sec] = patterns
    return patterns


def secretary_name_in_text(secretary: str, text: str) -> bool:
    return bool(re.search(rf'(?i)\b(?:{"|".join(SECRETARY_VARIANTS[secretary])})\b', text or ''))


def metadata_consistency_supports(secretary: str, fields: dict) -> bool:
    # Ensure metadata match is not a loose cross-mention.
    if secretary_name_in_text(secretary, fields.get('secretary_inferred', '')):
        return True
    for fld in ('title', 'meta_description'):
        if secretary_name_in_text(secretary, fields.get(fld, '')):
            return True
    return False


def find_secretary_by_metadata(row: dict, enabled_secretaries, max_page: int, require_discovered_from: bool):
    fields = {
        'title': row.get('title', '') or '',
        'meta_description': row.get('meta_description', '') or '',
        'url': row.get('url', '') or '',
        'final_url': row.get('final_url', '') or '',
        'original_url': row.get('original_url', '') or '',
        'discovered_from': row.get('discovered_from', '') or '',
        'secretary_inferred': row.get('secretary_inferred', '') or '',
        'listing_anchor_text': row.get('listing_anchor_text', '') or '',
    }
    raw_head = (row.get('raw_text', '') or '')[:3200]

    for sec in enabled_secretaries:
        effective_max_page = listing_page_cap_for_secretary(sec, max_page)
        lineage_tier, _, _, _ = classify_lineage_support(row, sec, max_page)
        event_supported = bool(EVENT_CONTENT_CUE.search(' | '.join([
            fields['title'],
            fields['meta_description'],
            fields['listing_anchor_text'],
            fields['discovered_from'],
            fields['url'],
            fields['final_url'],
        ])))
        transcript_supported = bool(QUESTION_OR_TRANSCRIPT_CUE.search(raw_head) or GENERIC_SPEAKER_CUE.search(raw_head))

        listing_hit = None
        listing_hit_field = None
        listing_hit_page = None
        for rx in metadata_listing_regexes(sec):
            for fld in ('discovered_from', 'url', 'final_url', 'original_url'):
                v = fields[fld].strip()
                m = rx.match(v)
                if not m:
                    continue
                page = int(m.group(1)) if m.groups() and m.group(1) else 1
                if page <= effective_max_page:
                    listing_hit = v
                    listing_hit_field = fld
                    listing_hit_page = page
                    break
            if listing_hit:
                break

        # strongest metadata evidence: discovered_from listing lineage.
        if listing_hit and listing_hit_field == 'discovered_from':
            return {
                'secretary': sec,
                'match_type': f'metadata_listing_discovered_from_p{listing_hit_page}',
                'match_confidence': 'medium',
                'source': 'metadata_rule',
                'speaker_excerpt': listing_hit[:320],
            }

        # optionally require discovered_from listing lineage for metadata-based acceptance.
        if require_discovered_from:
            continue

        # secondary metadata evidence from other listing-like URL fields + consistency checks.
        if (
            listing_hit
            and listing_hit_field in {'url', 'final_url', 'original_url'}
            and metadata_consistency_supports(sec, fields)
            and event_supported
            and lineage_tier in {'strong', 'medium'}
        ):
            return {
                'secretary': sec,
                'match_type': f'metadata_listing_{listing_hit_field}',
                'match_confidence': 'low',
                'source': 'metadata_rule',
                'speaker_excerpt': listing_hit[:320],
            }

        # fallback explicit name mention in metadata fields (lowest confidence, needs consistency).
        for fld in ('title', 'meta_description', 'url', 'final_url', 'original_url', 'discovered_from', 'secretary_inferred'):
            if (
                secretary_name_in_text(sec, fields[fld])
                and metadata_consistency_supports(sec, fields)
                and event_supported
                and (lineage_tier in {'strong', 'medium'} or transcript_supported)
            ):
                excerpt = fields[fld].strip().replace('\n', ' ')
                return {
                    'secretary': sec,
                    'match_type': f'metadata_name_{fld}',
                    'match_confidence': 'low',
                    'source': 'metadata_rule',
                    'speaker_excerpt': excerpt[:320],
                }

    return None


def parse_date_ymd(y: int, m: int, d: int):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_month_day_year(month_text: str, d: str, y: str):
    try:
        dt = datetime.strptime(f'{month_text} {d} {y}', '%B %d %Y').date()
        return dt
    except ValueError:
        try:
            dt = datetime.strptime(f'{month_text} {d} {y}', '%b %d %Y').date()
            return dt
        except ValueError:
            return None


def parse_unambiguous_slash_date(a: str, b: str, y: str):
    """Parse slash dates only when MM/DD vs DD/MM is unambiguous."""
    try:
        p1, p2, year = int(a), int(b), int(y)
    except ValueError:
        return None

    if p1 > 12 and 1 <= p2 <= 12:
        day, month = p1, p2
    elif p2 > 12 and 1 <= p1 <= 12:
        month, day = p1, p2
    else:
        return None

    return parse_date_ymd(year, month, day)


def extract_exact_date_from_text_fragment(text: str):
    """Extract an exact date from a short text fragment, if present."""
    if not text:
        return None

    m = TEXT_MONTH_DY.search(text)
    if m:
        dt = parse_month_day_year(m.group(1), m.group(2), m.group(3))
        if dt:
            return dt

    m = TEXT_MONTH_DY_NOCOMMA.search(text)
    if m:
        dt = parse_month_day_year(m.group(1), m.group(2), m.group(3))
        if dt:
            return dt

    m = TEXT_D_MONTH_Y.search(text)
    if m:
        dt = parse_month_day_year(m.group(2), m.group(1), m.group(3))
        if dt:
            return dt

    m = TEXT_YMD.search(text)
    if m:
        dt = parse_date_ymd(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if dt:
            return dt

    m = TEXT_SLASH_DATE.search(text)
    if m:
        dt = parse_unambiguous_slash_date(m.group(1), m.group(2), m.group(3))
        if dt:
            return dt

    return None


def extract_event_date_from_row(row: dict):
    """Extract likely speech-event date from event-context title/meta/raw header blocks."""
    title = (row.get('title') or '').strip()
    meta = (row.get('meta_description') or '').strip()
    raw = (row.get('raw_text') or '')

    # Event-like title/subtitle with embedded exact date.
    if title and EVENT_CONTENT_CUE.search(title):
        dt = extract_exact_date_from_text_fragment(title)
        if dt:
            return dt, 'title_event_date'

    if meta and EVENT_CONTENT_CUE.search(meta):
        dt = extract_exact_date_from_text_fragment(meta)
        if dt:
            return dt, 'meta_event_date'

    listing_anchor = (row.get('listing_anchor_text') or '').strip()
    if listing_anchor and EVENT_CONTENT_CUE.search(' | '.join([title, meta, listing_anchor])):
        dt = extract_exact_date_from_text_fragment(listing_anchor)
        if dt:
            return dt, 'listing_anchor_event_date'

    # Event header zone in raw text (modern speech pages often place date near top headers).
    header_zone = raw[:4200]
    if not header_zone:
        return None, 'none'

    lines = [ln.strip() for ln in header_zone.splitlines() if ln and ln.strip()]
    for i, line in enumerate(lines):
        if not EVENT_HEADER_LINE_CUE.search(line):
            continue
        start = max(0, i - 2)
        end = min(len(lines), i + 11)
        for j in range(start, end):
            dt = extract_exact_date_from_text_fragment(lines[j])
            if dt:
                return dt, 'raw_text_event_header_date'

    return None, 'none'


def extract_metadata_best_date(row: dict):
    """Use metadata only (no date_inferred)."""
    url_blob = ' '.join([(row.get('final_url') or ''), (row.get('url') or '')])
    title = row.get('title', '') or ''
    meta = row.get('meta_description', '') or ''

    event_context_blob = ' | '.join([
        title,
        meta,
        row.get('listing_anchor_text', '') or '',
        row.get('discovered_from', '') or '',
        row.get('url', '') or '',
        row.get('final_url', '') or '',
    ])
    has_event_context = bool(EVENT_CONTENT_CUE.search(event_context_blob))

    # Prefer modern structured metadata fields when page/listing context appears event-like.
    if has_event_context:
        structured_priority = [
            ('listing_anchor_date', 'metadata_listing_anchor_date'),
            ('jsonld_start_date', 'metadata_jsonld_start_date'),
            ('jsonld_date_published', 'metadata_jsonld_date_published'),
            ('jsonld_date_created', 'metadata_jsonld_date_created'),
            ('time_tag_datetime', 'metadata_time_tag_datetime'),
            ('og_article_published_time', 'metadata_og_article_published_time'),
            ('article_published_time', 'metadata_article_published_time'),
        ]
        for field, source in structured_priority:
            dt = parse_date_inferred_value(row.get(field, '') or '')
            if dt:
                return dt, source

    # URL first
    for rx in (URL_PATH_YMD, URL_YMD, URL_COMPACT_YMD):
        m = rx.search(url_blob)
        if m:
            y, mo, da = int(m.group(1)), int(m.group(2)), int(m.group(3))
            dt = parse_date_ymd(y, mo, da)
            if dt:
                return dt, 'url_exact'

    # title/meta date mentions
    for text, source in ((title, 'title_date'), (meta, 'meta_date')):
        m = TEXT_MONTH_DY.search(text)
        if m:
            dt = parse_month_day_year(m.group(1), m.group(2), m.group(3))
            if dt:
                return dt, source
        m = TEXT_D_MONTH_Y.search(text)
        if m:
            dt = parse_month_day_year(m.group(2), m.group(1), m.group(3))
            if dt:
                return dt, source
        m = TEXT_YMD.search(text)
        if m:
            dt = parse_date_ymd(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if dt:
                return dt, source
        m = TEXT_SLASH_DATE.search(text)
        if m:
            dt = parse_unambiguous_slash_date(m.group(1), m.group(2), m.group(3))
            if dt:
                return dt, source

    return None, 'none'


def parse_date_inferred_value(value: str):
    s = (value or '').strip()
    if not s:
        return None
    # keep this conservative and robust
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(s[:19], fmt).date()
        except ValueError:
            continue
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        return parse_date_ymd(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def extract_raw_text_date(row: dict):
    raw = (row.get('raw_text') or '')[:6000]
    if not raw:
        return None, 'none'

    # Header-zone exact-date scan for label-based lines, conservative and exact only.
    header_zone = raw[:1500]
    for m in HEADER_ZONE_LABELLED_DATE.finditer(header_zone):
        dt = extract_exact_date_from_text_fragment(m.group(1))
        if dt:
            return dt, 'raw_text_header_labelled_date'

    m = RAW_MONTH_DY.search(raw)
    if m:
        dt = parse_month_day_year(m.group(1), m.group(2), m.group(3))
        if dt:
            return dt, 'raw_text_header_month_day_year'
    m = TEXT_D_MONTH_Y.search(raw)
    if m:
        dt = parse_month_day_year(m.group(2), m.group(1), m.group(3))
        if dt:
            return dt, 'raw_text_header_day_month_year'
    m = TEXT_YMD.search(raw)
    if m:
        dt = parse_date_ymd(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if dt:
            return dt, 'raw_text_header_ymd'
    m = TEXT_SLASH_DATE.search(raw)
    if m:
        dt = parse_unambiguous_slash_date(m.group(1), m.group(2), m.group(3))
        if dt:
            return dt, 'raw_text_header_unambiguous_slash_date'
    return None, 'none'


def extract_legacy_statement_url_date(row: dict):
    for fld in ('final_url', 'url', 'original_url'):
        u = (row.get(fld) or '').strip()
        m = LEGACY_STMT_YYMMDD.search(u)
        if not m:
            continue
        y2 = int(m.group(1)[:2])
        mo = int(m.group(1)[2:4])
        da = int(m.group(1)[4:6])
        # Legacy corpus span uses 1990s/2000s for these short forms.
        year = 1900 + y2 if y2 >= 90 else 2000 + y2
        dt = parse_date_ymd(year, mo, da)
        if dt:
            return dt, f'legacy_statement_url_{fld}'
    return None, 'none'


def extract_legacy_briefing_url_date(row: dict):
    """Parse dates from legacy briefing URL filenames like /briefings/0004/000424db.html."""
    for fld in ('final_url', 'url', 'original_url'):
        u = (row.get(fld) or '').strip()
        m = LEGACY_BRIEFING_YYMMDD.search(u)
        if not m:
            continue

        y2 = int(m.group(1)[:2])
        mo = int(m.group(1)[2:4])
        da = int(m.group(1)[4:6])

        # Albright-era archive pages are in 1997-2001.
        if y2 >= 97:
            year = 1900 + y2
        else:
            year = 2000 + y2

        dt = parse_date_ymd(year, mo, da)
        if dt:
            return dt, f'legacy_briefing_url_{fld}'

    return None, 'none'


def extract_legacy_filename_token_date(row: dict):
    """Parse legacy Albright-era YYMMDD tokens embedded in filename stems."""
    for fld in ('final_url', 'url', 'original_url'):
        u = (row.get(fld) or '').strip()
        if not u:
            continue

        path_part = u.rsplit('/', 1)[-1]
        stem = path_part.split('?', 1)[0].split('#', 1)[0].split('.', 1)[0]
        m = LEGACY_FILENAME_YYMMDD.search(stem)
        if not m:
            continue

        y2 = int(m.group(1)[:2])
        mo = int(m.group(1)[2:4])
        da = int(m.group(1)[4:6])

        # Constrain to Albright-era legacy year mapping only.
        if y2 >= 97:
            year = 1900 + y2
        elif y2 <= 1:
            year = 2000 + y2
        else:
            continue

        dt = parse_date_ymd(year, mo, da)
        if dt:
            return dt, f'legacy_filename_token_{fld}'

    return None, 'none'


def extract_exact_date_with_confidence(row: dict, secretary: str):
    # 0) event-context exact date (speech header/title/meta cues)
    event_dt, event_source = extract_event_date_from_row(row)
    if event_dt:
        return event_dt, event_source, 'high'

    # 1) date_inferred from scrape parse (strongest when present)
    inferred = parse_date_inferred_value(row.get('date_inferred', '') or '')
    if inferred:
        return inferred, 'date_inferred', 'high'

    # 2) metadata-derived date
    md_dt, md_source = extract_metadata_best_date(row)
    if md_dt:
        conf = 'high' if md_source == 'url_exact' else 'medium'
        return md_dt, md_source, conf

    # 3) legacy short-date statement URLs, e.g. /briefings/statements/970730d.html
    legacy_dt, legacy_source = extract_legacy_statement_url_date(row)
    if legacy_dt:
        return legacy_dt, legacy_source, 'medium'

    # 3b) legacy Albright-era briefing URLs, e.g. /briefings/0004/000424db.html
    if secretary == 'ALBRIGHT':
        legacy_brief_dt, legacy_brief_source = extract_legacy_briefing_url_date(row)
        if legacy_brief_dt:
            return legacy_brief_dt, legacy_brief_source, 'medium'

        # 3c) broader legacy filename token dates, e.g. ps990309a.html / 980123_topic.html
        legacy_token_dt, legacy_token_source = extract_legacy_filename_token_date(row)
        if legacy_token_dt:
            return legacy_token_dt, legacy_token_source, 'medium'

    # 4) raw text fallback
    raw_dt, raw_source = extract_raw_text_date(row)
    if raw_dt:
        return raw_dt, raw_source, 'low'

    return None, 'none', 'low'


def evaluate_date_window(secretary: str, dt):
    if not dt:
        return 'unknown', 'no_metadata_date'
    start, end = APPOINTMENT_WINDOWS.get(secretary, (None, None))
    if not start:
        return 'unknown', 'no_window_defined'
    if start <= dt <= end:
        return 'yes', f'within_window:{start.isoformat()}..{end.isoformat()}'
    return 'no', f'outside_window:{start.isoformat()}..{end.isoformat()}'


def should_keep_by_date_policy(status: str, policy: str) -> bool:
    if policy == 'off':
        return True
    if policy == 'drop_mismatch':
        return status != 'no'
    if policy == 'require_known_valid':
        return status == 'yes'
    return True


def classify_date_quality(exact_date: str, metadata_date: str, exact_source: str, metadata_source: str) -> str:
    if not (exact_date or metadata_date):
        return 'none'

    src = exact_source if exact_date else metadata_source
    high_sources = {
        'date_inferred',
        'raw_text_event_header_date',
        'metadata_jsonld_start_date',
        'metadata_jsonld_date_published',
        'metadata_jsonld_date_created',
        'metadata_time_tag_datetime',
        'metadata_og_article_published_time',
        'metadata_article_published_time',
        'url_exact',
    }
    medium_sources = {
        'title_event_date',
        'meta_event_date',
        'raw_text_header_month_day_year',
        'raw_text_header_day_month_year',
        'raw_text_header_ymd',
        'title_date',
        'meta_date',
        'legacy_statement_url_final_url',
        'legacy_statement_url_url',
        'legacy_statement_url_original_url',
        'legacy_briefing_url_final_url',
        'legacy_briefing_url_url',
        'legacy_briefing_url_original_url',
        'legacy_filename_token_final_url',
        'legacy_filename_token_url',
        'legacy_filename_token_original_url',
        'metadata_listing_anchor_date',
    }

    if src in high_sources:
        return 'high'
    if src in medium_sources:
        return 'medium'
    return 'low'


def classify_location_quality(location_best: str, location_source: str) -> str:
    if not location_best:
        return 'none'

    if location_source in {'jsonld_location_name', 'jsonld_content_location', 'meta_geo_placename'}:
        return 'high'
    if location_source in {'raw_dateline_location', 'filter_raw_location_fallback', 'filter_article_meta_location'}:
        return 'medium'
    if location_source in {'listing_anchor_location'}:
        return 'low'
    return 'low'


def derive_qa_flag_and_reason(date_quality: str, location_quality: str) -> tuple[str, str]:
    if date_quality in {'high', 'medium'} and location_quality in {'high', 'medium'}:
        return 'good', 'none'
    if date_quality == 'none' and location_quality == 'none':
        return 'review', 'both_missing'
    if date_quality == 'none':
        return 'review', 'missing_date'
    if location_quality == 'none':
        return 'review', 'missing_location'
    if date_quality == 'low' and location_quality == 'low':
        return 'review', 'weak_both_sources'
    if date_quality == 'low':
        return 'review', 'weak_date_source'
    if location_quality == 'low':
        return 'review', 'weak_location_source'
    return 'review', 'mixed_quality'


def chunked_iterable(iterable, chunk_size: int):
    chunk = []
    for item in iterable:
        chunk.append(item)
        if len(chunk) >= chunk_size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def make_worker_config(args, enabled_secretaries):
    return {
        'min_conf': args.min_confidence,
        'date_window_policy': args.date_window_policy,
        'include_metadata_candidates': bool(args.include_metadata_candidates),
        'metadata_max_page': int(args.metadata_max_page),
        'metadata_require_discovered_from': bool(args.metadata_require_discovered_from),
        'enabled_secretaries': tuple(enabled_secretaries),
    }


def init_worker(config: dict):
    global WORKER_CONFIG, WORKER_COMPILED_RULES, WORKER_ENABLED_SECRETARIES
    WORKER_CONFIG = config
    WORKER_ENABLED_SECRETARIES = tuple(config['enabled_secretaries'])
    WORKER_COMPILED_RULES = build_compiled_rules(WORKER_ENABLED_SECRETARIES)


def process_single_row(row: dict, config: dict, compiled_rules: dict, enabled_secretaries):
    stats = Counter()
    raw_text = row.get('raw_text', '') or ''

    result = None
    gate_pos = ''
    gate_neg = ''
    gate_passed = ''
    if raw_text:
        # Path B: explicit SECRETARY_{NAME}-style speaker token.
        result = find_explicit_secretary_token(row, enabled_secretaries)
        if not result:
            # Path A candidate: secretary speaking cue + event-type gate.
            result = find_secretary_speaker(row, compiled_rules)
    else:
        stats['missing_text_rows'] += 1

    if not result and config['include_metadata_candidates']:
        result = find_secretary_by_metadata(
            row,
            enabled_secretaries,
            config['metadata_max_page'],
            config['metadata_require_discovered_from'],
        )

    if not result:
        return None, stats

    if is_probable_non_event_template_page(row):
        if result.get('match_type') in {'explicit_secretary_token', 'explicit_secretary_token_metadata_supported'}:
            if not has_minimal_event_evidence_for_metadata_supported(row):
                stats['gate_rejection:rejected_non_event_template_explicit_token'] += 1
                return None, stats

    lineage_tier, lineage_reason, lineage_parent_url, lineage_rule_hit = classify_lineage_support(
        row,
        result['secretary'],
        config['metadata_max_page'],
    )

    # Do not let metadata-supported/metadata-only paths admit obvious listing/hub pages.
    if is_probable_listing_or_hub_page(row):
        if result.get('source') == 'metadata_rule' or result.get('match_type') == 'explicit_secretary_token_metadata_supported':
            eventish_anchor = bool(EVENT_CONTENT_CUE.search(' | '.join([
                row.get('listing_anchor_text', '') or '',
                row.get('title', '') or '',
                row.get('meta_description', '') or '',
            ])))
            if not (lineage_tier == 'strong' and eventish_anchor and has_minimal_event_evidence_for_metadata_supported(row)):
                stats['gate_rejection:rejected_listing_or_hub_candidate'] += 1
                return None, stats
            stats['lineage_override:hub_reject_overridden'] += 1

    # Minimal row-loss pass: tighten only metadata-supported explicit-token fallback rows.
    if result.get('match_type') == 'explicit_secretary_token_metadata_supported':
        if not has_minimal_event_evidence_for_metadata_supported(row):
            stats['gate_rejection:rejected_metadata_supported_without_event_evidence'] += 1
            return None, stats

    if result.get('acceptance_path') != 'explicit_secretary_token':
        passed_gate, gate_pos, gate_neg = content_type_gate(row, raw_text)
        gate_passed = 'yes' if passed_gate else 'no'
        if not passed_gate:
            stats['gate_rejection:failed_content_type_gate'] += 1
            if not gate_pos:
                stats['gate_rejection:missing_positive_content_type'] += 1
            if gate_neg:
                stats['gate_rejection:hit_negative_content_type'] += 1
            return None, stats
        result['acceptance_path'] = 'speech_event_plus_speaker'
    else:
        gate_passed = 'bypassed_explicit_token'

    if not confidence_allowed(result['match_confidence'], config['min_conf']):
        return None, stats

    exact_dt, exact_source, exact_conf = extract_exact_date_with_confidence(row, result['secretary'])
    metadata_dt, metadata_source = extract_metadata_best_date(row)
    # date window evaluates against best available exact date first
    dw_status, dw_note = evaluate_date_window(result['secretary'], exact_dt or metadata_dt)
    if not should_keep_by_date_policy(dw_status, config['date_window_policy']):
        stats['dropped_by_date_policy'] += 1
        return None, stats

    row_out = dict(row)
    row_out['certain_speaker_secretary'] = result['secretary']
    row_out['certain_speaker_match_type'] = result['match_type']
    row_out['certain_speaker_match_confidence'] = result['match_confidence']
    row_out['certain_speaker_source'] = result.get('source', 'transcript_rule')
    row_out['exact_date'] = exact_dt.isoformat() if exact_dt else ''
    row_out['exact_date_source'] = exact_source
    row_out['exact_date_confidence'] = exact_conf
    row_out['metadata_best_date'] = metadata_dt.isoformat() if metadata_dt else ''
    row_out['metadata_best_date_source'] = metadata_source
    row_out['date_window_valid'] = dw_status
    row_out['date_window_note'] = dw_note
    row_out['content_type_gate_passed'] = gate_passed
    row_out['content_type_positive_hit'] = gate_pos
    row_out['content_type_negative_hit'] = gate_neg
    row_out['acceptance_path'] = result.get('acceptance_path', 'speech_event_plus_speaker')
    row_out['certain_speaker_excerpt'] = result['speaker_excerpt']
    row_out['lineage_support_tier'] = lineage_tier
    row_out['lineage_support_reason'] = lineage_reason
    row_out['lineage_parent_url'] = lineage_parent_url
    row_out['lineage_rule_hit'] = lineage_rule_hit

    location_best = (row.get('event_location_best') or '').strip()
    location_source = (row.get('event_location_source') or '').strip()
    # Filter-only location enhancement fallback (only for accepted rows).
    if not location_best or location_source == 'listing_anchor_location':
        fallback_loc = fallback_extract_location_from_raw_text(row, allow_relaxed=(lineage_tier == 'strong'))
        if fallback_loc:
            location_best = fallback_loc
            location_source = 'filter_raw_location_fallback'
            row_out['event_location_best'] = location_best
            row_out['event_location_source'] = location_source
    if not location_best:
        meta_loc, meta_src = infer_location_from_article_meta(row)
        if meta_loc:
            location_best = meta_loc
            location_source = meta_src
            row_out['event_location_best'] = location_best
            row_out['event_location_source'] = location_source
    if not location_best:
        pa_loc = infer_washington_dc_for_pa_row(row)
        if pa_loc:
            location_best = pa_loc
            location_source = 'filter_pa_inferred_washington_dc'
            row_out['event_location_best'] = location_best
            row_out['event_location_source'] = location_source
    date_quality = classify_date_quality(row_out['exact_date'], row_out['metadata_best_date'], exact_source, metadata_source)
    location_quality = classify_location_quality(location_best, location_source)
    qa_flag, qa_reason = derive_qa_flag_and_reason(date_quality, location_quality)

    row_out['date_quality_tier'] = date_quality
    row_out['location_quality_tier'] = location_quality
    row_out['qa_quality_flag'] = qa_flag
    row_out['qa_failure_reason'] = qa_reason

    kept_meta = {
        'secretary': result['secretary'],
        'match_type': result['match_type'],
        'match_confidence': result['match_confidence'],
        'source': result.get('source', 'transcript_rule'),
        'acceptance_path': result.get('acceptance_path', 'speech_event_plus_speaker'),
        'dw_status': dw_status,
        'qa_flag': qa_flag,
        'qa_reason': qa_reason,
        'date_quality': date_quality,
        'location_quality': location_quality,
        'exact_source': exact_source,
        'metadata_source': metadata_source,
        'location_source': location_source or 'none',
        'url': (row.get('final_url') or row.get('url') or '').strip(),
        'has_any_date': bool(row_out['exact_date'] or row_out['metadata_best_date']),
        'has_location': bool(location_best),
        'lineage_tier': lineage_tier,
        'lineage_reason': lineage_reason,
    }
    return {'row_out': row_out, 'meta': kept_meta}, stats


def process_chunk_rows(rows, config: dict, compiled_rules: dict, enabled_secretaries):
    out = []
    aggregate_stats = Counter()
    aggregate_stats['input_rows'] = len(rows)
    for row in rows:
        kept_payload, row_stats = process_single_row(row, config, compiled_rules, enabled_secretaries)
        aggregate_stats.update(row_stats)
        if kept_payload:
            out.append(kept_payload)
    return out, dict(aggregate_stats)


def process_chunk_worker(rows):
    return process_chunk_rows(rows, WORKER_CONFIG, WORKER_COMPILED_RULES, WORKER_ENABLED_SECRETARIES)


def ingest_kept_payload(payload, writer, sec_counts, rule_counts, conf_counts, date_window_counts,
                        source_counts, acceptance_path_counts, lineage_tier_counts, lineage_reason_counts,
                        qa_flag_counts, qa_reason_counts,
                        date_quality_counts, location_quality_counts, review_date_source_counts,
                        review_location_source_counts, sec_quality_stats, failure_samples,
                        review_distribution):
    row_out = payload['row_out']
    meta = payload['meta']

    writer.writerow(row_out)

    sec = meta['secretary']
    sec_counts[sec] += 1
    rule_counts[meta['match_type']] += 1
    conf_counts[meta['match_confidence']] += 1
    date_window_counts[meta['dw_status']] += 1
    source_counts[meta['source']] += 1
    acceptance_path_counts[meta['acceptance_path']] += 1
    lineage_tier_counts[meta.get('lineage_tier', 'none')] += 1
    lineage_reason_counts[meta.get('lineage_reason', 'none')] += 1

    qa_flag = meta['qa_flag']
    qa_reason = meta['qa_reason']
    date_quality = meta['date_quality']
    location_quality = meta['location_quality']

    qa_flag_counts[qa_flag] += 1
    qa_reason_counts[qa_reason] += 1
    date_quality_counts[date_quality] += 1
    location_quality_counts[location_quality] += 1

    if sec not in sec_quality_stats:
        sec_quality_stats[sec] = Counter()
    sec_quality_stats[sec]['total'] += 1
    if meta['has_any_date']:
        sec_quality_stats[sec]['date_any'] += 1
    if meta['has_location']:
        sec_quality_stats[sec]['location_any'] += 1
    if meta['has_any_date'] and meta['has_location']:
        sec_quality_stats[sec]['date_and_location'] += 1
    sec_quality_stats[sec][f'date_quality_{date_quality}'] += 1
    sec_quality_stats[sec][f'location_quality_{location_quality}'] += 1
    sec_quality_stats[sec][f'qa_reason_{qa_reason}'] += 1

    if qa_flag == 'review':
        review_date_source_counts[meta['exact_source'] if row_out['exact_date'] else meta['metadata_source']] += 1
        review_location_source_counts[meta['location_source']] += 1

        if qa_reason in REVIEW_TARGET_REASONS:
            dist = review_distribution[qa_reason]
            dist['match_type'][meta.get('match_type', 'none')] += 1
            dist['lineage_tier'][meta.get('lineage_tier', 'none')] += 1
            dist['date_source'][(meta['exact_source'] if row_out['exact_date'] else meta['metadata_source']) or 'none'] += 1
            dist['location_source'][meta.get('location_source', 'none') or 'none'] += 1
            dist['url_bucket'][bucketize_url_path(meta.get('url', ''))] += 1
            for tok in extract_review_signal_tokens(row_out):
                dist['tokens'][tok] += 1

        if qa_reason not in failure_samples:
            failure_samples[qa_reason] = []
        if len(failure_samples[qa_reason]) < 20:
            failure_samples[qa_reason].append({
                'secretary': sec,
                'url': meta['url'],
                'exact_date_source': meta['exact_source'],
                'metadata_best_date_source': meta['metadata_source'],
                'event_location_source': meta['location_source'],
                'match_type': meta['match_type'],
            })


def main():
    args = parse_args()
    min_conf = args.min_confidence
    date_window_policy = args.date_window_policy
    workers = max(1, int(args.workers))
    chunk_size = max(1, int(args.chunk_size))
    enabled_secretaries = parse_secretary_selection(args.metadata_secretaries)
    compiled_rules = build_compiled_rules(enabled_secretaries)
    worker_config = make_worker_config(args, enabled_secretaries)

    total_rows = 0
    passing_rows = 0
    missing_text_rows = 0
    sec_counts = Counter()
    rule_counts = Counter()
    conf_counts = Counter()
    date_window_counts = Counter()
    source_counts = Counter()
    acceptance_path_counts = Counter()
    lineage_tier_counts = Counter()
    lineage_reason_counts = Counter()
    gate_rejection_counts = Counter()
    lineage_override_counts = Counter()
    dropped_by_date_policy = 0
    qa_flag_counts = Counter()
    qa_reason_counts = Counter()
    date_quality_counts = Counter()
    location_quality_counts = Counter()
    review_date_source_counts = Counter()
    review_location_source_counts = Counter()
    review_distribution = {
        reason: {
            'match_type': Counter(),
            'lineage_tier': Counter(),
            'date_source': Counter(),
            'location_source': Counter(),
            'url_bucket': Counter(),
            'tokens': Counter(),
        }
        for reason in REVIEW_TARGET_REASONS
    }
    sec_quality_stats = {}
    failure_samples = {}

    with INPUT_CSV.open('r', encoding='utf-8', errors='replace', newline='') as infile, \
         OUTPUT_CSV.open('w', encoding='utf-8', newline='') as outfile:

        safe_iter = (line.replace('\x00', '') for line in infile)
        reader = csv.DictReader(safe_iter)
        fieldnames = list(reader.fieldnames or [])
        extra_fields = [
            'certain_speaker_secretary',
            'certain_speaker_match_type',
            'certain_speaker_match_confidence',
            'certain_speaker_source',
            'exact_date',
            'exact_date_source',
            'exact_date_confidence',
            'metadata_best_date',
            'metadata_best_date_source',
            'date_window_valid',
            'date_window_note',
            'content_type_gate_passed',
            'content_type_positive_hit',
            'content_type_negative_hit',
            'acceptance_path',
            'certain_speaker_excerpt',
            'date_quality_tier',
            'location_quality_tier',
            'qa_quality_flag',
            'qa_failure_reason',
            'lineage_support_tier',
            'lineage_support_reason',
            'lineage_parent_url',
            'lineage_rule_hit',
        ]
        writer = csv.DictWriter(outfile, fieldnames=fieldnames + extra_fields)
        writer.writeheader()

        chunk_iter = chunked_iterable(reader, chunk_size)

        if workers <= 1:
            progress = tqdm(desc='Filtering Text', unit='rows')
            for rows_chunk in chunk_iter:
                total_rows += len(rows_chunk)
                kept_payloads, chunk_stats = process_chunk_rows(rows_chunk, worker_config, compiled_rules, enabled_secretaries)

                missing_text_rows += chunk_stats.get('missing_text_rows', 0)
                dropped_by_date_policy += chunk_stats.get('dropped_by_date_policy', 0)
                for k, v in chunk_stats.items():
                    if k.startswith('gate_rejection:'):
                        gate_rejection_counts[k.split(':', 1)[1]] += v
                    if k.startswith('lineage_override:'):
                        lineage_override_counts[k.split(':', 1)[1]] += v

                for payload in kept_payloads:
                    passing_rows += 1
                    ingest_kept_payload(
                        payload,
                        writer,
                        sec_counts,
                        rule_counts,
                        conf_counts,
                        date_window_counts,
                        source_counts,
                        acceptance_path_counts,
                        lineage_tier_counts,
                        lineage_reason_counts,
                        qa_flag_counts,
                        qa_reason_counts,
                        date_quality_counts,
                        location_quality_counts,
                        review_date_source_counts,
                        review_location_source_counts,
                        sec_quality_stats,
                        failure_samples,
                        review_distribution,
                    )

                progress.update(len(rows_chunk))
            progress.close()
        else:
            with ProcessPoolExecutor(max_workers=workers, initializer=init_worker, initargs=(worker_config,)) as executor:
                progress = tqdm(desc='Filtering Text', unit='rows')
                for kept_payloads, chunk_stats in executor.map(process_chunk_worker, chunk_iter):
                    chunk_total = chunk_stats.get('input_rows', 0)
                    total_rows += chunk_total

                    missing_text_rows += chunk_stats.get('missing_text_rows', 0)
                    dropped_by_date_policy += chunk_stats.get('dropped_by_date_policy', 0)
                    for k, v in chunk_stats.items():
                        if k.startswith('gate_rejection:'):
                            gate_rejection_counts[k.split(':', 1)[1]] += v
                        if k.startswith('lineage_override:'):
                            lineage_override_counts[k.split(':', 1)[1]] += v

                    for payload in kept_payloads:
                        passing_rows += 1
                        ingest_kept_payload(
                            payload,
                            writer,
                            sec_counts,
                            rule_counts,
                            conf_counts,
                            date_window_counts,
                            source_counts,
                            acceptance_path_counts,
                            lineage_tier_counts,
                            lineage_reason_counts,
                            qa_flag_counts,
                            qa_reason_counts,
                            date_quality_counts,
                            location_quality_counts,
                            review_date_source_counts,
                            review_location_source_counts,
                            sec_quality_stats,
                            failure_samples,
                            review_distribution,
                        )

                    progress.update(chunk_total)
                progress.close()

    with SUMMARY_TXT.open('w', encoding='utf-8') as f:
        f.write(f'Input CSV: {INPUT_CSV}\n')
        f.write(f'Output CSV: {OUTPUT_CSV}\n')
        f.write(f'Min confidence used: {min_conf}\n')
        f.write(f'Date window policy: {date_window_policy}\n')
        f.write(f'Include metadata candidates: {args.include_metadata_candidates}\n')
        f.write(f'Metadata max page: {args.metadata_max_page}\n')
        f.write(f'Workers used: {workers}\n')
        f.write(f'Chunk size used: {chunk_size}\n')
        f.write(f'Metadata secretaries: {"ALL" if args.metadata_secretaries.upper()=="ALL" else args.metadata_secretaries}\n')
        f.write(f'Metadata requires discovered_from listing lineage: {args.metadata_require_discovered_from}\n')
        f.write(f'Total input rows: {total_rows}\n')
        f.write(f'Rows with missing raw_text: {missing_text_rows}\n')
        f.write(f'Rows flagged as certain speaker: {passing_rows}\n\n')
        f.write(f'Rows dropped by date policy: {dropped_by_date_policy}\n\n')
        f.write('Per-secretary counts:\n')
        for sec in sorted(SECRETARY_VARIANTS.keys()):
            f.write(f'  {sec}: {sec_counts.get(sec, 0)}\n')
        f.write('\nMatch-rule counts:\n')
        for rule, count in rule_counts.most_common():
            f.write(f'  {rule}: {count}\n')
        f.write('\nMatch-confidence counts:\n')
        for conf, count in conf_counts.most_common():
            f.write(f'  {conf}: {count}\n')
        f.write('\nDate-window status counts:\n')
        for k, v in date_window_counts.most_common():
            f.write(f'  {k}: {v}\n')
        f.write('\nMatch-source counts:\n')
        for k, v in source_counts.most_common():
            f.write(f'  {k}: {v}\n')
        f.write('\nAcceptance-path counts:\n')
        for k, v in acceptance_path_counts.most_common():
            f.write(f'  {k}: {v}\n')
        f.write('\nLineage support tier counts:\n')
        for k, v in lineage_tier_counts.most_common():
            f.write(f'  {k}: {v}\n')
        f.write('\nLineage support reason counts:\n')
        for k, v in lineage_reason_counts.most_common():
            f.write(f'  {k}: {v}\n')
        f.write('\nGate rejection counts:\n')
        for k, v in gate_rejection_counts.most_common():
            f.write(f'  {k}: {v}\n')
        f.write('\nLineage override counts:\n')
        for k, v in lineage_override_counts.most_common():
            f.write(f'  {k}: {v}\n')

    print(f'Wrote {OUTPUT_CSV}')
    print(f'Wrote {SUMMARY_TXT}')
    print(f'Rows flagged: {passing_rows} / {total_rows}')
    print('Confidence counts:', dict(conf_counts))
    print('Date-window status counts:', dict(date_window_counts))
    print('Acceptance path counts:', dict(acceptance_path_counts))
    print('Gate rejection counts:', dict(gate_rejection_counts))

    with QUALITY_AUDIT_TXT.open('w', encoding='utf-8') as f:
        f.write(f'Quality Audit CSV: {OUTPUT_CSV}\n')
        f.write(f'Total rows audited: {passing_rows}\n\n')

        f.write('QA flag counts:\n')
        for k, v in qa_flag_counts.most_common():
            f.write(f'  {k}: {v}\n')
        f.write('\nQA failure reason counts:\n')
        for k, v in qa_reason_counts.most_common():
            f.write(f'  {k}: {v}\n')

        f.write('\nDate quality counts:\n')
        for k, v in date_quality_counts.most_common():
            f.write(f'  {k}: {v}\n')
        f.write('\nLocation quality counts:\n')
        for k, v in location_quality_counts.most_common():
            f.write(f'  {k}: {v}\n')

        f.write('\nTop review date sources:\n')
        for k, v in review_date_source_counts.most_common(15):
            f.write(f'  {k}: {v}\n')
        f.write('\nTop review location sources:\n')
        for k, v in review_location_source_counts.most_common(15):
            f.write(f'  {k}: {v}\n')

        f.write('\nPer-secretary quality coverage:\n')
        for sec in sorted(SECRETARY_VARIANTS.keys()):
            stats = sec_quality_stats.get(sec, Counter())
            total = stats.get('total', 0)
            if total == 0:
                f.write(f'  {sec}: total=0\n')
                continue
            f.write(
                f"  {sec}: total={total}, date_any={stats.get('date_any',0)}, "
                f"location_any={stats.get('location_any',0)}, "
                f"date_and_location={stats.get('date_and_location',0)}\n"
            )
            f.write(
                f"    date_quality: high={stats.get('date_quality_high',0)}, "
                f"medium={stats.get('date_quality_medium',0)}, low={stats.get('date_quality_low',0)}, none={stats.get('date_quality_none',0)}\n"
            )
            f.write(
                f"    location_quality: high={stats.get('location_quality_high',0)}, "
                f"medium={stats.get('location_quality_medium',0)}, low={stats.get('location_quality_low',0)}, none={stats.get('location_quality_none',0)}\n"
            )

        f.write('\nFailure reason samples (up to 20 each):\n')
        for reason, samples in failure_samples.items():
            f.write(f'\n  [{reason}]\n')
            for s in samples:
                f.write(
                    f"    - sec={s['secretary']} | url={s['url']} | match_type={s['match_type']} | "
                    f"exact_source={s['exact_date_source']} | metadata_source={s['metadata_best_date_source']} | "
                    f"location_source={s['event_location_source']}\n"
                )

        f.write('\nReview distribution diagnostics (target reasons):\n')
        for reason in ('missing_date', 'missing_location', 'both_missing'):
            dist = review_distribution.get(reason)
            if not dist:
                continue
            f.write(f'\n  [{reason}]\n')
            f.write('    top_match_types:\n')
            for k, v in dist['match_type'].most_common(12):
                f.write(f'      {k}: {v}\n')
            f.write('    top_lineage_tiers:\n')
            for k, v in dist['lineage_tier'].most_common(8):
                f.write(f'      {k}: {v}\n')
            f.write('    top_date_sources:\n')
            for k, v in dist['date_source'].most_common(10):
                f.write(f'      {k}: {v}\n')
            f.write('    top_location_sources:\n')
            for k, v in dist['location_source'].most_common(10):
                f.write(f'      {k}: {v}\n')
            f.write('    top_url_buckets:\n')
            for k, v in dist['url_bucket'].most_common(12):
                f.write(f'      {k}: {v}\n')
            f.write('    top_signal_tokens:\n')
            for k, v in dist['tokens'].most_common(20):
                f.write(f'      {k}: {v}\n')

    print(f'Wrote {QUALITY_AUDIT_TXT}')


if __name__ == '__main__':
    main()
