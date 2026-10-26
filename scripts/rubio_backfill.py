#!/usr/bin/env python3
"""rubio_backfill.py — Standalone end-to-end backfill of Rubio speeches from www.state.gov.

Discovers new URLs from the /remarks-secretary-rubio/ archive walk and the state.gov
XML sitemaps (page-sitemaps plus the press-release / briefing / external-content
release sitemaps), plus supplemental URL lists and the pending-fetch queue; fetches
each page live, classifies it (is Rubio speaking or writing in his own voice?),
extracts content, and writes a CSV matching the schema of
secretaryofstate_speeches_final.fused.v44.patched.enriched.csv (plus modality,
byline, listing_date, discovery_source).

Three objectives enforced for every row:
  speech_text  — Secretary Rubio's utterances only, no boilerplate
  speaker_text — all natural speakers without boilerplate (full transcript/exchange)
  raw_text     — full page plain text from live fetch

Sidecars (next to --output unless given; data/ in CI; see docs/rubio_coverage_audit.md):
  rubio_skipped.csv           every rejection: url, reason, stage, block_type, byline,
                              title, first_400, labels, run_id                 (C-03)
  rubio_listing_snapshot.csv  every archive slot walked                        (D-03)
  rubio_pending.csv           fetch failures, re-fed every run until fetched   (F-01)
  rubio_stuck.csv             pending URLs failed in >= 3 runs (only if any)
  rubio_discovery_state.json  last_complete_floor + sitemap classifications    (D-04)
  rubio_backfill_summary.json run counters

Usage:
    python3 rubio_backfill.py [options]     (see --help)

Exit codes: 0 done (including "nothing new"); non-zero when discovery could not
prove completeness (DiscoveryError) -- never "no new speeches" on a failed crawl.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).parent))

from corpus_keys import (
    canonical_url_key,
    url_year_month,
    canonicalize_live_url,
    slug_date_key,
    slug_ym_key,
    url_slug,
)
from hybrid_corpus_recovery import (
    extract_secretary_only_text,
    guess_speech_type,
    normalize_url,
    parse_date_from_url,
)
from speech_recover import (
    _DATE_LINE_RE,
    _HIDE_ANCHOR_RE,
    _TAGS_SENTINEL,
    _extract_modern_state_content,
)
from speech_extract import BASE_DIR, make_speaker_text
import corpus_title
import rubio_turns

try:
    import requests
    from bs4 import BeautifulSoup, CData, NavigableString
except ImportError:
    print('[rubio_backfill] ERROR: requests and beautifulsoup4 are required.', file=sys.stderr)
    sys.exit(1)


def _make_soup(html: str):
    """Parse HTML with lxml when available, falling back to stdlib html.parser."""
    try:
        return BeautifulSoup(html, 'lxml')
    except Exception as e:
        if 'lxml' not in str(e):
            raise
        return BeautifulSoup(html, 'html.parser')

csv.field_size_limit(10 ** 7)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SECRETARY = 'rubio'
RUBIO_START = date(2025, 1, 21)  # Rubio appointment window start

_ENRICHED_DEFAULT = BASE_DIR / 'secretaryofstate_speeches_final.fused.v44.patched.enriched.csv'
_SITEMAP_INDEX = 'https://www.state.gov/sitemap_index.xml'
_REMARKS_INDEX = 'https://www.state.gov/remarks-secretary-rubio/'

# state.gov is intermittently flaky: a single 2026-10-12 run saw three
# page-sitemaps return 504 and the remarks index read-timeout outright. With
# no retry and a swallowed exception that collapsed discovery to zero
# candidates, and the job still reported success -- a whole week of speeches
# was silently skipped. Retry transient failures before giving up.
_FETCH_ATTEMPTS = 3
# (connect, read). The read limit is generous because state.gov routinely
# takes 30s+ under load; the connect limit is short because an unreachable
# host will not get better by waiting. At (60, 60) one dead page cost ~3.25
# min across three attempts, and the 2026-10-12 dispatch spent 26 of its 29
# minutes in discovery that way.
_FETCH_TIMEOUT = (10, 45)
_FETCH_BACKOFF = 5.0     # seconds, multiplied by the attempt number
# Sitemaps are the second, independent source (audit D-01/D-02): page-sitemaps
# for legacy flat slugs, and the release sitemaps for every current-era release
# (off-collection items like the Institute of Peace remarks live only there).
# Every sitemap gets the full retry count; incremental runs also share a time
# budget, --full-rescan does not.
_SITEMAP_BUDGET_MIN = 10.0
_RELEASE_SITEMAP_PREFIXES = (
    'state_press_release-sitemap',
    'state_briefing-sitemap',
    'state_ext_content-sitemap',
)
# Release-sitemap and flat-page candidates are unfiltered (~1.2k press releases a
# month, nearly all by other people), so at most this many are fetched per run.
# The rest are logged and retried. Raised from 50 on 2026-10-26: 50 left ~300
# release candidates unfetched every week.
_SITEMAP_FETCH_CAP = 150
# Floor slack below the latest corpus Date (was 14; audit D-04).
_FLOOR_BUFFER_DAYS = 60
# A pending URL that has failed in this many runs is "stuck" (issue opened in CI).
_STUCK_ATTEMPTS = 3

# Run sidecars, written next to --output (data/ in CI).
_SKIPPED_NAME = 'rubio_skipped.csv'
_SNAPSHOT_NAME = 'rubio_listing_snapshot.csv'
_PENDING_NAME = 'rubio_pending.csv'
_STATE_NAME = 'rubio_discovery_state.json'
_STUCK_NAME = 'rubio_stuck.csv'
_SUMMARY_NAME = 'rubio_backfill_summary.json'
# Hand decisions, checked into the repo.
_EXCLUSIONS_DEFAULT = Path(__file__).resolve().parent.parent / 'data' / 'rubio_coverage_exclusions.csv'

_SKIPPED_COLUMNS = ['url', 'reason', 'stage', 'block_type', 'byline', 'title',
                    'first_400', 'labels', 'run_id']
_SNAPSHOT_COLUMNS = ['page', 'pos', 'href', 'url_key', 'type', 'title', 'list_date',
                     'total_pages', 'items_header', 'pass']
_PENDING_COLUMNS = ['url', 'first_seen', 'attempts', 'last_error', 'source']


class DiscoveryError(RuntimeError):
    """A discovery source could not be read, so "nothing new" is unprovable."""


class SkipLog:
    """Every rejection, one row each -> data/rubio_skipped.csv (audit C-03).

    Skips used to be stderr lines only; that is how the Cuban Independence Day
    video message sat in "[skip] Not active speaker" for 8 weeks unnoticed.
    stage: discovery_sitemap | discovery_listing | prefetch | fetch | extract |
    post_fetch | classify.
    """

    def __init__(self, run_id: str = ''):
        self.run_id = run_id
        self.rows: list[dict] = []

    def add(self, url: str, reason: str, stage: str, *, block_type: str = '',
            byline: str = '', title: str = '', text: str = '', labels: str = '') -> None:
        self.rows.append({
            'url': url, 'reason': reason, 'stage': stage, 'block_type': block_type,
            'byline': byline, 'title': title,
            'first_400': ' '.join((text or '')[:800].split())[:400],
            'labels': labels, 'run_id': self.run_id,
        })

    def counts(self) -> Counter:
        return Counter(r['reason'] for r in self.rows)

    def write(self, path: Path) -> None:
        _write_rows(path, _SKIPPED_COLUMNS, self.rows)


def _get_with_retry(session: requests.Session, url: str, *,
                    attempts: int = _FETCH_ATTEMPTS,
                    label: str = '') -> "requests.Response":
    """GET with bounded retry/backoff. Raises the last exception on failure."""
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = session.get(url, headers=_HEADERS, timeout=_FETCH_TIMEOUT)
            resp.raise_for_status()
            return resp
        except Exception as e:  # noqa: BLE001 -- requests raises a wide family
            last = e
            if attempt < attempts:
                delay = _FETCH_BACKOFF * attempt
                print(f'[rubio_backfill] retry {attempt}/{attempts - 1} in {delay:.0f}s '
                      f'for {label or url}: {e}', file=sys.stderr)
                time.sleep(delay)
    assert last is not None
    raise last


_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) '
        'Gecko/20100101 Firefox/115.0'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}

# File extensions and path segments that are never speech pages
_EXCLUDE_EXTS = ('.pdf', '.png', '.jpg', '.jpeg', '.gif', '.zip', '.doc', '.docx')
_EXCLUDE_PATHS = ('/translations/', '/wp-content/')

# Foreign-language path prefixes (state.gov mirrors speeches in other languages)
_FOREIGN_LANG_SEGS = (
    '/entretien',    # French "phone call / interview"
    '/declaracao',   # Portuguese "declaration"
    '/declaraci',    # Spanish "declaración"
    '/comunicado',   # Spanish/Portuguese "communiqué"
    '/llamada',      # Spanish "phone call"
    '/reunion',      # Spanish "meeting"
    '/rencontre',    # French "meeting"
    '/appel',        # French "call"
    '/reuniao',      # Portuguese "meeting"
)

# Non-speech informational pages whose URLs still contain "rubio"
_NON_SPEECH_SEGS = (
    'countries-visited-and-mileage',   # travel statistics page
    'rubio-zea',                       # Ana Gabriela Rubio Zea (narcotics trafficker, not Secretary)
)

# URL path segments that identify non-active-speaker pages: diplomatic readouts,
# travel advisories, staff briefings. Applied at discovery time to avoid fetching
# pages where Rubio is discussed but does not speak.
_NON_SPEAKER_URL_PATS = (
    'secretary-rubios-call-with-',
    'secretary-rubios-meeting-with-',
    'secretary-rubios-travel-to-',
    'secretary-rubios-travel-',
    'on-the-record-briefing-on-secretary',
    'secretary-rubios-participation-in-',
)

# Byline lines in the block header (audit C-02). A byline is "Name, Title"; these
# words after the first comma mark a title (as in rubio_ground_truth.py).
_BYLINE_TITLE_RE = re.compile(
    r'\b(?:Secretary|President|Minister|Spokesperson|Spokesman|Ambassador|'
    r'Administrator|Director|Adviser|Advisor|Representative|Envoy|Counselor|'
    r'Chief|Governor|Senator|Chair(?:man|woman)?|Coordinator|Commissioner|'
    r'Attorney General|Leader|Deputy|Under Secretary|Head|Chancellor|King|'
    r'Prime Minister|Foreign|Designate)\b'
)
_SPOKES_OFFICE_RE = re.compile(r'^Office of the Spokes(?:person|man)$', re.IGNORECASE)
_SPOKES_BYLINE_RE = re.compile(r'\bSpokes(?:person|man)\b', re.IGNORECASE)
# "Joint Statement by Secretary of State Marco Rubio and ..." /
# "...(WHO): Joint Statement by Secretary of State Rubio and Secretary ... Kennedy"
_JOINT_TITLE_RE = re.compile(r'Joint Statement (?:by|from|of)\b[^:]*\bRubio\b', re.IGNORECASE)
# A joint statement Rubio releases personally, named in the opening line rather
# than the title: "The text of the following statement was released by Secretary
# of State Marco Rubio, Secretary of Commerce ..." (2025-08-12 IMO) or "Joint
# Statement by Secretary of State Marco Rubio, United States Trade Representative
# ..." (2025-10-16 U.S.-Brazil). Statements "released by the Governments of ..."
# do not match: those are government-to-government, not his voice.
_JOINT_RELEASED_RE = re.compile(
    r'(?:statement (?:was|is) (?:released|issued) by|Joint Statement by)\s+'
    r'(?:U\.S\.\s+)?Secretary of State Marco Rubio\b', re.IGNORECASE)

# Sitemap XML namespace
_SITEMAP_NS = '{http://www.sitemaps.org/schemas/sitemap/0.9}'

# Remarks-archive listing page evidence (same patterns as rubio_ground_truth.py).
_LISTING_PAGE_RE = re.compile(r'/remarks-secretary-rubio/page/(\d+)/?')
_ITEMS_HEADER_RE = re.compile(r'\b(\d[\d,]*)\s+(?:items?|results?)\b', re.IGNORECASE)
_LIST_DATE_RE = re.compile(
    r'\b(?:January|February|March|April|May|June|July|August|September|'
    r'October|November|December)\s+\d{1,2},\s+\d{4}\b'
)

# State.gov nav boilerplate signals — appear at the start of extracted text when
# extract_secretary_only_text falls back to raw content with no speaker labels found.
_NAV_BOILERPLATE_SIGNALS = (
    'and Resources\nAbout\n',
    'About\nBack\nAbout\n',
    'History\nPrevious Administration Archives\n',
    'Secretary of State\nHome\n',
    'Foreign Relations of the United States\nNational Museum',
)

# Speaker labels are found by rubio_turns.py (case-sensitive, Unicode-aware, split and
# typo labels resolved).  The old case-insensitive ASCII regexes here took vocatives
# ("Secretary Rubio, thank you...") for Rubio turns and missed RAMÍREZ: / ŠEFČOVIČ
# boundaries (audit X-03..X-06).

# CSV column order must match the enriched CSV exactly
_FIELDNAMES = [
    'speech_type', 'Location', 'Date', 'secretary', 'Title', 'speech_text',
    'row_id', 'url', 'url_match_score', 'url_match_method', 'url_match_status',
    'match_pass', 'cleaning_profile_used', 'pass3_stage', 'pass3_profile_used',
    'raw_text', 'speaker_text', 'extraction_mode',
    # Count of "(In Spanish.)"-type placeholders inside Rubio's own text: words he
    # said that state.gov never transcribed (audit X-10).  corpus_rubio_update
    # carries it, with 'modality', into data/rubio_merge_provenance.csv as
    # 'Untranscribed Rubio Segments' / 'Modality' (sidecar, not published columns).
    'untranscribed_rubio_segments',
    # Plan step P2/P4 (audit D2, C-02, M-06): spoken | written | video | joint;
    # the block byline line(s); the archive listing date (corpus_rubio_update's
    # Date fallback); which discovery source found the URL.
    'modality', 'byline', 'listing_date', 'discovery_source',
]

# Third-person attribution patterns: page is ABOUT Rubio, not BY Rubio.
# Applied as a secondary content check after extraction (catches /releases/ edge cases).
# A match is no longer final (audit C-01): _active_speaker_decision keeps the page
# when a transcript marker or a Rubio label follows the intro.
_THIRD_PERSON_OPENINGS = (
    'The below is attributable to Spokesperson',
    'Secretary of State Marco Rubio spoke',
    'Secretary of State Marco Rubio met',
    'Secretary of State Marco Rubio will',
    'Secretary Rubio spoke',
    'Secretary Rubio met',
    'Secretary Rubio will',
    'Today, Secretary of State Marco Rubio delivered',
)
# "Secretary Rubio and ..." only counts as a readout opening with a readout verb
# ("Secretary Rubio and Foreign Minister X met / spoke / discussed / held ...");
# it used to match any sentence starting that way.
_THIRD_PERSON_AND_RE = re.compile(
    r"^Secretary (?:of State )?(?:Marco )?Rubio and\b[^.]{0,200}?\b(?:met|spoke|discussed|held)\b"
)
# Transcript markers: after a third-person intro, his own words follow.
# The wording varies page to page, so the "his text follows" forms are listed
# broadly: "Below is the transcript of the Secretary's message", "The text of his
# remarks follows", "The full text is below", "The transcript follows".
_TRANSCRIPT_MARKER_RE = re.compile(
    r'translated transcript|courtesy translation|'
    r'video (?:message|remarks|address) (?:is|are) below|'
    r'as prepared(?: for delivery)?|as delivered|'
    r'below is the (?:full )?(?:transcript|text)\b|'
    r'\btranscript (?:is )?(?:below|follows)|'
    r'\btext of (?:his|the secretary.s) (?:remarks|message|statement|address)'
    r' (?:is below|follows)|'
    r'\b(?:full )?text (?:is below|follows)',
    re.IGNORECASE,
)
_MARKER_SEARCH_CHARS = 2000        # the intro, not the body
_VIDEO_RE = re.compile(r'\bvideo (?:message|remarks|address|statement)\b', re.IGNORECASE)
# Speech Types that are spoken events (Modality 'spoken' even without labels).
_SPOKEN_SPEECH_TYPES = frozenset({
    'remarks', 'remarks to the press', 'interview', 'press availability',
    'joint press availability', 'speech', 'testimony', 'town hall', 'press briefing',
})
_WRITTEN_SPEECH_TYPES = frozenset({'press statement', 'statement', 'op-ed'})


# Pipeline metadata fields set uniformly for all backfill rows
_PIPELINE_META = {
    'url_match_score':      '1.000000',
    'url_match_method':     'direct_live_fetch',
    'url_match_status':     'matched',
    'match_pass':           'rubio_backfill',
    'cleaning_profile_used': 'live_fetch',
    'pass3_stage':          '',
    'pass3_profile_used':   '',
}


# ---------------------------------------------------------------------------
# URL filtering
# ---------------------------------------------------------------------------

def _strip_query(url: str) -> str:
    """URL without its ?query / #fragment ("...?utm_source=homepage" is the same page)."""
    return (url or '').split('#', 1)[0].split('?', 1)[0]


def _is_excluded(url: str) -> bool:
    """True for URLs that are never speech pages (binaries, translations).

    A query string no longer excludes a URL (audit D-07: "?utm_source=..." links
    were dropped silently); callers strip it with _strip_query first.
    """
    low = _strip_query(url).lower()
    for ext in _EXCLUDE_EXTS:
        if low.endswith(ext):
            return True
    for seg in _EXCLUDE_PATHS:
        if seg in low:
            return True
    return False


def _is_false_positive_url(url: str) -> bool:
    """True for URLs that contain 'rubio' but are not Secretary Rubio speeches."""
    low = url.lower()
    for seg in _FOREIGN_LANG_SEGS:
        if seg in low:
            return True
    for seg in _NON_SPEECH_SEGS:
        if seg in low:
            return True
    return False


# Sitemap-only sources: unfiltered by slug, capped per run, and remembered once
# classified out (discovery state) so the cap rotates through the backlog.
_CAPPED_SOURCES = frozenset({'sitemap_release', 'sitemap_flat'})
# Flat slugs that are navigation/topic pages, never a release.
_NON_ITEM_PREFIXES = ('bureau', 'office-', 'countries-areas', 'policy-issues', 'about',
                      'biographies', 'remarks-secretary-', 'releases', 'translations',
                      'u-s-relations-with', 'u-s-embassy', 'embassy-', 'consulate-')


def _is_flat_item_url(url: str) -> bool:
    """https://www.state.gov/<one-long-slug>/ that reads like a release, not a
    navigation or topic page (>= 3 hyphens -- "responding-to-panama-earthquake" --
    and not a bureau/office/country page)."""
    parts = [p for p in urlsplit(url).path.split('/') if p]
    return (len(parts) == 1 and parts[0].count('-') >= 3
            and not parts[0].lower().startswith(_NON_ITEM_PREFIXES))


def _is_rubio_speech_url(url: str) -> bool:
    """True if the URL is a Rubio active-speaker page (not a readout/advisory).

    Every URL in the existing Rubio corpus contains 'rubio' in the path.
    Also rejects non-speaker pages: diplomatic readouts, travel advisories,
    and staff briefings where Rubio is discussed but does not speak.
    """
    if 'rubio' not in url.lower():
        return False
    if _is_false_positive_url(url):
        return False
    low = url.lower()
    if any(pat in low for pat in _NON_SPEAKER_URL_PATS):
        return False
    return True


# ---------------------------------------------------------------------------
# Step 1: Load existing corpus
# ---------------------------------------------------------------------------

class CorpusIndex(NamedTuple):
    """Everything the scraper needs to know about what is already collected."""
    url_keys: set          # canonical_url_key(URL) and of the live-renamed form
    slug_ym: set           # slug + YYYY-MM  -- usable BEFORE a page is fetched
    slug_date: set         # slug + full Date -- usable AFTER a page is fetched
    max_row_id: int
    max_date: "date | None"  # latest Date for THIS secretary, for --since


def load_existing_urls(enriched_path: Path,
                       secretary: str = 'Rubio') -> CorpusIndex:
    """Index the existing corpus by every key the dedup stages need.

    Reads the capitalized research schema ("URL", "Row Id", "Date",
    "Secretary"), falling back to the legacy lowercase Project2 schema
    ("url", "row_id", ...) so the older local backfill CSVs still work.

    HISTORY -- why this function is paranoid now. It used to read only
    row.get('url') and row.get('row_id'), lowercase. The corpus CSV that CI
    feeds it uses "URL" and "Row Id". It did not raise, because
    row.get('row_id', 0) returns a perfectly valid 0, so every weekly run
    logged "0 existing URLs, max row_id=0", deduplicated against an empty
    set, and re-fetched all 773 Rubio pages -- about 21 minutes of the job.
    A silent no-op is the worst possible failure for a dedup index, so an
    index that comes back empty is now a hard error.
    """
    known: set[str] = set()
    slug_ym: set[str] = set()
    slug_date: set[str] = set()
    max_rid: int | None = None
    max_date: date | None = None
    n_rows = 0

    with open(enriched_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        cols = set(reader.fieldnames or [])
        # Capitalized research schema first, legacy lowercase as fallback.
        url_col = 'URL' if 'URL' in cols else ('url' if 'url' in cols else None)
        rid_col = 'Row Id' if 'Row Id' in cols else ('row_id' if 'row_id' in cols else None)
        date_col = 'Date' if 'Date' in cols else ('date' if 'date' in cols else None)
        sec_col = 'Secretary' if 'Secretary' in cols else ('secretary' if 'secretary' in cols else None)
        if url_col is None or rid_col is None:
            raise ValueError(
                f'{enriched_path}: no URL/Row Id columns found. '
                f'Columns present: {sorted(cols)[:12]}'
            )

        for row in reader:
            n_rows += 1
            url = (row.get(url_col) or '').strip()
            row_date = (row.get(date_col) or '').strip() if date_col else ''
            if url:
                known.add(canonical_url_key(url))
                # Index the renamed form too, so a candidate discovered under
                # a path state.gov has since renamed still matches.
                known.add(canonical_url_key(canonicalize_live_url(url)))
                k = slug_ym_key(url, row_date)
                if k:
                    slug_ym.add(k)
                k = slug_date_key(url, row_date)
                if k:
                    slug_date.add(k)
            try:
                rid = int(row.get(rid_col, 0) or 0)
                if max_rid is None or rid > max_rid:
                    max_rid = rid
            except (ValueError, TypeError):
                pass
            # Track the latest date for THIS secretary only -- the corpus runs
            # to 2025-01 for previous secretaries, which would pin the floor
            # far too early.
            if row_date and (sec_col is None or row.get(sec_col) == secretary):
                try:
                    d = date.fromisoformat(row_date)
                except ValueError:
                    continue
                if max_date is None or d > max_date:
                    max_date = d

    if max_rid is None:
        raise ValueError(f'{enriched_path}: could not read max row id from {rid_col!r}')
    if not known:
        raise ValueError(
            f'{enriched_path}: read {n_rows:,} rows but indexed 0 URLs from '
            f'column {url_col!r} -- schema mismatch. Refusing to run with an '
            f'empty dedup index (that is what made every weekly run a full '
            f're-scrape).'
        )

    return CorpusIndex(known, slug_ym, slug_date, max_rid, max_date)


# ---------------------------------------------------------------------------
# Step 2: URL Discovery
# ---------------------------------------------------------------------------

class Candidate(NamedTuple):
    """A URL to consider, and where it came from."""
    url: str
    # archive | sitemap_page | sitemap_flat | sitemap_release | supplemental | held | pending
    source: str
    listing_date: str = ''    # ISO date the archive listing showed, if any
    # For a re-fed pending URL: the source that first found it. Decides whether
    # a page with no byline may be kept (see _TRUSTED_SOURCES).
    origin: str = ''
    # Sitemap <lastmod> (ISO) for undated sitemap_flat pages: fetch-order only,
    # never a Date (it is the edit date).
    lastmod: str = ''


def _parse_lastmod(text: str) -> date | None:
    """Parse ISO lastmod string to date; return None on failure."""
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _sitemap_kind(loc: str) -> str:
    """'page', 'release', or '' for a child sitemap that is not read."""
    name = urlsplit(loc).path.rsplit('/', 1)[-1].lower()
    if name.startswith('page-sitemap'):
        return 'page'
    if any(name.startswith(p) for p in _RELEASE_SITEMAP_PREFIXES):
        return 'release'
    return ''


def _in_floor_window(loc: str, lastmod: date | None, since: date) -> bool:
    """URL /YYYY/MM/ >= the floor month when the URL has one; otherwise lastmod >=
    since; an undated URL with no lastmod is kept (decided after the fetch)."""
    ym = url_year_month(loc)
    if ym is not None:
        return ym >= (since.year, since.month)
    return lastmod is None or lastmod >= since


def discover_sitemap_urls(session: requests.Session, rate_limit: float,
                          since: date = RUBIO_START, *,
                          attempts: int = _FETCH_ATTEMPTS,
                          budget_min: float | None = None,
                          skiplog: "SkipLog | None" = None,
                          stats: dict | None = None) -> list[Candidate]:
    """Crawl state.gov sitemaps; return candidate URLs.

    Two kinds of child sitemap are read:
      page-sitemap*   legacy flat slugs. Filtered as before: <lastmod> >= since
                      (lastmod is the EDIT date, so only a hint) and
                      _is_rubio_speech_url ('rubio' in the slug, no readout
                      pattern).
      state_press_release-sitemap*, state_briefing-sitemap*,
      state_ext_content-sitemap*
                      every current-era release (audit D-01/D-02). These were
                      never read, which is why "0 from sitemaps" in every
                      incremental run and why the Institute of Peace and
                      Homestead remarks and the RFK Jr. joint statements, all
                      off the remarks collection, were never seen. NO 'rubio'
                      or blacklist filter here -- 70% of real Rubio releases
                      have no 'rubio' in the slug. Kept when the URL month (or,
                      undated, lastmod) is inside the floor window; the
                      post-fetch classification decides, and run() caps how
                      many are fetched per run. A release child sitemap whose
                      own lastmod is before `since` holds nothing newer and is
                      not fetched.

    Best-effort: a sitemap failure is a warning (recorded in stats), since the
    remarks archive is the primary source. `attempts` is per sitemap; once
    `budget_min` minutes have passed the remaining sitemaps are skipped.
    """
    stats = stats if stats is not None else {}
    stats.update(sitemaps_read=0, sitemaps_failed=[], sitemaps_old=0, sitemaps_budget_skipped=0,
                 sitemap_page_hits=0, sitemap_flat_hits=0, sitemap_release_hits=0)
    candidates: list[Candidate] = []
    deadline = time.monotonic() + budget_min * 60 if budget_min else None

    print(f'[rubio_backfill] Fetching sitemap index: {_SITEMAP_INDEX}')
    try:
        resp = _get_with_retry(session, _SITEMAP_INDEX, attempts=attempts, label='sitemap index')
        root = ElementTree.fromstring(resp.text)
    except Exception as e:  # noqa: BLE001
        print(f'[rubio_backfill] WARNING: Could not read sitemap index: {e}', file=sys.stderr)
        stats['sitemaps_failed'].append(_SITEMAP_INDEX)
        return candidates

    children: list[tuple[str, str]] = []
    for sitemap in root.iter(f'{_SITEMAP_NS}sitemap'):
        loc = (sitemap.findtext(f'{_SITEMAP_NS}loc') or '').strip()
        kind = _sitemap_kind(loc)
        if not kind:
            continue
        child_mod = _parse_lastmod((sitemap.findtext(f'{_SITEMAP_NS}lastmod') or '').strip())
        if kind == 'release' and child_mod and child_mod < since:
            stats['sitemaps_old'] += 1
            continue
        children.append((loc, kind))

    n_page = sum(k == 'page' for _, k in children)
    print(f'[rubio_backfill] Found {n_page} page sitemaps and {len(children) - n_page} '
          f'release sitemaps to scan ({stats["sitemaps_old"]} release sitemap(s) last '
          f'modified before {since} skipped)')

    for i, (sitemap_url, kind) in enumerate(children, 1):
        if deadline is not None and time.monotonic() > deadline:
            stats['sitemaps_budget_skipped'] = len(children) - i + 1
            print(f'[rubio_backfill] WARNING: sitemap budget ({budget_min:g} min) spent -- '
                  f'skipping the remaining {len(children) - i + 1} sitemap(s)',
                  file=sys.stderr)
            break
        time.sleep(rate_limit)
        try:
            resp = _get_with_retry(session, sitemap_url, attempts=attempts, label=sitemap_url)
            root = ElementTree.fromstring(resp.text)
        except Exception as e:  # noqa: BLE001
            print(f'[rubio_backfill] WARNING: skipping sitemap {sitemap_url}: {e}',
                  file=sys.stderr)
            stats['sitemaps_failed'].append(sitemap_url)
            continue
        stats['sitemaps_read'] += 1

        found_in_this = 0
        for url_el in root.iter(f'{_SITEMAP_NS}url'):
            loc = _strip_query((url_el.findtext(f'{_SITEMAP_NS}loc') or '').strip())
            if not loc:
                continue
            lastmod = _parse_lastmod((url_el.findtext(f'{_SITEMAP_NS}lastmod') or '').strip())
            if kind == 'page':
                if lastmod and lastmod < since:
                    continue
                if _is_excluded(loc):
                    if skiplog is not None and 'rubio' in loc.lower():
                        skiplog.add(loc, 'excluded_path', 'discovery_sitemap')
                    continue
                if _is_rubio_speech_url(loc):
                    candidates.append(Candidate(loc, 'sitemap_page'))
                    stats['sitemap_page_hits'] += 1
                    found_in_this += 1
                elif (lastmod and _is_flat_item_url(loc)
                      and not any(pat in loc.lower() for pat in _NON_SPEAKER_URL_PATS)):
                    # Undated flat page edited inside the window, no 'rubio' in
                    # the slug: /responding-to-panama-earthquake/ is a 2026-10-09
                    # Rubio press statement that neither the archive nor the
                    # 'rubio' filter ever surfaced. Classified after fetch (needs
                    # a Rubio byline or labels) and capped like release sitemaps.
                    candidates.append(Candidate(loc, 'sitemap_flat', lastmod=lastmod.isoformat()))
                    stats['sitemap_flat_hits'] += 1
                    found_in_this += 1
            else:
                if not _in_floor_window(loc, lastmod, since):
                    continue
                if _is_excluded(loc):
                    if skiplog is not None:
                        skiplog.add(loc, 'excluded_path', 'discovery_sitemap')
                    continue
                candidates.append(Candidate(loc, 'sitemap_release'))
                stats['sitemap_release_hits'] += 1
                found_in_this += 1

        if found_in_this:
            print(f'  [{i}/{len(children)}] {sitemap_url} → {found_in_this} candidates')

    if stats['sitemaps_failed']:
        print(f'[rubio_backfill] WARNING: {len(stats["sitemaps_failed"])} sitemap(s) unreadable: '
              f'{stats["sitemaps_failed"][:5]}', file=sys.stderr)
    return candidates


def _iso_list_date(text: str) -> str:
    try:
        return datetime.strptime((text or '').strip(), '%B %d, %Y').date().isoformat()
    except ValueError:
        return ''


def parse_remarks_listing(html: str) -> dict:
    """One /remarks-secretary-rubio/ listing page -> items + pagination evidence.

    items: [{href, title, type, list_date}] in page order; total_pages: the
    input[name=total_pages] value (int or None); max_link_page: highest
    /page/N/ link; items_header / items_count: the "N items" header.
    """
    soup = _make_soup(html)
    items = []
    for el in soup.find_all(class_='collection-result'):
        a = el.find('a', class_='collection-result__link', href=True) or el.find('a', href=True)
        if a is None:
            continue
        href = (a.get('href') or '').strip()
        if not href or '{{' in href:
            continue
        href = urljoin(_REMARKS_INDEX, href)
        title = ' '.join(a.get_text(' ', strip=True).split())
        list_date = type_line = ''
        for t in el.stripped_strings:
            t = ' '.join(t.split())
            if not t or t == title:
                continue
            m = _LIST_DATE_RE.search(t)
            if m and not list_date:
                list_date = m.group(0)
            elif not type_line and len(t) <= 60:
                type_line = t
        items.append({'href': href, 'title': title, 'type': type_line, 'list_date': list_date})

    total_pages = None
    inp = soup.find('input', attrs={'name': 'total_pages'})
    if inp is not None:
        try:
            total_pages = int((inp.get('value') or '').strip())
        except ValueError:
            total_pages = None

    max_link_page = 1
    for a in soup.find_all('a', href=True):
        m = _LISTING_PAGE_RE.search(a['href'])
        if m:
            max_link_page = max(max_link_page, int(m.group(1)))

    items_header = ''
    for el in soup.find_all(class_=re.compile(r'collection-info|results?-count|count')):
        if _ITEMS_HEADER_RE.search(el.get_text(' ', strip=True)):
            items_header = ' '.join(el.get_text(' ', strip=True).split())[:80]
            break
    if not items_header:
        m = _ITEMS_HEADER_RE.search(soup.get_text(' ', strip=True)[:20000])
        if m:
            items_header = m.group(0)
    items_count = None
    m = _ITEMS_HEADER_RE.search(items_header)
    if m:
        try:
            items_count = int(m.group(1).replace(',', ''))
        except ValueError:
            items_count = None
    return {'items': items, 'total_pages': total_pages, 'max_link_page': max_link_page,
            'items_header': items_header, 'items_count': items_count}


def _item_ym(item: dict) -> tuple | None:
    """(year, month) of a listing item: URL /YYYY/MM/, else the listed date."""
    ym = url_year_month(item['href'])
    if ym is None and item.get('list_date'):
        iso = _iso_list_date(item['list_date'])
        if iso:
            ym = (int(iso[:4]), int(iso[5:7]))
    return ym


def listing_drift(pages: dict[int, dict], page_size: int, max_page: int) -> tuple[int, int, set]:
    """(dup_slots, short_pages, pages to re-walk) for the walked listing pages (D-03).

    The archive is paged one request at a time with no snapshot: an item that
    moves between two page fetches shows up twice, and the slot it vacated -- some
    other item -- is never seen. A duplicate slot therefore means a missed slot;
    so does a short page that is not the last. Affected pages are every page a
    duplicate appears on, its neighbours, and every short page.
    """
    occ: dict[str, list[int]] = {}
    slots = 0
    for pg, parsed in pages.items():
        for it in parsed['items']:
            occ.setdefault(canonical_url_key(it['href']), []).append(pg)
            slots += 1
    dup_slots = slots - len(occ)
    affected: set[int] = set()
    for pgs in occ.values():
        if len(pgs) > 1:
            for p in pgs:
                affected |= {p - 1, p, p + 1}
    short = 0
    for pg, parsed in pages.items():
        if pg < max_page and page_size and len(parsed['items']) < page_size:
            short += 1
            affected.add(pg)
    return dup_slots, short, affected & set(pages)


def discover_remarks_urls(session: requests.Session, rate_limit: float,
                          since: date = RUBIO_START, *,
                          in_corpus=None,
                          snapshot: list | None = None,
                          skiplog: "SkipLog | None" = None,
                          stats: dict | None = None,
                          run_id: str = '') -> list[Candidate]:
    """Walk the paginated remarks archive; return article content URLs.

    Two-hop discovery:
      1. Listing pages (/remarks-secretary-rubio/page/N/) — scanned but not kept
      2. Content pages (from .collection-result > a on each listing page) — returned

    These URLs live under /releases/office-of-the-spokes*/ and often don't
    contain 'rubio', so the page-sitemap filter cannot see them.

    The walk is verifiable (audit D-03..D-06):
      * page 1 with 0 links, or one page of >= 10 links with no pagination at
        all, raises DiscoveryError (markup change, not "nothing new");
      * the page count is the larger of the /page/N/ links and
        input[name=total_pages];
      * an unreadable page is retried once more at the end with a cache-buster;
        one still unreadable INSIDE the floor window raises DiscoveryError --
        it used to be skipped while the job stayed green (2026-10-12: 9 pages);
      * duplicate slots and short pages are re-walked once with a cache-buster,
        and both passes' links are kept;
      * every slot goes to `snapshot` (data/rubio_listing_snapshot.csv).
    `in_corpus(url)`: links whose key is NOT in the corpus are kept whatever
    their month (D-04: late-listed and backdated items); the month floor only
    decides where the walk may stop. stats['complete'] says whether the walk is
    proven complete (no unreadable in-window page, no unresolved duplicate slot
    or short page, unique >= the header count on a full walk).
    """
    stats = stats if stats is not None else {}
    snapshot = snapshot if snapshot is not None else []
    floor_ym = (since.year, since.month)
    buster = run_id or datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')

    def fetch_listing(pg: int, cache_bust: bool = False) -> dict:
        url = _REMARKS_INDEX if pg == 1 else f'{_REMARKS_INDEX}page/{pg}/'
        if cache_bust:
            url += f'?cb={buster}'
        resp = _get_with_retry(session, url, label=f'remarks page {pg}')
        return parse_remarks_listing(resp.text)

    seen_items: list[tuple[int, dict]] = []     # (page, item), both passes

    def record(pg: int, parsed: dict, pass_no: int) -> None:
        for pos, it in enumerate(parsed['items'], 1):
            snapshot.append({
                'page': pg, 'pos': pos, 'href': it['href'],
                'url_key': canonical_url_key(it['href']), 'type': it['type'],
                # ISO, so the verifier's Check AH can window and slug+date-match it
                # (the raw 'October 6, 2026' text compared as a string never filtered).
                'title': it['title'],
                'list_date': _iso_list_date(it['list_date']) or it['list_date'],
                'total_pages': parsed['total_pages'] if parsed['total_pages'] is not None else '',
                'items_header': parsed['items_header'], 'pass': pass_no,
            })
            seen_items.append((pg, it))

    print(f'[rubio_backfill] Fetching remarks index: {_REMARKS_INDEX}')
    try:
        p1 = fetch_listing(1)
    except Exception as e:
        # NOT a warning. This index is the primary discovery path for
        # current-era speeches: the /releases/office-of-the-spokes*/YYYY/MM/
        # pages frequently do not contain 'rubio' ('togo-independence-day').
        # Returning an empty list here is indistinguishable from "no speeches
        # this week", which is exactly how the 2026-10-12 run reported success
        # while silently skipping a week of speeches.
        raise DiscoveryError(
            f'remarks index {_REMARKS_INDEX} unreachable after '
            f'{_FETCH_ATTEMPTS} attempts: {e}'
        ) from e
    if not p1['items']:
        raise DiscoveryError(
            f'remarks index {_REMARKS_INDEX} returned no .collection-result links -- '
            f'the listing markup has changed (audit D-06).')
    max_page = max(p1['max_link_page'], p1['total_pages'] or 1)
    if max_page == 1 and len(p1['items']) >= 10:
        raise DiscoveryError(
            f'remarks index lists {len(p1["items"])} items on page 1 but exposes no '
            f'pagination (no /page/N/ links, no total_pages input) -- the pagination '
            f'markup has changed and the walk would silently stop at page 1 (D-06).')
    print(f'[rubio_backfill] Remarks archive: {max_page} pages to scan '
          f'(page links: {p1["max_link_page"]}, total_pages input: {p1["total_pages"]}, '
          f'header: {p1["items_header"]!r})')
    pages: dict[int, dict] = {1: p1}
    record(1, p1, 1)

    # Pages 2..max_page, stopping once we have walked past the floor.
    #
    # The archive is expected to be reverse-chronological, but that could NOT
    # be verified from the development network (state.gov 403s every
    # non-browser client), so the stop is deliberately conservative:
    #   * it needs TWO consecutive pages that are both entirely older than the
    #     floor and contributed nothing in-window, so a single pinned/featured
    #     item or one out-of-order insert cannot truncate the walk;
    #   * a page whose links expose no parseable month (URL or listed date)
    #     never counts toward the stop.
    stale_streak = 0
    stopped_at = None
    undated_pages = 0
    failed_pages: list[int] = []
    in_window_failed: list[int] = []
    for pg in range(2, max_page + 1):
        time.sleep(rate_limit)
        try:
            parsed = fetch_listing(pg)
        except Exception as e:
            print(f'[rubio_backfill] WARNING: remarks page {pg} unreadable: {e}', file=sys.stderr)
            failed_pages.append(pg)
            # Under the reverse-chronological assumption a page right after an
            # in-window page may hold in-window items: unreadable, it is fatal
            # (after one more try below). Once a stale page has been seen it is
            # out of the window. It neither counts toward the stop nor resets it.
            if stale_streak == 0:
                in_window_failed.append(pg)
            continue
        pages[pg] = parsed
        record(pg, parsed, 1)
        yms = [_item_ym(it) for it in parsed['items']]
        dated = [ym for ym in yms if ym is not None]
        oldest = min(dated) if dated else None
        n_in = sum(1 for ym in yms if ym is None or ym >= floor_ym)
        if parsed['items'] and oldest is None:
            undated_pages += 1
            print(f'[rubio_backfill] WARNING: remarks page {pg} had {len(parsed["items"])} '
                  f'link(s) but no parseable date -- not counting it toward early-stop.',
                  file=sys.stderr)
        if oldest is not None and oldest < floor_ym and n_in == 0:
            stale_streak += 1
            if stale_streak >= 2:
                stopped_at = pg
                print(f'[rubio_backfill] remarks early-stop after page {pg}/{max_page} '
                      f'(floor {since:%Y-%m}; page oldest {oldest[0]:04d}-{oldest[1]:02d})')
                break
        else:
            stale_streak = 0

        if pg % 10 == 0 or pg == max_page:
            print(f'  [remarks {pg}/{max_page}] {len(seen_items)} slots so far')

    # Second chance for unreadable pages, through the CDN with a cache-buster.
    for pg in list(failed_pages):
        time.sleep(rate_limit)
        try:
            parsed = fetch_listing(pg, cache_bust=True)
        except Exception as e:  # noqa: BLE001
            print(f'[rubio_backfill] WARNING: remarks page {pg} still unreadable: {e}',
                  file=sys.stderr)
            continue
        pages[pg] = parsed
        record(pg, parsed, 2)
        failed_pages.remove(pg)
        if pg in in_window_failed:
            in_window_failed.remove(pg)
    stats['failed_pages'] = sorted(set(failed_pages))
    if in_window_failed:
        raise DiscoveryError(
            f'{len(in_window_failed)} remarks listing page(s) inside the {since:%Y-%m} '
            f'window unreadable after retries: {in_window_failed[:10]} -- every item '
            f'listed there would be missed (audit D-05).')

    # Drift check: duplicate slots / short pages -> re-walk with a cache-buster.
    page_size = len(p1['items'])
    walked_max = stopped_at or max_page
    dup_slots, short_pages, affected = listing_drift(pages, page_size, max_page)
    n_pass1 = len(seen_items)
    dup_after, short_after = dup_slots, short_pages
    rewalked: list[int] = []
    if affected:
        print(f'[rubio_backfill] WARNING: {dup_slots} duplicate slot(s), {short_pages} short '
              f'page(s) -- re-walking pages {sorted(affected)} with a cache-buster (D-03)',
              file=sys.stderr)
        for pg in sorted(affected):
            time.sleep(rate_limit)
            try:
                parsed = fetch_listing(pg, cache_bust=True)
            except Exception as e:  # noqa: BLE001
                print(f'[rubio_backfill] WARNING: re-walk of page {pg} failed: {e}',
                      file=sys.stderr)
                continue
            pages[pg] = parsed
            record(pg, parsed, 2)
            rewalked.append(pg)
        dup_after, short_after, _ = listing_drift(pages, page_size, max_page)

    # Candidates: every link seen in either pass, once.
    candidates: list[Candidate] = []
    kept_keys: set[str] = set()
    excluded_keys: set[str] = set()
    in_window_slots = 0
    for n, (pg, it) in enumerate(seen_items):
        href = _strip_query(it['href'])
        key = canonical_url_key(href)
        ym = _item_ym(it)
        in_window = ym is None or ym >= floor_ym
        if n < n_pass1 and in_window:
            in_window_slots += 1
        if _is_excluded(href):
            if key not in excluded_keys and skiplog is not None:
                skiplog.add(href, 'excluded_path', 'discovery_listing', title=it['title'],
                            block_type=it['type'])
            excluded_keys.add(key)
            continue
        if key in kept_keys:
            continue
        if in_window or (in_corpus is not None and not in_corpus(href)):
            kept_keys.add(key)
            candidates.append(Candidate(href, 'archive', _iso_list_date(it['list_date'])))

    unique = len({canonical_url_key(it['href']) for _, it in seen_items})
    expected = p1['items_count'] if stopped_at is None else None
    reasons = []
    if failed_pages:
        reasons.append(f'out-of-window pages unreadable {failed_pages[:10]}')
    if dup_after:
        reasons.append(f'{dup_after} duplicate slot(s) after re-walk')
    if short_after:
        reasons.append(f'{short_after} short page(s) after re-walk')
    if expected is not None and unique < expected:
        reasons.append(f'{unique} unique links < {expected} in the items header')
    stats.update(listed=n_pass1, in_window=in_window_slots, unique=unique,
                 dup_slots=dup_slots, dup_slots_after=dup_after, short_pages=short_pages,
                 rewalked_pages=rewalked, max_page=max_page, walked_to=walked_max,
                 total_pages=p1['total_pages'], items_header=p1['items_header'],
                 stopped_at=stopped_at, archive_candidates=len(candidates),
                 complete=not reasons, incomplete_reasons=reasons)

    if stopped_at is None and max_page > 1 and floor_ym > (RUBIO_START.year, RUBIO_START.month):
        # Canary: the walk was supposed to be scoped but never stopped. The
        # pipeline still works, just slowly -- surface it rather than hide it.
        print(f'[rubio_backfill] WARNING: early-stop never fired -- walked all {max_page} '
              f'remarks pages despite a {since:%Y-%m} floor. Is the archive still '
              f'reverse-chronological?', file=sys.stderr)
    if undated_pages:
        print(f'[rubio_backfill] WARNING: {undated_pages} remarks page(s) exposed no dates.',
              file=sys.stderr)
    print(f'[rubio_backfill] remarks walk: listed={n_pass1} in_window={in_window_slots} '
          f'unique={unique} dup_slots={dup_slots} (after re-walk {dup_after}) '
          f'short_pages={short_pages} failed_pages={stats["failed_pages"]} '
          f'pages={walked_max}/{max_page} complete={not reasons}'
          + (f' -- {"; ".join(reasons)}' if reasons else ''))
    return candidates


def _dedup_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """First occurrence wins, matched on the URL key and on its renamed (live) form,
    so a spokesperson/spokesman pair of the same page is fetched once."""
    seen: set[str] = set()
    unique: list[Candidate] = []
    for c in candidates:
        url = _strip_query(c.url.strip())
        key = canonical_url_key(url)
        live = canonical_url_key(canonicalize_live_url(url))
        if not key or key in seen or live in seen:
            continue
        seen.update({key, live})
        unique.append(c._replace(url=url))
    return unique


def collect_candidate_urls(
    session: requests.Session,
    rate_limit: float,
    supplemental_file: Path | None = None,
    since: date = RUBIO_START,
    *,
    full_rescan: bool = False,
    sitemap_budget_min: float | None = _SITEMAP_BUDGET_MIN,
    in_corpus=None,
    snapshot: list | None = None,
    skiplog: "SkipLog | None" = None,
    stats: dict | None = None,
    run_id: str = '',
    extra: list[Candidate] | tuple = (),
) -> list[Candidate]:
    """Remarks archive + supplemental/pending + sitemaps → deduplicated candidates.

    Order matters for the dedup (first wins): archive links carry their listing
    date and are never capped, supplemental and pending URLs come next, sitemap
    hits last.

    Raises DiscoveryError when the archive could not be read, rather than
    returning a short list. "Nothing new this week" and "the crawl failed"
    look identical downstream, and conflating them is how the 2026-10-12 run
    reported success while skipping a week of speeches. Sitemaps stay
    best-effort, but get the retry budget in incremental runs too.
    """
    stats = stats if stats is not None else {}
    # discover_remarks_urls raises DiscoveryError if its index is unreachable,
    # a page inside the window is unreadable, or the markup changed.
    archive = discover_remarks_urls(session, rate_limit, since, in_corpus=in_corpus,
                                    snapshot=snapshot, skiplog=skiplog, stats=stats,
                                    run_id=run_id)
    sm_stats: dict = {}
    sitemap = discover_sitemap_urls(
        session, rate_limit, since,
        budget_min=None if full_rescan else sitemap_budget_min,
        skiplog=skiplog, stats=sm_stats)
    stats.update(sm_stats)

    supplemental: list[Candidate] = []
    if supplemental_file:
        supplemental = [Candidate(u, 'supplemental') for u in read_url_list(Path(supplemental_file))]
        print(f'[rubio_backfill] Added supplemental URLs from {Path(supplemental_file).name}')

    print(f'[rubio_backfill] discovery: {len(archive)} from the remarks archive, '
          f'{len(sitemap)} from sitemaps ({sm_stats.get("sitemap_page_hits", 0)} page, '
          f'{sm_stats.get("sitemap_flat_hits", 0)} flat, '
          f'{sm_stats.get("sitemap_release_hits", 0)} release), '
          f'{len(supplemental) + len(extra)} supplemental/pending/held')
    return _dedup_candidates(archive + supplemental + list(extra) + sitemap)


# ---------------------------------------------------------------------------
# Step 3a: Rich page fetch
# ---------------------------------------------------------------------------

def _flatten_jsonld(ld_data) -> dict:
    """Flatten a JSON-LD value (dict or list-of-dicts) into a single dict."""
    if isinstance(ld_data, list):
        merged: dict = {}
        for item in ld_data:
            if isinstance(item, dict):
                merged.update(item)
        return merged
    if isinstance(ld_data, dict):
        return ld_data
    return {}


# Inline elements: their text joins the surrounding line instead of starting a new
# one. Everything else (p, div, li, h1-h6, td, button, ...) is a line of its own.
_INLINE_TAGS = frozenset({
    'a', 'abbr', 'b', 'bdi', 'bdo', 'cite', 'code', 'data', 'del', 'dfn', 'em',
    'font', 'i', 'ins', 'kbd', 'label', 'mark', 'q', 's', 'samp', 'small', 'span',
    'strike', 'strong', 'sub', 'sup', 'time', 'tt', 'u', 'var', 'wbr',
})
# Same exclusions as get_text(): script/style/template bodies are not page text.
_SKIP_TAGS = frozenset({'script', 'style', 'noscript', 'template'})


def _block_text(soup) -> str:
    """Page text with one line per block element (inline tags joined, whitespace
    collapsed). The line layout otherwise mirrors get_text('\n', strip=True), so the
    hide / Tags anchors of _extract_modern_state_content still apply."""
    lines: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        line = ' '.join(''.join(buf).split())
        if line:
            lines.append(line)
        buf.clear()

    def walk(node) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                if type(child) in (NavigableString, CData):
                    buf.append(str(child))
                continue
            name = (child.name or '').lower()
            if name in _SKIP_TAGS:
                continue
            if name == 'br':
                flush()
            elif name in _INLINE_TAGS:
                walk(child)
            else:
                flush()
                walk(child)
                flush()

    walk(soup)
    flush()
    return '\n'.join(lines)


class FetchFailure(Exception):
    """A page could not be fetched. kind: 'fetch_error', or 'external_redirect' when
    the request had been redirected off state.gov (audit F-02: /external-content/
    -> whitehouse.gov, intermittently 403)."""

    def __init__(self, kind: str, detail: str, final_url: str = ''):
        super().__init__(f'{kind}: {detail}')
        self.kind = kind
        self.detail = detail
        self.final_url = final_url


def _is_state_gov(url: str) -> bool:
    host = (urlsplit(url or '').hostname or '').lower()
    return host == 'state.gov' or host.endswith('.state.gov')


def _failed_final_url(exc: Exception) -> str:
    """Where a failed request ended up (after redirects), when requests says."""
    for attr in ('response', 'request'):
        obj = getattr(exc, attr, None)
        u = getattr(obj, 'url', None) if obj is not None else None
        if u:
            return str(u)
    return ''


def fetch_rubio_page(url: str, session: requests.Session, *,
                     attempts: int = _FETCH_ATTEMPTS,
                     raise_errors: bool = False) -> dict | None:
    """Fetch a www.state.gov page; return metadata + raw_text dict, or None on error.

    Uses _get_with_retry (timeout (10, 45), backoff) -- it used to be one
    session.get(timeout=30) with no retry, so a single read timeout cost the page
    until the floor moved past it (audit F-01; 15 timeouts in the CI logs). With
    raise_errors=True a failure raises FetchFailure instead of returning None, so
    run() can queue the URL in the pending file with the reason.
    """
    try:
        resp = _get_with_retry(session, url, attempts=attempts, label=url)
    except Exception as e:  # noqa: BLE001 -- requests raises a wide family
        final = _failed_final_url(e)
        kind = 'external_redirect' if final and not _is_state_gov(final) else 'fetch_error'
        print(f'  [fetch] ERROR {url}: {e}'
              + (f' (redirected off state.gov to {final})' if kind == 'external_redirect' else ''),
              file=sys.stderr)
        if raise_errors:
            raise FetchFailure(kind, f'{type(e).__name__}: {e}'[:300],
                               final if kind == 'external_redirect' else '') from e
        return None
    final_url = str(getattr(resp, 'url', '') or url)
    if not _is_state_gov(final_url):
        print(f'  [fetch] NOTE {url} redirected off state.gov to {final_url} '
              f'(external_redirect; parsed as fetched)', file=sys.stderr)

    try:
        soup = _make_soup(resp.text)
    except Exception as e:
        print(f'  [fetch] Parse ERROR {url}: {e}', file=sys.stderr)
        if raise_errors:
            raise FetchFailure('fetch_error', f'parse error: {e}'[:300]) from e
        return None

    raw_text = soup.get_text('\n', strip=True)
    # Per-block text (audit X-05/X-09): get_text('\n') puts a newline at every
    # inline-tag boundary, so "<strong>SECRETARY RUBIO</strong>:" became
    # "SECRETARY RUBIO\n:" and footnote <sup>s became orphan lines. raw_text stays
    # as it was (the corpus Raw Text column and _parse_block_metadata depend on its
    # line layout); extract_row uses content_text only when it carries exactly the
    # same characters as raw_text's content block.
    try:
        content_text = _extract_modern_state_content(_block_text(soup))
    except Exception as e:  # noqa: BLE001 -- never lose a page to this refinement
        print(f'  [fetch] per-block text failed for {url}: {e}', file=sys.stderr)
        content_text = ''

    # Parse JSON-LD structured data
    ld: dict = {}
    for tag in soup.find_all('script', type='application/ld+json'):
        try:
            parsed = json.loads(tag.string or '{}')
            ld.update(_flatten_jsonld(parsed))
        except (json.JSONDecodeError, Exception):
            pass

    # Extract location from JSON-LD contentLocation (may be string or dict)
    cl = ld.get('contentLocation', '')
    if isinstance(cl, dict):
        jsonld_location = cl.get('name', '') or cl.get('addressLocality', '')
    else:
        jsonld_location = str(cl) if cl else ''

    title = ''
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    og = soup.find('meta', attrs={'property': 'og:title'})
    og_title = (og.get('content') or '').strip() if og else ''

    return {
        'raw_text': raw_text,
        'content_text': content_text,
        'jsonld_date_published': str(ld.get('datePublished', '')),
        'jsonld_location_name': jsonld_location,
        'article_meta_doctype': str(ld.get('@type', '')),
        'title': title,
        'og_title': og_title,
        'jsonld_headline': str(ld.get('headline', '') or ''),
        'final_url': final_url,
    }


def page_headline(page: dict) -> str:
    """The Title: the page headline, by the same rule the corpus Title patch and
    verifier Check AJ use (corpus_title.page_title on Raw Text: the on-page
    headline, else the <title>), then the JSON-LD headline / og:title. Never the
    URL slug or the speech text -- with nothing found it stays empty, and the
    per-row gate's Empty Title check holds the row."""
    headline, _ = corpus_title.page_title(page.get('raw_text') or '')
    for cand in (headline, page.get('jsonld_headline'), page.get('og_title'), page.get('title')):
        cand = corpus_title.clean_title(cand or '')
        if cand and not corpus_title._BARE_TYPE_RE.match(cand):
            return cand
    return ''


# ---------------------------------------------------------------------------
# Step 3c: Metadata extraction from page block
# ---------------------------------------------------------------------------

def _is_byline_line(line: str) -> bool:
    """A meta line is a byline when it is the Office of the Spokesperson or
    "Name, Title" (title words after the first comma); anything else between the
    type line and the date is a dateline (venue, city). Same rule as
    rubio_ground_truth.parse_block."""
    if _SPOKES_OFFICE_RE.match(line):
        return True
    name, sep, rest = line.partition(', ')
    return bool(sep) and len(name) <= 60 and bool(_BYLINE_TITLE_RE.search(rest))


def _parse_block_metadata(raw: str) -> dict:
    """Extract title, speech_type, byline(s), location, date from the hide→Tags block.

    Block structure (after the hide anchor):
        {Title}
        {Speech type}                ← e.g. "Press Statement", "Remarks", "Media Note"
        {Name}, Secretary of State   ← byline(s): one or more, or "Office of the Spokesperson"
        {Venue / City}               ← optional dateline line(s)
        {Date}                       ← standalone Month DD, YYYY line
        {speech content}

    Keys: block_title, block_speech_type, block_date, block_bylines (list),
    block_byline (' | '-joined), block_location (the last dateline line).
    The byline is its own field now (audit C-02): it used to fall into
    block_location whenever no dateline followed it, so the media notes' "Office
    of the Spokesperson" became a Location (17461, 17463) and so did the RFK Jr.
    byline (17609, M-01).
    """
    m = _HIDE_ANCHOR_RE.search(raw)
    if not m:
        return {}

    after_hide = raw[m.end():]
    end_pos = after_hide.find(_TAGS_SENTINEL)
    if end_pos == -1:
        end_pos = after_hide.find('\nBack to Top\n')
    if end_pos == -1:
        end_pos = len(after_hide)

    block = after_hide[:end_pos]
    lines = [l.strip() for l in block.split('\n') if l.strip()]

    result: dict = {}
    date_idx = -1
    for i, line in enumerate(lines):
        if _DATE_LINE_RE.match(line):
            date_idx = i
            result['block_date'] = line
            break

    if date_idx == -1:
        return result

    meta_lines = lines[:date_idx]
    if meta_lines:
        result['block_title'] = meta_lines[0]

    # Speech type: 2nd meta line (index 1), after title (index 0)
    if len(meta_lines) >= 2:
        result['block_speech_type'] = meta_lines[1]

    bylines: list[str] = []
    datelines: list[str] = []
    for line in meta_lines[2:]:
        (bylines if _is_byline_line(line) else datelines).append(line)
    result['block_bylines'] = bylines
    result['block_byline'] = ' | '.join(bylines)

    # Location: the last dateline line, if it doesn't name the Secretary. Never a
    # byline, and never the type line (a title/type/date-only block has none).
    for candidate in reversed(datelines):
        if 'Secretary of State' not in candidate and 'Rubio' not in candidate:
            result['block_location'] = candidate
            break

    return result


def _parse_date(jsonld_date: str, url: str, block_date: str) -> str:
    """Resolve Date in ISO YYYY-MM-DD format from JSON-LD, URL, or block."""
    # JSON-LD: may be "2025-01-21" or "2025-01-21T00:00:00+00:00"
    if jsonld_date:
        try:
            return date.fromisoformat(jsonld_date[:10]).isoformat()
        except ValueError:
            pass

    # URL date pattern
    d = parse_date_from_url(url)
    if d:
        return d.isoformat()

    # Block date string: "January 21, 2025" → parse to ISO
    if block_date:
        try:
            return datetime.strptime(block_date.strip(), '%B %d, %Y').date().isoformat()
        except ValueError:
            pass

    return ''


def _resolve_speech_type(page_meta: dict, block_meta: dict, url: str) -> str:
    """Resolve speech_type using JSON-LD doctype, block text, title, and URL fallback."""
    # URL-based overrides run first: these URL patterns unambiguously identify the speech
    # type regardless of what the JSON-LD metadata says (which may say 'remarks' generically).
    low = url.lower()
    if ('before-the-senate' in low or 'before-the-house' in low
            or 'before-the-committee' in low or 'confirmation-hearing' in low
            or ('appropriations' in low and ('hearing' in low or 'testimony' in low))):
        return 'testimony'
    if 'remarks-to-the-press' in low or 'remarks-to-press' in low:
        return 'remarks to the press'

    # Try guess_speech_type with available fields (it keyword-matches doctype + title)
    row_for_guess = {
        'article_meta_doctype': page_meta.get('article_meta_doctype', ''),
        'title': page_meta.get('title', ''),
    }
    # Supplement doctype with block speech_type if JSON-LD @type is generic
    block_st = block_meta.get('block_speech_type', '')
    if block_st and not row_for_guess['article_meta_doctype']:
        row_for_guess['article_meta_doctype'] = block_st

    # "Media Note" is NOT a Speech Type (decided 2026-10-21: no vocabulary
    # change). Spokesperson media notes (17461, 17463) are rejected by the
    # byline gate; a Rubio-bylined one that survives carries his own words
    # (e.g. the 2026-05-20 Cuban video message) and takes the existing
    # vocabulary below, as 17528 'Video Remarks' -> Remarks did.

    stype = guess_speech_type(row_for_guess)
    if stype and stype != 'unknown':
        return stype

    # URL keyword fallback
    if 'interview' in low or '-with-' in low:
        return 'interview'
    if 'remarks' in low:
        return 'remarks'
    if 'statement' in low:
        return 'statement'
    if 'press-availability' in low or 'press_availability' in low:
        return 'press availability'
    return 'remarks'  # sensible default for SoS pages


_LOC_NORMALIZE = {
    'Washington, DC': 'Washington, D.C.',
    'Washington DC': 'Washington, D.C.',
    # 'Office of the Spokesperson' is a byline, not a place; it no longer reaches
    # block_location (see _parse_block_metadata).
    'Aboard Air Force One': 'Washington, D.C.',
}


def _resolve_location(page_meta: dict, block_meta: dict) -> str:
    """Resolve Location from JSON-LD or the block dateline, normalized to corpus
    conventions; '' when the page gives neither.

    No blanket 'Washington, D.C.' default any more (plan step 6a, M-04): a
    spoken item with no dateline is not evidence of a Washington event, and a
    filled-in value here passed corpus_rubio_update's strict normalizer, so
    provisional_location (which never invents Washington) never ran. The
    caller applies the newsroom default to written statements only, after
    classification has decided the modality.
    """
    loc = page_meta.get('jsonld_location_name', '').strip()
    if not loc:
        loc = block_meta.get('block_location', '').strip()
    return _LOC_NORMALIZE.get(loc, loc)


# ---------------------------------------------------------------------------
# Step 3b + 3d: Extract + validate
# ---------------------------------------------------------------------------

def _collect_rubio_turns(speaker_text: str) -> str:
    """Concatenate every Secretary Rubio turn in a multi-speaker transcript.

    Thin wrapper over rubio_turns.rubio_text (turns end only at real labels, so
    "VICE PRESIDENT VANCE:" interjections, Unicode labels and split "LABEL\\n:"
    labels are all boundaries; no minimum turn length).
    """
    text, _ = rubio_turns.rubio_text(speaker_text)
    return text


def _third_person_opening(text: str) -> str:
    """The third-person opening `text` starts with ('' if none)."""
    # Normalize whitespace and smart quotes before matching so \xa0, curly apostrophes,
    # and multiple spaces don't prevent pattern matches.
    sample = ' '.join((text or '')[:500].split())
    sample = sample.replace('\u2018', "'").replace('\u2019', "'")
    for pat in _THIRD_PERSON_OPENINGS:
        if sample.startswith(pat):
            return pat
    m = _THIRD_PERSON_AND_RE.match(sample)
    return m.group(0) if m else ''


def _transcript_marker_cut(block: str) -> tuple[str, str]:
    """(marker, text after the marker's line) for the LAST transcript marker in the
    intro of `block`, or ('', '') when there is none.

    The Cuban Independence Day video message (audit C-01) reads: third-person intro,
    "A courtesy translation of the Secretary's video message is below.", then
    "Translated Transcript", then his words -- the last marker's line ends the intro.
    """
    intro = (block or '')[:_MARKER_SEARCH_CHARS]
    last = None
    for m in _TRANSCRIPT_MARKER_RE.finditer(intro):
        last = m
    if last is None:
        return '', ''
    eol = block.find('\n', last.end())
    after = block[eol + 1:].strip() if eol != -1 else ''
    return last.group(0), after


def _active_speaker_decision(url: str, speech_text: str, block: str = '',
                             n_rubio_turns: int = 0) -> tuple[bool, str, str]:
    """(keep, reason, text_after_marker) for the third-person gate (audit C-01).

    A third-person opening ("Today, Secretary of State Marco Rubio delivered ...")
    used to drop the page outright, and with it the one video message whose
    translated transcript followed the intro. Now an opening only drops the page
    when the block has neither a transcript marker nor any Rubio speaker label.
    With a marker, text_after_marker is his text (empty when the marker ends the
    block); with labels, the labelled turns already are.
    """
    if url.rstrip('/').endswith('/remarks-secretary-rubio'):
        return False, 'index_page', ''
    opening = _third_person_opening(speech_text)
    if not opening:
        return True, '', ''
    if n_rubio_turns:
        return True, 'third_person_intro+rubio_labels', ''
    marker, after = _transcript_marker_cut(block or speech_text)
    if marker:
        return True, f'third_person_intro+marker:{marker.lower()}', after
    if opening.startswith('The below is attributable') or _THIRD_PERSON_AND_RE.match(opening):
        return False, 'readout', ''
    return False, 'third_person', ''


def _is_active_speaker(url: str, speech_text: str, extraction_mode: str = '') -> bool:
    """False for pages where Rubio is discussed but not actively speaking."""
    keep, _, _ = _active_speaker_decision(url, speech_text, speech_text,
                                          int('rubio_turns' in (extraction_mode or '')))
    return keep


class PageDecision(NamedTuple):
    keep: bool
    reason: str           # why kept / skipped (skipped-sidecar reason when not kept)
    modality: str         # spoken | written | video | joint ('' when skipped)
    speech_text: str      # replacement SOS text (text after a transcript marker), or ''
    labels: list          # distinct speaker labels in the block (for the sidecar)


def _byline_decision(block_meta: dict, title: str, n_rubio_turns: int,
                     trusted: bool = True, text: str = '') -> tuple[bool, str, bool]:
    """(keep, reason, joint) from the block byline lines (audit C-02).

    Keep when a byline names Rubio, when the block has Rubio speaker labels, or for
    a joint statement he co-signs. Spokesperson bylines ("Office of the
    Spokesperson" media notes, rows 17461/17463) are third-person releases about
    him. A page with no parseable byline (legacy layout, /external-content/) is
    kept only when `trusted` -- found by a curated source (the remarks archive,
    the supplemental list, the held queue). A sitemap-only page with no byline
    and no Rubio labels is a bureau notice (Foreign Military Sales, TIP
    determinations: the 2026-10-21 dry run merged six) and is rejected. The byline is the meta line,
    never a substring search -- "Office of the Spokesperson" is in the footer of
    every page.
    """
    bylines = block_meta.get('block_bylines') or []
    btype = (block_meta.get('block_speech_type') or '').strip().lower()
    title = block_meta.get('block_title') or title or ''
    rubio_by = any('Rubio' in b for b in bylines)
    others = [b for b in bylines if 'Rubio' not in b and not _SPOKES_BYLINE_RE.search(b)]
    joint_title = (bool(_JOINT_TITLE_RE.search(title))
                   or bool(_JOINT_RELEASED_RE.search((text or '')[:600])))
    is_statement = 'statement' in btype or 'joint statement' in title.lower()
    joint = (rubio_by and bool(others) and is_statement) or joint_title
    if rubio_by:
        return True, 'rubio_byline', joint
    if n_rubio_turns:
        return True, 'rubio_labels', joint
    if joint_title:
        return True, 'joint_cosigned', True
    if not bylines:
        return (True, 'no_byline', False) if trusted else (False, 'no_byline_untrusted', False)
    if any(_SPOKES_BYLINE_RE.search(b) for b in bylines):
        return False, 'spokesperson_byline', False
    return False, 'other_byline', False


def _modality(stype: str, block: str, n_rubio_turns: int, marker_reason: str,
              joint: bool) -> str:
    """spoken | written | video | joint (decided-scope D2)."""
    if joint:
        return 'joint'
    # Video: a label-less video message (transcript marker under a third-person
    # intro, or a non-statement type mentioning one). A written Press Statement
    # that merely links "my video message" (17547) stays written.
    st = (stype or '').strip().lower()
    if (not n_rubio_turns and _VIDEO_RE.search((block or '')[:_MARKER_SEARCH_CHARS])
            and ('marker' in marker_reason or st not in _WRITTEN_SPEECH_TYPES)):
        return 'video'
    if n_rubio_turns or 'marker' in marker_reason:
        return 'spoken'
    if (stype or '').strip().lower() in _SPOKEN_SPEECH_TYPES:
        return 'spoken'
    return 'written'


# Sources whose listing already says "this is Rubio" (the curated archive, a
# hand-checked supplemental entry, a row held from an earlier run). Sitemaps
# list every release on state.gov, so a sitemap-only page needs positive evidence.
_TRUSTED_SOURCES = frozenset({'archive', 'supplemental', 'held'})


def classify_rubio_page(url: str, page: dict, block_meta: dict, extracted: dict,
                        trusted: bool = True) -> PageDecision:
    """Post-fetch decision: is this page Rubio speaking or writing in his own voice?

    Runs the third-person gate (with transcript markers), then the byline gate,
    and assigns the Modality. Every page discovered from any source goes through
    this, which is what lets discovery read the release sitemaps unfiltered.
    """
    mode = extracted.get('extraction_mode', '')
    n_turns = int('rubio_turns' in mode)
    block = extracted.get('speaker_text') or extracted.get('speech_text') or ''
    try:
        labels = rubio_turns.rubio_text(block)[1].get('labels', [])
    except Exception:  # noqa: BLE001 -- the sidecar inventory must never cost a page
        labels = []
    keep, reason, after = _active_speaker_decision(url, extracted.get('speech_text', ''),
                                                   block, n_turns)
    if not keep:
        return PageDecision(False, reason, '', '', labels)
    by_keep, by_reason, joint = _byline_decision(block_meta, page.get('title', ''), n_turns,
                                                 trusted, text=block)
    # A transcript marker under a third-person intro is evidence on its own (a
    # Spokesperson-bylined video message would still be his words).
    if not by_keep and 'marker' not in reason:
        return PageDecision(False, by_reason, '', '', labels)
    stype = _resolve_speech_type(page, block_meta, url)
    modality = _modality(stype, block, n_turns, reason, joint)
    return PageDecision(True, reason or by_reason, modality, after, labels)


def _is_nav_boilerplate(text: str) -> bool:
    """True when extracted text starts with state.gov nav menu rather than speech content."""
    sample = text[:500]
    return any(sig in sample for sig in _NAV_BOILERPLATE_SIGNALS)


def _has_non_rubio_label(text: str) -> bool:
    """True if text contains any speaker label that is not a Secretary Rubio label."""
    return any(t.label is not None and not t.is_rubio for t in rubio_turns.segment(text))


# ---------------------------------------------------------------------------
# Content block (plan step P1a) and footnote errata (P1e)
# ---------------------------------------------------------------------------

def _alnum(text: str) -> str:
    return re.sub(r'[\W_]+', '', text or '')


def _rubio_content_block(page: dict) -> tuple[str, str]:
    """(block, source): the hide -> Tags/"Back to Top" content block of the page.

    Extraction used to run on the whole page, so the last Rubio turn ran on into the
    footer and was vetoed for containing "Contact Us" (audit X-01, 68 rows). Bounding
    first removes the footer before any turn is cut. The per-block content_text from
    fetch_rubio_page is preferred when it has exactly the same characters (ignoring
    whitespace and punctuation) as raw_text's block -- i.e. it only moved line
    breaks; otherwise raw_text's block is used.
    """
    raw_block = _extract_modern_state_content(page.get('raw_text', '') or '')
    ct = page.get('content_text') or ''
    # Back-link footnotes (bare-number <sup> marker, definition + "↩︎") are
    # parsed in raw_text's line layout. The per-block text glues the marker onto
    # its word ("simply starting1 a war", footnote "ending"), so the correction is
    # never applied and the footnote body trails the last turn (2025-12-04
    # Institute of Peace remarks, held by Check AD in the 2026-10-21 dry run).
    # Bracketed "[k]" footnotes survive per-block text and still use it.
    if _has_backlink_footnotes(raw_block):
        return raw_block, 'raw'
    if ct and raw_block and _alnum(ct) == _alnum(raw_block):
        return ct, 'per_block'
    return raw_block, 'raw'


def _has_backlink_footnotes(block: str) -> bool:
    """True when the block has a back-link footnote list (an own-line arrow)."""
    return any(ln.strip() in _FN_BACKLINK for ln in block.split('\n'))


# "[1]" / "[ii]" footnote markers (lowercase roman only, so "[I]t's" is not one).
_FN_KEY = r'(\d{1,2}|[ivx]{1,5})'
_FN_OWNLINE_RE = re.compile(rf'^\s*\[{_FN_KEY}\]\*?\s*(.*)$')
_FN_INLINE_RE = re.compile(rf'\s*\[{_FN_KEY}\]\*?')
# A linked marker flattened by get_text('\n') into "[\n1\n]*" (17415).
_FN_SPLIT_RE = re.compile(rf'\[[ \t]*\n\s*{_FN_KEY}[ \t]*\n\s*\]\*?[ \t]*\n?')
_FN_BACKLINK = ('\u21a9\ufe0e', '\u21a9\ufe0f', '\u21a9', '\u2191')   # back-link arrows
# A line that starts with an uppercase speaker label ("QUESTION:", "SECRETARY RUBIO")
# or a split colon; footnote folding never joins such a line onto the sentence above.
_LABEL_START_RE = re.compile(r'^\s*(?::|[^a-z:]{1,60}(?::|$))')
_FN_SEPARATOR_RE = re.compile(r'^\s*(?:_{3,}|-{3,}|\*{3,})\s*$')


def _fn_norm(word: str) -> str:
    return re.sub(r'[\W_]+', '', word).lower()


def _apply_correction(before: str, fix: str, after: str = '') -> tuple[str, str]:
    """Replace the words around a footnote marker with the footnote's correction.

    The footnotes on these pages are official errata for the word(s) before the
    marker: "extraordinarily[1]" -> "extraordinary", "secretary of states[2]" ->
    "secretaries of state", "February of 2024[1]" -> "2025". The replaced span starts
    at the nearest preceding word equal to (or sharing a 4-letter prefix with) the
    correction's first word; failing that, it is as many words as the correction
    has. When the correction runs past the marker -- "Kingdom of Norway[i] but also
    Denmark" with "Kingdom of Denmark but also Greenland" (17686) -- the matching
    words after the marker are replaced too. A long correction with no anchor is an
    explanatory note (17008: "Washington D.C. Secretary Rubio met with..."), not an
    erratum: only the marker is dropped.

    Returns (new_before, new_after).
    """
    fw = fix.split()
    words = list(re.finditer(r'\S+', before))
    if not fw or not words:
        return before, after
    n = len(fw)
    reach = min(len(words), n + 2)
    k = None
    if n <= 8:
        first = _fn_norm(fw[0])
        for kk in range(1, reach + 1):
            if _fn_norm(words[-kk].group()) == first:
                k = kk
                break
        if k is None and len(first) >= 4:
            for kk in range(1, reach + 1):
                w = _fn_norm(words[-kk].group())
                if len(w) >= 4 and w[:4] == first[:4]:
                    k = kk
                    break
    if k is None:
        if n > 4:
            return before, after
        k = min(n, len(words))
    last = words[-1].group()
    trail = re.search(r'[.,;:!?”"\')]+$', last)
    repl = fix
    if trail and not re.search(r'[.,;:!?”"\')]$', fix):
        repl += trail.group()
    # Correction continues past the marker: consume the words it re-states.
    rest = fw[k:]
    if rest:
        aw = list(re.finditer(r'\S+', after))
        if aw and _fn_norm(aw[0].group()) == _fn_norm(rest[0]):
            m = min(len(rest), len(aw))
            after = after[aw[m - 1].end():]
    return before[:words[-k].start()] + repl, after


def _apply_footnote_errata(block: str) -> tuple[str, int]:
    """Apply state.gov footnote errata to a content block and drop the footnote list.

    Two layouts occur (audit X-09; rows 17006 17008 17251 17255 17415 17492 17502
    17578 17592 17686 17700); a linked marker split as "[\n1\n]*" is rejoined first:
      "[k]" ... "[k] text" -- the definition is the LAST occurrence of "[k]", on its
        own line, with its text inline or on the next line; earlier occurrences are
        references.
      reference "[k]" or a bare "k" line, definition "text" on the line before a
        "↩︎" / "↑" back-link (17492, 17578, 17006).
    Returns (new_block, n_corrections_applied). An own-line "[k]" with no definition
    is just removed; an inline "[n]" with no definition is left alone (it may be
    real text).
    """
    lines = _FN_SPLIT_RE.sub(r'[\1]\n', block).split('\n')
    defs: dict[str, str] = {}
    drop: set[int] = set()

    # Layout 1: "[k]" definitions.
    occ: dict[str, list[tuple[int, bool]]] = {}     # key -> [(line, own_line)]
    for i, ln in enumerate(lines):
        m = _FN_OWNLINE_RE.match(ln)
        if m:
            occ.setdefault(m.group(1), []).append((i, True))
        else:
            for mm in _FN_INLINE_RE.finditer(ln):
                occ.setdefault(mm.group(1), []).append((i, False))
    for key, seen in occ.items():
        # A definition is the last occurrence, on its own line, after a reference.
        if len(seen) < 2 or not seen[-1][1]:
            continue
        d = seen[-1][0]
        text = _FN_OWNLINE_RE.match(lines[d]).group(2).strip()
        drop.add(d)
        j = d + 1
        if not text and j < len(lines) and not _FN_OWNLINE_RE.match(lines[j]):
            text = lines[j].strip()
            drop.add(j)
            j += 1
        if j < len(lines) and lines[j].strip() in _FN_BACKLINK:
            drop.add(j)
        defs[key] = text

    # Layout 2: "text" + back-link line; the n-th back-link defines key n.
    backlinks = [i for i, ln in enumerate(lines)
                 if ln.strip() in _FN_BACKLINK and i not in drop]
    if backlinks:
        first_bl = backlinks[0]
        refs = {}                                  # key -> line indexes (before list)
        for i, ln in enumerate(lines[:first_bl]):
            s = ln.strip()
            m = _FN_OWNLINE_RE.match(ln)
            if m and not m.group(2) and m.group(1) not in defs:
                refs.setdefault(m.group(1), []).append(i)
            elif re.fullmatch(r'\d{1,2}', s):
                refs.setdefault(s, []).append(i)
        for n, bl in enumerate(backlinks, 1):
            key = str(n)
            if bl >= 1 and key in refs and (bl - 1) not in drop:
                defs[key] = lines[bl - 1].strip()
                drop.update({bl - 1, bl})
                for i in refs[key]:
                    lines[i] = f'[{key}]'
            else:
                drop.add(bl)                       # stray back-link arrow
                # An orphan definition (nothing in the text references it) sitting
                # just under the "____" separator is part of the footnote list, not
                # speech: drop it with its arrow rather than leave it trailing the
                # last turn.
                if (bl >= 1 and (bl - 1) not in drop and any(
                        _FN_SEPARATOR_RE.match(lines[k]) for k in range(max(0, bl - 4), bl - 1))):
                    drop.add(bl - 1)

    has_orphan = any((m := _FN_OWNLINE_RE.match(ln)) and not m.group(2) for ln in lines)
    if not defs and not drop and not has_orphan:
        return block, 0     # (a split "[\n1\n]" alone never occurs without a list)

    # Separator line(s) directly above the first definition belong to the list.
    if drop:
        k = min(drop) - 1
        while k >= 0 and (_FN_SEPARATOR_RE.match(lines[k]) or not lines[k].strip()):
            drop.add(k)
            k -= 1
    kept = [ln for i, ln in enumerate(lines) if i not in drop]

    # Fold own-line references into their sentence, then apply each correction.
    out: list[str] = []
    applied = 0
    i = 0
    while i < len(kept):
        m = _FN_OWNLINE_RE.match(kept[i])
        if m and not m.group(2) and out:
            key = m.group(1)
            prev = out.pop()
            nxt = kept[i + 1] if i + 1 < len(kept) else ''
            glue = bool(nxt) and not _LABEL_START_RE.match(nxt)
            if key in defs:
                prev, tail = _apply_correction(prev, defs[key], nxt if glue else '')
                if glue:
                    nxt = tail
                applied += 1
            if glue:
                sep = '' if re.match(r'\s*[,.;:!?)\]]', nxt) else ' '
                out.append((prev.rstrip() + sep + nxt.lstrip()).strip())
                i += 2
            else:
                out.append(prev)         # never glue a speaker label onto a sentence
                i += 1
            continue
        if m and not m.group(2):
            i += 1                       # leading orphan marker: drop it
            continue
        ln = kept[i]
        for mm in reversed(list(_FN_INLINE_RE.finditer(ln))):
            if mm.group(1) in defs:
                head, tail = _apply_correction(ln[:mm.start()], defs[mm.group(1)],
                                               ln[mm.end():])
                ln = head + tail
                applied += 1
        out.append(ln)
        i += 1
    return '\n'.join(out), applied


# Trailing "# # #" end-of-release marker (17008); not text.
_END_MARK_RE = re.compile(r'(?:\n\s*#(?:\s*#)+\s*)+$')


# No-content-block fallback (17095, a whitehouse.gov page reached via
# /external-content/: one long line, no hide/Tags anchors). The body runs from the
# first date in the header to the "Related" / "Notifications" nav.
_NOBLOCK_DATE_RE = re.compile(
    r'(?:January|February|March|April|May|June|July|August|September|October|'
    r'November|December)\s+\d{1,2},\s+\d{4}'
)
_NOBLOCK_END_RE = re.compile(r'(?:^|\s)(?:Related|Notifications|Tags|Back to Top)(?=\s|$)')


def _dated_body_fallback(raw: str) -> str:
    m = _NOBLOCK_DATE_RE.search(raw[:1500])
    if not m:
        return ''
    body = raw[m.end():]
    e = _NOBLOCK_END_RE.search(body)
    if e:
        body = body[:e.start()]
    body = body.strip()
    return body if len(body.split()) >= 20 else ''


def extract_row(url: str, page: dict) -> dict | None:
    """Extract (speaker_text, speech_text, mode) and validate; return None to skip.

    Rubio path (plan step P1, audit X-01..X-10):
      1. Bound the page to its content block (hide -> Tags) BEFORE extraction, so no
         turn can run into the footer (X-01). This replaces the old after-the-fact
         Tags cut (former Pass 5).
      2. Apply footnote errata and drop the footnote list (X-09).
      3. If the block has >= 1 Rubio label (rubio_turns), speech_text is exactly his
         turns (X-03..X-06; stage directions incl. "(Via translation)" removed).
         Otherwise the page is label-less -- a written statement, op-ed or video
         message in his own voice -- and the block itself is his text.
      4. No content block at all: make_speaker_text without the half-text fallback
         (X-08), then the label path or the legacy extractor without the whole-chunk
         boilerplate veto (X-02); failing that, a dated-body fallback, else the row
         is returned with an empty speech_text for review.
    Extra key: untranscribed_rubio_segments (X-10).
    """
    raw = page['raw_text']
    block, block_src = _rubio_content_block(page)
    n_errata = 0
    if block.strip():
        block, n_errata = _apply_footnote_errata(block)
        block = _END_MARK_RE.sub('', block).rstrip()

    if block.strip():
        speaker_text = block
        speech_text, stats = rubio_turns.rubio_text(block, log=True, context=url)
        if stats['n_rubio_turns']:
            extraction_mode = 'no_checkpoint+rubio_turns'
            untranscribed = stats['untranscribed_segments']
        else:
            speech_text = block
            extraction_mode = 'no_checkpoint+content_block'
            untranscribed = rubio_turns._count_untranscribed(
                rubio_turns._normalize_chars(block))
            if stats['n_other_turns']:
                # Labels, none of them his: a briefing by someone else, or a label
                # rubio_turns could not resolve (logged above). Keep for review.
                extraction_mode += '+no_rubio_label'
        if block_src == 'per_block':
            extraction_mode += '+per_block'
    else:
        speaker_text = make_speaker_text(raw, url, SECRETARY, half_fallback=False)
        if speaker_text and _is_nav_boilerplate(speaker_text):
            speaker_text = ''
        speech_text, untranscribed = '', 0
        if speaker_text:
            speech_text, stats = rubio_turns.rubio_text(speaker_text, log=True, context=url)
            if stats['n_rubio_turns']:
                extraction_mode = 'no_block+rubio_turns'
                untranscribed = stats['untranscribed_segments']
            else:
                speech_text, mode = extract_secretary_only_text(
                    speaker_text, SECRETARY, boilerplate_veto=False)
                extraction_mode = f'no_block+{mode}'
        if not speech_text.strip():
            speech_text = _dated_body_fallback(raw)
            speaker_text = speaker_text or speech_text
            extraction_mode = 'no_block+dated_body_fallback'
        if not speech_text.strip():
            extraction_mode = 'no_block+needs_review'
            print(f'  [extract] no content block or anchor; speech_text empty: {url}',
                  file=sys.stderr)

    if n_errata:
        extraction_mode += '+footnote_errata'

    # --- Validation ---
    if len(speech_text.strip()) < 100 and len(raw.strip()) < 200:
        print(f'  [skip] Too short — speech_text={len(speech_text)}, raw={len(raw)}: {url}',
              file=sys.stderr)
        return None

    return {
        'speaker_text':    speaker_text,
        'speech_text':     speech_text,
        'extraction_mode': extraction_mode,
        'untranscribed_rubio_segments': untranscribed,
    }


# ---------------------------------------------------------------------------
# Run-to-run state: discovery floor, pending queue, exclusions (plan steps 4/5)
# ---------------------------------------------------------------------------

def _live_key(url: str) -> str:
    """canonical_url_key after the known path renames (spokesperson -> spokesman),
    so both spellings of one page share a key in the pending queue and exclusions."""
    return canonical_url_key(canonicalize_live_url(url))


def _in_corpus(url: str, index: "CorpusIndex") -> bool:
    """Pre-fetch membership test, the same keys run() dedups on."""
    if canonical_url_key(url) in index.url_keys or _live_key(url) in index.url_keys:
        return True
    k = slug_ym_key(url)
    return bool(k and k in index.slug_ym)


def load_discovery_state(path: Path | None) -> dict:
    """data/rubio_discovery_state.json, or {} (loudly) when there is none yet."""
    if path is None:
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except FileNotFoundError:
        print(f'[rubio_backfill] WARNING: no discovery state at {path} -- the floor falls '
              f'back to latest corpus date minus the buffer. Expected only on the first '
              f'run after this file was introduced.', file=sys.stderr)
    except (OSError, ValueError) as e:
        print(f'[rubio_backfill] WARNING: unreadable discovery state {path}: {e} -- '
              f'ignoring it.', file=sys.stderr)
    return {}


def save_discovery_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=1, sort_keys=True)
        f.write('\n')


def resolve_since(args: argparse.Namespace, index: "CorpusIndex",
                  state: dict | None = None) -> date:
    """Work out how far back discovery should reach.

    Order: explicit --since > min(last_complete_floor, latest corpus date for
    this secretary minus --since-buffer-days) > RUBIO_START. --full-rescan
    forces RUBIO_START. RUBIO_START is the hard floor in every case.

    Why a buffer rather than resuming exactly at the latest stored date:
      1. state.gov publishes transcripts late -- a Thursday speech can appear
         the following Tuesday, after Monday's run has already moved past it.
      2. Items are added to the remarks collection late or backdated
         (serbia-statehood-day, dated 2026-02, appeared months later; audit
         D-04). 60 days, not the old 14, because the floor is only a walk
         limit: links whose key is not in the corpus are kept regardless.
      3. One failed weekly run must not leave a permanent hole.
    Why last_complete_floor: the latest Date in the corpus advances even when
    a run only half-walked the archive (2026-10-12 merged 13 rows with 9
    listing pages unreadable). The state file remembers the floor of the last
    run whose walk was COMPLETE, and the floor never moves past it until
    another complete run.
    """
    if getattr(args, 'full_rescan', False):
        return RUBIO_START
    if getattr(args, 'since', None):
        try:
            explicit = date.fromisoformat(args.since)
        except ValueError:
            raise SystemExit(f'ERROR: --since must be YYYY-MM-DD, got {args.since!r}')
        return max(explicit, RUBIO_START)
    floor = RUBIO_START
    if index.max_date is not None:
        buffer_days = int(getattr(args, 'since_buffer_days', _FLOOR_BUFFER_DAYS)
                          or _FLOOR_BUFFER_DAYS)
        floor = index.max_date - timedelta(days=buffer_days)
    lcf = (state or {}).get('last_complete_floor')
    if lcf:
        try:
            floor = min(floor, date.fromisoformat(str(lcf)[:10]))
        except ValueError:
            print(f'[rubio_backfill] WARNING: bad last_complete_floor {lcf!r} in the '
                  f'discovery state -- ignored.', file=sys.stderr)
    return max(floor, RUBIO_START)


def read_pending(path: Path | None) -> dict[str, dict]:
    """data/rubio_pending.csv keyed by _live_key: URLs whose fetch failed."""
    out: dict[str, dict] = {}
    if path is None or not path.exists():
        return out
    with open(path, newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            url = (r.get('url') or '').strip()
            if not url:
                continue
            try:
                attempts = int(r.get('attempts') or 0)
            except ValueError:
                attempts = 0
            out[_live_key(url)] = {'url': url, 'first_seen': r.get('first_seen') or '',
                                   'attempts': attempts, 'last_error': r.get('last_error') or '',
                                   'source': r.get('source') or ''}
    return out


def update_pending(previous: dict[str, dict], *, fetched_ok: set, resolved: set,
                   failures: dict[str, tuple[str, str]], today: str,
                   sources: dict[str, str] | None = None) -> dict[str, dict]:
    """Carry the pending queue forward one run.

    fetched_ok -- keys fetched successfully this run (removed, whatever the
                  page was then classified as: that decision is in the
                  skipped sidecar)
    resolved   -- keys found in the corpus or excluded by decision (removed)
    failures   -- key -> (url, error) for fetches that failed this run
    A previous entry not attempted this run is kept unchanged.
    """
    out: dict[str, dict] = {}
    for key, row in previous.items():
        if key in fetched_ok or key in resolved:
            continue
        row = dict(row)
        if key in failures:
            row['attempts'] = int(row.get('attempts') or 0) + 1
            row['last_error'] = failures[key][1][:300]
        out[key] = row
    for key, (url, err) in failures.items():
        if key not in out:
            out[key] = {'url': url, 'first_seen': today, 'attempts': 1, 'last_error': err[:300],
                        'source': (sources or {}).get(key, '')}
    return out


def write_pending(path: Path, pending: dict[str, dict]) -> None:
    _write_rows(path, _PENDING_COLUMNS,
                sorted(pending.values(), key=lambda r: (r.get('first_seen', ''), r['url'])))


def stuck_entries(pending: dict[str, dict], threshold: int = _STUCK_ATTEMPTS) -> list[dict]:
    """Pending URLs that have failed in `threshold` or more separate runs."""
    return [r for r in pending.values() if int(r.get('attempts') or 0) >= threshold]


class Exclusions(NamedTuple):
    url_keys: dict      # _live_key -> reason
    slug_dates: dict    # slug_date_key -> reason


def load_exclusions(path: Path | None) -> Exclusions:
    """data/rubio_coverage_exclusions.csv (url_key, slug_date_key, reason, decided_by, date).

    Pages a person decided are out of scope (readouts, the IWOC preview, the
    archive index page, Spokesperson media notes). They are skipped before the
    fetch, so known non-items stop costing a request every run. slug_date_key is
    written human-readably as "slug|YYYY-MM-DD".
    """
    keys: dict[str, str] = {}
    sdk: dict[str, str] = {}
    if path is None or not path.exists():
        return Exclusions(keys, sdk)
    with open(path, newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            reason = (r.get('reason') or 'excluded').strip()
            uk = (r.get('url_key') or '').strip()
            if uk:
                keys[_live_key(uk if '://' in uk else 'https://' + uk)] = reason
            sd = (r.get('slug_date_key') or '').strip()
            if '|' in sd:
                slug, day = sd.split('|', 1)
                k = slug_date_key(f'https://www.state.gov/{slug.strip()}/', day.strip())
                if k:
                    sdk[k] = reason
    return Exclusions(keys, sdk)


def read_url_list(path: Path) -> list[str]:
    """Supplemental URL file: one URL per line ('#' comments, blank lines and
    trailing '# note' ignored), or a CSV with a 'url'/'URL' column (rubio_held.csv)."""
    if not path.exists():
        print(f'[rubio_backfill] WARNING: URL list {path} not found -- skipped.',
              file=sys.stderr)
        return []
    urls: list[str] = []
    if path.suffix.lower() == '.csv':
        with open(path, newline='', encoding='utf-8') as f:
            for r in csv.DictReader(f):
                u = (r.get('url') or r.get('URL') or '').strip()
                if u:
                    urls.append(u)
        return urls
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            tok = line.split()[0]
            if tok.startswith('http'):
                urls.append(tok)
    return urls


def _write_rows(path: Path, columns: list[str], rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def _run_id() -> str:
    rid = os.environ.get('GITHUB_RUN_ID', '').strip()
    if rid:
        return rid
    return datetime.now(timezone.utc).strftime('local-%Y%m%dT%H%M%SZ')


def _gh_output(**kv) -> None:
    gh_out = os.environ.get('GITHUB_OUTPUT')
    if not gh_out:
        return
    with open(gh_out, 'a', encoding='utf-8') as fh:
        for k, v in kv.items():
            fh.write(f'{k}={v}\n')


def _sitemap_priority(c: "Candidate") -> tuple:
    """Fetch order under --sitemap-fetch-cap: 'rubio' slugs, then other non-readout
    slugs, then readout/travel slugs; newest URL month first within each."""
    low = c.url.lower()
    readout = any(p in low for p in _NON_SPEAKER_URL_PATS) or 'readout' in low
    ym = url_year_month(c.url)
    if ym is None and c.lastmod:          # undated flat page: its edit month
        ym = (int(c.lastmod[:4]), int(c.lastmod[5:7]))
    return (readout, 'rubio' not in low, -(ym[0] * 100 + ym[1]) if ym else 0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _sidecar_paths(args: argparse.Namespace, out_dir: Path) -> dict[str, Path | None]:
    def pick(attr: str, name: str) -> Path:
        v = getattr(args, attr, None)
        return Path(v) if v else out_dir / name
    excl = getattr(args, 'exclusions', None)
    return {
        'skipped':  pick('skipped_out', _SKIPPED_NAME),
        'snapshot': pick('listing_snapshot', _SNAPSHOT_NAME),
        'pending':  pick('pending', _PENDING_NAME),
        'state':    pick('discovery_state', _STATE_NAME),
        'stuck':    pick('stuck_out', _STUCK_NAME),
        'summary':  pick('summary_out', _SUMMARY_NAME),
        'exclusions': Path(excl) if excl else _EXCLUSIONS_DEFAULT,
    }


def run(args: argparse.Namespace) -> None:
    enriched_path = Path(args.enriched)
    output_path = Path(args.output)
    side = _sidecar_paths(args, output_path.parent)
    url_files = [Path(p) for p in (getattr(args, 'urls', None) or [])]
    # A file named explicitly on the command line must exist. A silently empty
    # supplemental list or exclusions file lets the job go green while the known
    # off-collection items are never fetched (the 2026-10-12 silent-success lesson;
    # both seed files were once gitignored and so absent on the runner).
    missing = [p for p in url_files if not p.exists()]
    if getattr(args, 'exclusions', None) and not side['exclusions'].exists():
        missing.append(side['exclusions'])
    if missing:
        raise SystemExit('[rubio_backfill] ERROR: input file(s) given on the command line '
                         'not found: ' + ', '.join(str(p) for p in missing))
    rate_limit = float(args.rate_limit)
    dry_run = args.dry_run
    supplemental_only = bool(getattr(args, 'supplemental_only', False))
    run_id = _run_id()
    today = datetime.now(timezone.utc).date().isoformat()
    skiplog = SkipLog(run_id)
    snapshot: list[dict] = []
    disc: dict = {}

    def write_discovery_sidecars() -> None:
        skiplog.write(side['skipped'])
        _write_rows(side['snapshot'], _SNAPSHOT_COLUMNS, snapshot)
        print(f'[rubio_backfill] sidecars: {side["skipped"]} ({len(skiplog.rows)} rows), '
              f'{side["snapshot"]} ({len(snapshot)} listing slots)')

    # Step 1: Load existing corpus + run-to-run state
    print(f'[rubio_backfill] Loading existing corpus: {enriched_path.name}')
    index = load_existing_urls(enriched_path)
    max_row_id = index.max_row_id
    state = load_discovery_state(side['state'])
    since = resolve_since(args, index, state)
    exclusions = load_exclusions(side['exclusions'])
    pending = read_pending(side['pending'])
    print(f'[rubio_backfill] {len(index.url_keys):,} existing URL keys, '
          f'{len(index.slug_ym):,} slug+month keys, max row_id={max_row_id}, '
          f'latest Rubio date={index.max_date}')
    print(f'[rubio_backfill] run {run_id}: {len(pending)} pending URL(s) from '
          f'{side["pending"]}, {len(exclusions.url_keys)} excluded URL key(s), '
          f'last_complete_floor={state.get("last_complete_floor")}')

    # Step 2: Discover candidates
    session = requests.Session()
    mode = ('supplemental only' if supplemental_only else
            'full re-scan' if getattr(args, 'full_rescan', False) else 'incremental')
    print(f'[rubio_backfill] Discovery floor: {since} ({mode})')
    extra: list[Candidate] = []
    for p in url_files:
        src = 'held' if 'held' in p.name else 'supplemental'
        got = read_url_list(p)
        extra += [Candidate(u, src) for u in got]
        print(f'[rubio_backfill] {len(got)} supplemental URL(s) from {p.name}')
    # Pending URLs are re-fed EVERY run, whatever --since says: a page that
    # timed out must not age out of the window before it is ever read (F-01).
    extra += [Candidate(r['url'], 'pending', origin=r.get('source') or '')
              for r in pending.values()]
    try:
        if supplemental_only:
            all_candidates = _dedup_candidates(extra)
        else:
            all_candidates = collect_candidate_urls(
                session, rate_limit, None, since,
                full_rescan=getattr(args, 'full_rescan', False),
                sitemap_budget_min=getattr(args, 'sitemap_budget_min', _SITEMAP_BUDGET_MIN),
                in_corpus=lambda u: _in_corpus(u, index),
                snapshot=snapshot, skiplog=skiplog, stats=disc, run_id=run_id,
                extra=extra)
    except DiscoveryError as e:
        # Exit non-zero so the workflow step FAILS and GitHub notifies, rather
        # than the job going green with "No new speeches found". The pending
        # queue and the discovery state are NOT advanced, so the next
        # successful run re-covers this window: a transient outage costs a
        # delay, not data. The sidecars are still written for diagnosis.
        write_discovery_sidecars()
        _gh_output(discovery_complete='false')
        sys.exit(
            f'ERROR: discovery failed — {e}\n'
            f'       Refusing to report "no new speeches" when the crawl did not '
            f'complete. state.gov returns 504/timeouts intermittently; re-run the '
            f'workflow, and if it persists check whether the remarks archive URL or '
            f'its markup has changed.'
        )
    print(f'[rubio_backfill] {len(all_candidates):,} candidate URLs discovered (pre-dedup)')

    # Deduplicate against the existing corpus.
    #
    # Two keys, because a URL key alone is not rename-proof: when state.gov
    # renamed /office-of-the-spokesperson/ to /office-of-the-spokesman/ in
    # September 2026, every one of 565 already-collected Rubio speeches looked
    # brand new. The slug and the month survive that rename, so (slug, YYYY-MM)
    # catches it here -- BEFORE spending a fetch on each one.
    #
    # Month granularity, not day: the URL only carries /YYYY/MM/, so a
    # day-precise test here would drop same-month candidates we still want.
    # Full (slug, Date) precision is applied after fetching, once the real
    # speech date is known.
    classified = state.setdefault('sitemap_classified', {})
    new_cands: list[Candidate] = []
    resolved: set[str] = set()
    skipped_url_key = skipped_slug_ym = known_sitemap = 0
    for c in all_candidates:
        u, key = c.url, _live_key(c.url)
        if canonical_url_key(u) in index.url_keys or key in index.url_keys:
            skipped_url_key += 1
            resolved.add(key)
            continue
        k = slug_ym_key(u)
        if k and k in index.slug_ym:
            skipped_slug_ym += 1
            resolved.add(key)
            continue
        if key in exclusions.url_keys:
            skiplog.add(u, 'excluded_by_decision', 'prefetch',
                        text=exclusions.url_keys[key])
            resolved.add(key)
            continue
        if c.source in _CAPPED_SOURCES and key in classified:
            known_sitemap += 1      # fetched and classified out in an earlier run
            continue
        new_cands.append(c)
    print(f'[rubio_backfill] {skipped_url_key + skipped_slug_ym:,} already in corpus '
          f'(url key: {skipped_url_key:,}, slug+month: {skipped_slug_ym:,})')
    if skipped_slug_ym:
        print(f'[rubio_backfill] NOTE: {skipped_slug_ym:,} candidate(s) matched only on '
              f'slug+month, not on URL -- state.gov has likely renamed a path segment. '
              f'If this count is large, add the rename to PATH_RENAMES in corpus_keys.py.')
    if known_sitemap:
        print(f'[rubio_backfill] {known_sitemap:,} sitemap candidate(s) already classified '
              f'out in an earlier run (discovery state) -- not re-fetched')

    # Sitemap-only candidates are capped (D-01): one month of press releases is
    # ~1.2k URLs, nearly all by other people. The cap rotates through the
    # backlog because anything fetched and classified out is remembered above.
    cap = int(getattr(args, 'sitemap_fetch_cap', _SITEMAP_FETCH_CAP))
    sm = sorted((c for c in new_cands if c.source in _CAPPED_SOURCES), key=_sitemap_priority)
    if cap >= 0 and len(sm) > cap:
        dropped = sm[cap:]
        keep_keys = {_live_key(c.url) for c in sm[:cap]}
        new_cands = [c for c in new_cands
                     if c.source not in _CAPPED_SOURCES or _live_key(c.url) in keep_keys]
        for c in dropped:
            skiplog.add(c.url, 'sitemap_fetch_cap', 'prefetch')
        print(f'[rubio_backfill] WARNING: --sitemap-fetch-cap {cap}: {len(dropped):,} sitemap '
              f'candidate(s) not fetched this run (listed in {side["skipped"].name}, reason '
              f'sitemap_fetch_cap), e.g. {[c.url for c in dropped[:5]]}', file=sys.stderr)
    print(f'[rubio_backfill] {len(new_cands):,} new URLs to fetch '
          f'({dict(Counter(c.source for c in new_cands))})')

    if dry_run:
        print('[rubio_backfill] --dry-run: stopping before fetch '
              '(pending queue and discovery state left unchanged).')
        for c in new_cands[:20]:
            print(f'  [{c.source}] {c.url}')
        if len(new_cands) > 20:
            print(f'  ... and {len(new_cands) - 20} more')
        write_discovery_sidecars()
        return

    # Step 3–4: Fetch, extract, build rows
    rows_out: list[dict] = []
    failed = 0
    fetched_ok: set[str] = set()
    failures: dict[str, tuple[str, str]] = {}
    failure_sources: dict[str, str] = {}   # key -> first-finding source, for pending
    next_row_id = max_row_id + 1

    def skip(c: Candidate, reason: str, stage: str, *, block_meta: dict | None = None,
             page: dict | None = None, text: str = '', labels: str = '') -> None:
        print(f'  [skip] {reason}: {c.url}', file=sys.stderr)
        bm = block_meta or {}
        skiplog.add(c.url, reason, stage, block_type=bm.get('block_speech_type', ''),
                    byline=bm.get('block_byline', ''),
                    title=(page or {}).get('title', ''), text=text, labels=labels)
        if c.source in _CAPPED_SOURCES and stage in ('classify', 'extract', 'post_fetch'):
            classified[_live_key(c.url)] = {'reason': reason, 'date': today}

    for i, c in enumerate(new_cands, 1):
        url = c.url
        time.sleep(rate_limit)
        print(f'  [{i}/{len(new_cands)}] [{c.source}] {url}')

        try:
            page = fetch_rubio_page(url, session, raise_errors=True)
        except FetchFailure as f:
            failed += 1
            failure_sources[_live_key(url)] = c.origin or c.source
            failures[_live_key(url)] = (url, f'{f.kind}: {f.detail}'
                                        + (f' (final {f.final_url})' if f.final_url else ''))
            skiplog.add(url, f.kind, 'fetch',
                        text=f.detail + (f' | final URL {f.final_url}' if f.final_url else ''))
            continue
        fetched_ok.add(_live_key(url))

        # Metadata from page block
        block_meta = _parse_block_metadata(page['raw_text'])

        # Date
        resolved_date = _parse_date(
            page['jsonld_date_published'],
            url,
            block_meta.get('block_date', ''),
        )

        # speech_type + location
        stype = _resolve_speech_type(page, block_meta, url)
        location = _resolve_location(page, block_meta)

        # Date validation: skip rows clearly outside Rubio's term
        # (small buffer to 2025-01-15 allows confirmation hearing coverage)
        if resolved_date:
            try:
                row_date = date.fromisoformat(resolved_date)
                if row_date < date(2025, 1, 15):
                    skip(c, 'pre_tenure_date', 'post_fetch', block_meta=block_meta, page=page,
                         text=f'Date {resolved_date}')
                    continue
            except ValueError:
                pass

        # Content extraction
        extracted = extract_row(url, page)
        if extracted is None:
            skip(c, 'too_short', 'extract', block_meta=block_meta, page=page,
                 text=page.get('raw_text', ''))
            continue

        # Classification (plan step P2): active-speaker gate with transcript
        # markers, then the byline gate.
        origin = c.origin if c.source == 'pending' else c.source
        decision = classify_rubio_page(normalize_url(url), page, block_meta, extracted,
                                       trusted=(origin in _TRUSTED_SOURCES
                                                or (c.source == 'pending' and not origin)))
        if not decision.keep:
            skip(c, decision.reason, 'classify', block_meta=block_meta, page=page,
                 text=extracted.get('speaker_text') or extracted['speech_text'],
                 labels=' | '.join(decision.labels))
            continue

        # Written statements and recorded video messages issued by the
        # Department carry no dateline; the corpus convention (565 of 566 Rubio
        # Press Statements in v2026.10.19) is the Washington newsroom. Spoken items with no dateline get no
        # default and fall through to corpus_rubio_update's provisional_location.
        if not location and decision.modality in ('written', 'joint', 'video'):
            location = 'Washington, D.C.'

        # Post-fetch dedup: now that the real speech date is known, apply the
        # full-strength (slug, Date) key. This catches a rename that ALSO
        # changed the URL's /YYYY/MM/ segment, which the pre-fetch slug+month
        # filter cannot see. Verified collision-free across all 17,617 corpus
        # rows, so a hit here is a genuine duplicate.
        sd_key = slug_date_key(url, resolved_date or '')
        if sd_key and sd_key in index.slug_date:
            skip(c, 'dedup_slug_date', 'post_fetch', block_meta=block_meta, page=page)
            continue
        if sd_key and sd_key in exclusions.slug_dates:
            skip(c, 'excluded_by_decision', 'post_fetch', block_meta=block_meta, page=page,
                 text=exclusions.slug_dates[sd_key])
            continue

        title = page_headline(page)

        row = {
            'speech_type':  stype,
            'Location':     location,
            'Date':         resolved_date,
            'secretary':    SECRETARY,
            'Title':        title,
            'speech_text':  decision.speech_text or extracted['speech_text'],
            'row_id':       str(next_row_id),
            'url':          normalize_url(url),
            'raw_text':     page['raw_text'],
            'speaker_text': extracted['speaker_text'],
            'extraction_mode': extracted['extraction_mode'] + (
                '+after_transcript_marker' if decision.speech_text else ''),
            'untranscribed_rubio_segments': extracted['untranscribed_rubio_segments'],
            'modality':     decision.modality,
            'byline':       block_meta.get('block_byline', ''),
            'listing_date': c.listing_date,
            'discovery_source': c.source,
            **_PIPELINE_META,
        }
        rows_out.append(row)
        next_row_id += 1

        if i % 10 == 0:
            print(f'  ... {i}/{len(new_cands)} fetched, {len(rows_out)} written so far')

    # Step 5: Write output (also when empty: the workflow counts its rows)
    print(f'\n[rubio_backfill] Writing {len(rows_out):,} rows to {output_path.name}...')
    _write_rows(output_path, _FIELDNAMES, rows_out)

    # Run-to-run state.
    new_pending = update_pending(pending, fetched_ok=fetched_ok, resolved=resolved,
                                 failures=failures, today=today, sources=failure_sources)
    write_pending(side['pending'], new_pending)
    stuck = stuck_entries(new_pending)
    if stuck:
        _write_rows(side['stuck'], _PENDING_COLUMNS, stuck)
        print(f'[rubio_backfill] WARNING: {len(stuck)} URL(s) have failed in '
              f'>= {_STUCK_ATTEMPTS} runs -- {side["stuck"]}', file=sys.stderr)
        for r in stuck[:10]:
            print(f'    [{r["attempts"]}x] {r["url"]} -- {r["last_error"][:120]}',
                  file=sys.stderr)
    elif side['stuck'].exists():
        side['stuck'].unlink()

    complete = bool(disc.get('complete')) and not supplemental_only
    state['last_run'] = {'run_id': run_id, 'date': today, 'since': since.isoformat(),
                         'mode': mode, 'complete': complete,
                         'incomplete_reasons': disc.get('incomplete_reasons', [])}
    if complete:
        buffer_days = int(getattr(args, 'since_buffer_days', _FLOOR_BUFFER_DAYS)
                          or _FLOOR_BUFFER_DAYS)
        lcf = (max(index.max_date - timedelta(days=buffer_days), RUBIO_START)
               if index.max_date else since)
        state['last_complete_floor'] = lcf.isoformat()
        state['last_complete_run'] = run_id
    elif not supplemental_only:
        print(f'[rubio_backfill] WARNING: archive walk not proven complete '
              f'({disc.get("incomplete_reasons")}) -- last_complete_floor stays at '
              f'{state.get("last_complete_floor")}, so the next run re-walks this window.',
              file=sys.stderr)
    # Forget sitemap classifications once they are far below any floor.
    cutoff = (since - timedelta(days=120)).isoformat()
    state['sitemap_classified'] = {k: v for k, v in classified.items()
                                   if (v.get('date') or '') >= cutoff}
    save_discovery_state(side['state'], state)
    write_discovery_sidecars()

    by_reason = skiplog.counts()
    summary = {
        'run_id': run_id, 'since': since.isoformat(), 'mode': mode,
        'listed': disc.get('listed', 0), 'in_window': disc.get('in_window', 0),
        'unique': disc.get('unique', 0), 'dup_slots': disc.get('dup_slots', 0),
        'dup_slots_after_rewalk': disc.get('dup_slots_after', 0),
        'failed_pages': disc.get('failed_pages', []),
        'sitemaps_failed': len(disc.get('sitemaps_failed', [])),
        'discovery_complete': complete,
        'candidates': len(all_candidates), 'to_fetch': len(new_cands),
        'fetched': len(fetched_ok), 'failed': failed, 'written': len(rows_out),
        'skipped_by_reason': dict(by_reason),
        'pending': len(new_pending), 'stuck': len(stuck),
    }
    with open(side['summary'], 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=1)
        f.write('\n')
    _gh_output(rows_written=len(rows_out), fetched=len(fetched_ok), fetch_failed=failed,
               skipped=sum(by_reason.values()), pending=len(new_pending), stuck=len(stuck),
               discovery_complete=str(complete).lower())

    print(f'[rubio_backfill] Done.')
    print(f'  Rows written:  {len(rows_out):,}')
    print(f'  Fetched:       {len(fetched_ok):,}')
    print(f'  Failed:        {failed:,}  (fetch error; kept in {side["pending"].name})')
    print(f'  Skipped:       {sum(by_reason.values()):,}  by reason: {dict(by_reason)}')
    print(f'[rubio_backfill] summary: listed={summary["listed"]} '
          f'in_window={summary["in_window"]} unique={summary["unique"]} '
          f'dup_slots={summary["dup_slots"]} failed_pages={summary["failed_pages"]} '
          f'fetched={summary["fetched"]} failed={failed} written={len(rows_out)} '
          f'pending={len(new_pending)} stuck={len(stuck)} complete={complete} '
          f'skipped_by_reason={dict(by_reason)}')

    # Quick quality check
    empty_speech = sum(1 for r in rows_out if not r['speech_text'].strip())
    empty_raw = sum(1 for r in rows_out if not r['raw_text'].strip())
    empty_spkr = sum(1 for r in rows_out if not r['speaker_text'].strip())
    dates = [r['Date'] for r in rows_out if r['Date']]
    print(f'\nQuality check:')
    print(f'  Empty speech_text:  {empty_speech}  (target: 0)')
    print(f'  Empty raw_text:     {empty_raw}  (target: 0)')
    print(f'  Empty speaker_text: {empty_spkr}  (target: ~0)')
    print(f'  Modality: {dict(Counter(r["modality"] for r in rows_out))}')
    if dates:
        print(f'  Date range: {min(dates)} – {max(dates)}')


def main() -> None:
    default_output = BASE_DIR / f'rubio_backfill_{date.today().isoformat()}.csv'

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--enriched', default=str(_ENRICHED_DEFAULT),
                        help='Existing enriched CSV (default: %(default)s)')
    parser.add_argument('--output', default=str(default_output),
                        help='Output CSV path (default: %(default)s). The sidecars below '
                             'default to this file\'s directory.')
    parser.add_argument('--urls', action='append', default=None, metavar='FILE',
                        help='Supplemental URLs: a text file (one per line, # comments) or a '
                             'CSV with a url column, e.g. rubio_held.csv. Repeatable. Fetched '
                             'whatever --since says, unless already in the corpus.')
    parser.add_argument('--supplemental-only', action='store_true',
                        help='Skip sitemap and archive discovery; fetch only --urls and the '
                             'pending queue. The discovery floor state is not advanced.')
    parser.add_argument('--rate-limit', default=1.0, type=float,
                        help='Seconds between HTTP requests (default: %(default)s)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Discover URLs only; do not fetch or write output (pending queue '
                             'and discovery state are left unchanged)')
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--since', default=None, metavar='YYYY-MM-DD',
                       help='Only discover pages dated on or after this. Default: '
                            'min(last_complete_floor from --discovery-state, the latest '
                            'date already in --enriched for this secretary minus '
                            '--since-buffer-days).')
    scope.add_argument('--full-rescan', action='store_true',
                       help='Ignore the incremental cutoff and walk the whole archive '
                            'back to the start of the term (only keys not in the corpus '
                            'are fetched). Manual dispatch only in CI (full_rescan input).')
    parser.add_argument('--since-buffer-days', type=int, default=_FLOOR_BUFFER_DAYS, metavar='N',
                        help='Days of slack below the latest stored date (default: '
                             '%(default)s), so a late-listed or backdated item, or a '
                             'missed weekly run, does not leave a permanent hole.')
    parser.add_argument('--sitemap-budget-min', type=float, default=_SITEMAP_BUDGET_MIN, metavar='MIN',
                        help='Incremental runs only: stop scanning sitemaps after this many '
                             'minutes (default: %(default)s; 0 = no limit).')
    parser.add_argument('--sitemap-fetch-cap', type=int, default=_SITEMAP_FETCH_CAP, metavar='N',
                        help='Fetch at most N candidates found only in the release sitemaps '
                             '(default: %(default)s; the rest are logged as sitemap_fetch_cap '
                             'in the skipped sidecar and retried next run).')
    parser.add_argument('--pending', default=None, metavar='CSV',
                        help=f'Pending-fetch queue (url, first_seen, attempts, last_error); '
                             f'read, then rewritten. Default: <output dir>/{_PENDING_NAME}')
    parser.add_argument('--discovery-state', default=None, metavar='JSON',
                        help=f'Floor/classification state; read, then rewritten. '
                             f'Default: <output dir>/{_STATE_NAME}')
    parser.add_argument('--exclusions', default=None, metavar='CSV',
                        help=f'Decided out-of-scope pages, skipped before fetch. '
                             f'Default: {_EXCLUSIONS_DEFAULT}')
    parser.add_argument('--skipped-out', default=None, metavar='CSV',
                        help=f'Every rejection, one row each. Default: <output dir>/{_SKIPPED_NAME}')
    parser.add_argument('--listing-snapshot', default=None, metavar='CSV',
                        help=f'Every remarks-archive slot walked. '
                             f'Default: <output dir>/{_SNAPSHOT_NAME}')
    parser.add_argument('--stuck-out', default=None, metavar='CSV',
                        help=f'Pending URLs failed in >= {_STUCK_ATTEMPTS} runs (written only '
                             f'when non-empty). Default: <output dir>/{_STUCK_NAME}')
    parser.add_argument('--summary-out', default=None, metavar='JSON',
                        help=f'Run counters. Default: <output dir>/{_SUMMARY_NAME}')
    parser.add_argument('--print-since', action='store_true',
                        help='Print the resolved discovery floor and exit. Used by CI to '
                             'compute the cutoff without duplicating the logic in YAML.')
    args = parser.parse_args()

    if args.print_since:
        index = load_existing_urls(Path(args.enriched))
        side = _sidecar_paths(args, Path(args.output).parent)
        since = resolve_since(args, index, load_discovery_state(side['state']))
        print(f'since={since.isoformat()}')
        _gh_output(since=since.isoformat())
        return

    run(args)


if __name__ == '__main__':
    main()
