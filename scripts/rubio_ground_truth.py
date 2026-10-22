#!/usr/bin/env python3
"""rubio_ground_truth.py -- READ-ONLY ground-truth crawl of state.gov for Rubio items.

Plan step 7 (docs/rubio_coverage_audit.md §3). Builds an inventory of every
state.gov item in which Marco Rubio speaks or writes in his own voice as
Secretary of State, independently of the weekly scraper's single discovery
source (the /remarks-secretary-rubio/ collection -- failure modes D-01, D-02,
D-08), and measures archive pagination drift (D-03).

It never writes to the corpus, never commits and never publishes. Its outputs
are CSV/JSON files under --out-dir, which the coverage reconciler (step 8) and
verifier check AC consume.

Stages (run in this order; pick a subset with --stages):
  E1  sitemap_index.xml inventory: every child <loc>, URL count, lastmod range
  E2  every URL in state_press_release-sitemap*, state_briefing-sitemap*,
      state_ext_content-sitemap* and page-sitemap* whose URL /YYYY/MM/ is
      >= 2025-01, or (undated URL) whose lastmod >= 2025-01-15. NO 'rubio'
      filter -- 'rubio' in the slug misses ~70% of real releases (D-02).
  E3  two full walks of /remarks-secretary-rubio/, the second with a
      ?gt=<run id> cache-buster, recording per-page cache headers,
      input[name=total_pages] and the "N items" header (D-03).
  E4  date-ordered walks of /releases/office-of-the-spokesman/ and
      /releases/secretary-of-state/ down to 2025-01-21 (D-01, D-08).
  E6  content probe of the diff set ONLY: (E2 ∪ E3 ∪ E4) minus corpus keys
      minus --classified URLs. Assigns gt_class and in_scope.

Load on state.gov is kept low on purpose: one session, no parallelism, at
least --min-interval seconds (+ a deterministic per-URL jitter) between
requests, exponential backoff honouring Retry-After on 429/502/503/504, a
hard --max-requests cap, and a resumable state file (--state-in/--state-out)
so a full inventory can be spread over several nights.

Corpus rows are classified OFFLINE from their stored Raw Text with the same
classifier the probe uses, so ground_truth.csv covers both what the corpus
already has and what it is missing, under one definition.

Usage:
    python scripts/rubio_ground_truth.py --corpus data/corpus.csv \\
        --stages E1,E2,E3,E4,E6 --max-requests 1500 --out-dir rubio_gt_2026-10-25
    python scripts/rubio_ground_truth.py --offline-fixtures tests/fixtures/gt_site \\
        --corpus tests/fixtures/gt_site/corpus_small.csv --out-dir /tmp/gt_dry
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).parent))

from corpus_keys import (
    canonical_url_key,
    canonicalize_live_url,
    slug_date_key,
    slug_ym_key,
    url_slug,
    url_year_month,
)
# Read-only reuse of the production parsers so ground truth and the weekly
# scraper read pages identically. Private helpers are imported, never edited.
from rubio_backfill import (
    _HEADERS,
    _flatten_jsonld,
    _make_soup,
    _parse_date,
    load_existing_urls,
)
from speech_recover import (
    _DATE_LINE_RE,
    _HIDE_ANCHOR_RE,
    _TAGS_SENTINEL,
    _extract_modern_state_content,
)

try:
    import requests
except ImportError:
    print('[rubio_ground_truth] ERROR: requests is required.', file=sys.stderr)
    sys.exit(1)

# The step-2c label segmenter, when it has landed. Optional: the crawl must
# run on a checkout that predates it, so fall back to a local regex.
try:
    import rubio_turns  # type: ignore
except Exception:  # noqa: BLE001 -- any import failure means "not available"
    rubio_turns = None

csv.field_size_limit(10 ** 9)

LOG = '[rubio_ground_truth]'

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SITEMAP_INDEX = 'https://www.state.gov/sitemap_index.xml'
REMARKS_INDEX = 'https://www.state.gov/remarks-secretary-rubio/'
E4_LISTINGS = (
    ('spokesman', 'https://www.state.gov/releases/office-of-the-spokesman/'),
    ('secretary', 'https://www.state.gov/releases/secretary-of-state/'),
)

# Sitemap families E2 reads (the part of the child name before -sitemapN.xml).
E2_FAMILIES = ('state_press_release', 'state_briefing', 'state_ext_content', 'page')

ALL_STAGES = ('E1', 'E2', 'E3', 'E4', 'E6', 'T1')

# source_set bitmask. 8/16 are not crawl stages but are recorded so a row's
# provenance is visible in one column.
SRC_E2 = 1           # release / page sitemaps
SRC_E3 = 2           # /remarks-secretary-rubio/ collection
SRC_E4 = 4           # spokesman / secretary-of-state release listings
SRC_CLASSIFIED = 8   # --classified CSV (hand-reviewed earlier)
SRC_CORPUS = 16      # an existing corpus Rubio row

DEFAULT_URL_MONTH_FLOOR = (2025, 1)
DEFAULT_LASTMOD_FLOOR = date(2025, 1, 15)
# Rubio was sworn in 2025-01-21. Listings stop there, and scope uses it as
# the event-date floor (audit §1 D3: row 17464, the 2025-01-15 confirmation
# opening, is pre-tenure).
DEFAULT_SCOPE_FLOOR = date(2025, 1, 21)

# Politeness. Retries count against --max-requests like any other request.
_TIMEOUT = (10, 45)
_RETRY_STATUSES = (429, 502, 503, 504)
_MAX_ATTEMPTS = 4
_BACKOFF_BASE = 5.0
_BACKOFF_CAP = 300.0
# A page still failing after this many separate runs is given up on (E6).
MAX_PROBE_RUNS = 3

GT_CLASSES = (
    'rubio_spoken', 'rubio_written_byline', 'rubio_video_transcript', 'rubio_joint',
    'readout', 'other_principal', 'spokesperson_third_person',
    'external_redirect', 'fetch_error',
)
IN_SCOPE_CLASSES = {
    'rubio_spoken': 'spoken',
    'rubio_written_byline': 'written',
    'rubio_video_transcript': 'video',
    'rubio_joint': 'joint',
}

# Block type lines (2nd line after the hide anchor) that are spoken events.
_SPOKEN_TYPES = (
    'remarks', 'remarks to the press', 'interview', 'press availability',
    'joint press availability', 'speech', 'testimony', 'town hall',
    'press briefing', 'department press briefing', 'briefing', 'press gaggle',
    'gaggle', 'on-the-record briefing', 'press conference',
)

# A byline line is "Name, Title"; anything else in the meta block is a
# dateline (venue / city). These words mark the part after the comma as a title.
_TITLE_WORDS_RE = re.compile(
    r'\b(?:Secretary|President|Minister|Spokesperson|Spokesman|Ambassador|'
    r'Administrator|Director|Adviser|Advisor|Representative|Envoy|Counselor|'
    r'Chief|Governor|Senator|Chair(?:man|woman)?|Coordinator|Commissioner|'
    r'Attorney General|Leader|Deputy|Under Secretary|Head|Chancellor|King|'
    r'Prime Minister|Foreign|Designate)\b'
)
_SPOKES_OFFICE_RE = re.compile(r'^Office of the Spokes(?:person|man)$', re.IGNORECASE)

# Own-line Rubio speaker labels, case-SENSITIVE and Unicode-tolerant
# (NBSP, a colon pushed onto the next line). Deliberately excludes
# "Secretary Rubio," (vocative, X-03), MRS RUBIO and SENATOR RUBIO (D9).
_WS = r'[ \t\u00a0\u2007\u202f]'
_RUBIO_LABEL_RE = re.compile(
    rf'(?m)^{_WS}*(?:THE{_WS}+)?SECRETARY(?:[-{_WS[1:-1]}]+DESIGNATE)?'
    rf'(?:{_WS}+OF{_WS}+STATE)?(?:{_WS}+[A-Z][A-Z.\']*)*?{_WS}+RUBIO{_WS}*\n?{_WS}*:'
)
# Fuzzy candidates: short uppercase label lines mentioning RUB / SECR that the
# strict pattern did not take (typos like "SECRETARY RUBI:", split labels).
_LABEL_LINE_RE = re.compile(r'(?m)^[ \t\u00a0]*([A-Z][A-Z .\'\-\u00c0-\u017f]{2,48})[ \t\u00a0]*:')

# Third-person openings: the page is ABOUT Rubio. Anchored at the start of the
# content block (after whitespace/quote normalization).
_THIRD_PERSON_RE = re.compile(
    r'^(?:(?:Today|Yesterday|This week|Earlier today|On [A-Z][a-z]+ \d{1,2})'
    r'(?:,\s*\d{4})?,?\s+)?(?:U\.S\.\s+)?Secretary(?:\s+of\s+State)?(?:\s+Marco)?\s+Rubio\b'
)
_READOUT_RE = re.compile(r'The below is attributable to', re.IGNORECASE)
# Transcript markers that turn a third-person intro into "his words follow".
_MARKERS = {
    'has_translated_transcript': re.compile(r'translated transcript', re.IGNORECASE),
    'has_courtesy_translation': re.compile(r'courtesy translation', re.IGNORECASE),
    'has_video_message_below': re.compile(r'video (?:message|remarks|address) (?:is|are) below', re.IGNORECASE),
    'has_as_prepared': re.compile(r'as prepared(?: for delivery)?', re.IGNORECASE),
    'has_as_delivered': re.compile(r'as delivered', re.IGNORECASE),
    'has_transcript': re.compile(r'\btranscript\b', re.IGNORECASE),
}
_VIDEO_RE = re.compile(r'\bvideo (?:message|remarks|address|statement)\b', re.IGNORECASE)

_ITEMS_HEADER_RE = re.compile(r'\b(\d[\d,]*)\s+(?:items?|results?)\b', re.IGNORECASE)
_LIST_DATE_RE = re.compile(
    r'\b(?:January|February|March|April|May|June|July|August|September|'
    r'October|November|December)\s+\d{1,2},\s+\d{4}\b'
)
_SITEMAP_FAMILY_RE = re.compile(r'/([A-Za-z0-9_\-]+?)-sitemap\d*\.xml', re.IGNORECASE)

# ---------------------------------------------------------------------------
# Output schemas
# ---------------------------------------------------------------------------

INVENTORY_COLS = ['child_loc', 'family', 'index_lastmod', 'n_urls', 'lastmod_min',
                  'lastmod_max', 'n_in_window', 'sample_paths', 'status', 'error']
LISTING_COLS = ['stage', 'listing', 'pass', 'page', 'pos', 'href', 'url_key',
                'slug_date_key', 'type', 'title', 'list_date', 'total_pages',
                'items_header', 'x_cache', 'age', 'cf_cache_status', 'resp_date',
                'status', 'request_url']
CANDIDATE_COLS = ['url', 'url_key', 'slug_ym_key', 'source_set', 'sources',
                  'list_type', 'list_title', 'list_date', 'lastmod', 'in_corpus',
                  'corpus_match', 'classified', 'probe_priority', 'probe_status',
                  'first_seen']
PROBE_COLS = ['url', 'url_key', 'slug_date_key', 'final_url', 'final_host', 'status',
              'error', 'redirected', 'date_published', 'date_modified',
              'content_location', 'author', 'block_type', 'byline', 'dateline',
              'block_date', 'title', 'rubio_label_count', 'fuzzy_label_candidates',
              'label_method', 'has_translated_transcript', 'has_courtesy_translation',
              'has_video_message_below', 'has_as_prepared', 'has_as_delivered',
              'has_transcript', 'third_person_opener', 'readout_opener', 'word_count',
              'first_600', 'gt_class', 'modality', 'event_date', 'in_scope',
              'needs_review', 'reason', 'probe_runs', 'probed_at']
GT_COLS = ['url_key', 'slug_date_key', 'canonical_url', 'gt_class', 'modality',
           'event_date', 'source_set', 'in_scope', 'first_seen', 'in_corpus',
           'corpus_row_id', 'needs_review', 'reason']
DIFF_COLS = ['url_key', 'canonical_url', 'gt_class', 'modality', 'event_date',
             'in_scope', 'source_set', 'needs_review', 'reason', 'title',
             'block_type', 'byline', 'status', 'first_600']
DRIFT_COLS = ['kind', 'listing', 'url_key', 'page_p1', 'pos_p1', 'page_p2', 'pos_p2',
              'value', 'detail']


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def url_key(url: str) -> str:
    """Matching key: canonical_url_key of the live-renamed URL, query/fragment dropped.

    canonicalize_live_url applies the office-of-the-spokesperson ->
    office-of-the-spokesman fold, host-scoped to www.state.gov, so frozen
    archive hosts keep their paths.
    """
    raw = (url or '').strip().split('#', 1)[0].split('?', 1)[0]
    return canonical_url_key(canonicalize_live_url(raw))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _iso_list_date(text: str) -> str:
    """'May 20, 2026' -> '2026-05-20'; '' when unparseable."""
    try:
        return datetime.strptime((text or '').strip(), '%B %d, %Y').date().isoformat()
    except ValueError:
        return ''


def _norm_space(text: str) -> str:
    text = (text or '').replace('\u2018', "'").replace('\u2019', "'")
    return ' '.join(text.split())


def _family(loc: str) -> str:
    m = _SITEMAP_FAMILY_RE.search(loc or '')
    return m.group(1).lower() if m else ''


def _write_csv(path: Path, cols: list[str], rows) -> int:
    n = 0
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, '') for c in cols})
            n += 1
    return n


# ---------------------------------------------------------------------------
# Polite fetcher
# ---------------------------------------------------------------------------

class BudgetExhausted(RuntimeError):
    """--max-requests (or --max-minutes) reached; state is saved, run resumable."""


class FetchResult:
    __slots__ = ('url', 'resp', 'error', 'attempts')

    def __init__(self, url, resp=None, error='', attempts=0):
        self.url = url
        self.resp = resp
        self.error = error
        self.attempts = attempts

    @property
    def status(self) -> int:
        return int(getattr(self.resp, 'status_code', 0) or 0)

    @property
    def ok(self) -> bool:
        return self.resp is not None and 200 <= self.status < 300


def _jitter(url: str, span: float) -> float:
    """Deterministic jitter in [0, span) from the URL hash (reproducible runs)."""
    if span <= 0:
        return 0.0
    h = int(hashlib.sha1(url.encode('utf-8')).hexdigest()[:8], 16)
    return span * (h / 0x100000000)


def _retry_after_seconds(value: str, now: float | None = None) -> float | None:
    """Retry-After as delta-seconds or HTTP-date; None when absent/unparseable."""
    value = (value or '').strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    ref = now if now is not None else time.time()
    return max(0.0, when.timestamp() - ref)


class PoliteFetcher:
    """One session, spaced requests, bounded backoff, hard request cap.

    `sleep` and `clock` are injectable so the tests can check spacing and the
    cap without real time passing. `used` counts every HTTP attempt this run,
    retries included; the cap is checked BEFORE each attempt.
    """

    def __init__(self, session, *, max_requests: int, min_interval: float = 2.0,
                 jitter: float = 1.0, max_attempts: int = _MAX_ATTEMPTS,
                 backoff_base: float = _BACKOFF_BASE, deadline: float | None = None,
                 sleep=time.sleep, clock=time.monotonic):
        self.session = session
        self.max_requests = max_requests
        self.min_interval = min_interval
        self.jitter = jitter
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.deadline = deadline
        self.sleep = sleep
        self.clock = clock
        self.used = 0
        self.retries = 0
        self.status_counts: dict[str, int] = {}
        self._last: float | None = None

    @property
    def remaining(self) -> int:
        return max(0, self.max_requests - self.used)

    def _space(self, url: str) -> None:
        if self._last is None:
            return
        wait = self.min_interval + _jitter(url, self.jitter) - (self.clock() - self._last)
        if wait > 0:
            self.sleep(wait)

    def get(self, url: str) -> FetchResult:
        last_err = ''
        resp = None
        for attempt in range(1, self.max_attempts + 1):
            if self.used >= self.max_requests:
                raise BudgetExhausted(f'request cap {self.max_requests} reached')
            if self.deadline is not None and self.clock() >= self.deadline:
                raise BudgetExhausted('wall-clock budget (--max-minutes) reached')
            self._space(url)
            self.used += 1
            self._last = self.clock()
            try:
                resp = self.session.get(url, headers=_HEADERS, timeout=_TIMEOUT,
                                        allow_redirects=True)
                last_err = ''
            except Exception as e:  # noqa: BLE001 -- requests raises a wide family
                resp = None
                last_err = f'{type(e).__name__}: {e}'
            key = str(getattr(resp, 'status_code', 'exc'))
            self.status_counts[key] = self.status_counts.get(key, 0) + 1
            retryable = resp is None or resp.status_code in _RETRY_STATUSES
            if not retryable or attempt == self.max_attempts:
                break
            delay = None
            if resp is not None:
                delay = _retry_after_seconds(resp.headers.get('Retry-After', ''))
            if delay is None:
                delay = self.backoff_base * (2 ** (attempt - 1))
            delay = min(delay, _BACKOFF_CAP)
            self.retries += 1
            print(f'{LOG} retry {attempt}/{self.max_attempts - 1} in {delay:.0f}s for {url}: '
                  f'{last_err or resp.status_code}', file=sys.stderr)
            self.sleep(delay)
            # The backoff already spaced this request out.
            self._last = self.clock()
        if resp is None:
            return FetchResult(url, None, last_err or 'no response', attempt)
        err = '' if 200 <= resp.status_code < 300 else f'HTTP {resp.status_code}'
        return FetchResult(url, resp, err, attempt)


# ---------------------------------------------------------------------------
# Offline fixture session (tests and --offline-fixtures dry runs)
# ---------------------------------------------------------------------------

class _FixtureResponse:
    def __init__(self, url, status, text, headers, history=()):
        self.url = url
        self.status_code = status
        self.text = text
        self.headers = headers
        self.history = list(history)


class FixtureSession:
    """A requests.Session stand-in that serves files listed in manifest.json.

    manifest.json: {"<url>": {"file": "x.html", "status": 200,
                              "headers": {...}, "final_url": "<url>"}}
    Lookup is exact, then with the query string dropped (so the E3 cache-buster
    pass hits the same fixture). Unknown URLs get a 404. Never touches the network.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        with open(self.root / 'manifest.json', encoding='utf-8') as f:
            self.manifest = json.load(f)
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        self.calls.append(url)
        entry = self.manifest.get(url) or self.manifest.get(url.split('?', 1)[0])
        if entry is None:
            return _FixtureResponse(url, 404, '', {})
        text = ''
        if entry.get('file'):
            text = (self.root / entry['file']).read_text(encoding='utf-8')
        final = entry.get('final_url') or url
        history = [object()] if final != url else []
        return _FixtureResponse(final, int(entry.get('status', 200)), text,
                                dict(entry.get('headers') or {}), history)


# ---------------------------------------------------------------------------
# Parsers (pure functions; unit-tested offline)
# ---------------------------------------------------------------------------

def _xml_children(root, name: str):
    """Elements whose local tag is `name`, namespace-agnostic."""
    for el in root.iter():
        tag = el.tag.rsplit('}', 1)[-1] if isinstance(el.tag, str) else ''
        if tag == name:
            yield el


def _child_text(el, name: str) -> str:
    for ch in el:
        tag = ch.tag.rsplit('}', 1)[-1] if isinstance(ch.tag, str) else ''
        if tag == name:
            return (ch.text or '').strip()
    return ''


def parse_sitemap_index(xml_text: str) -> list[dict]:
    """sitemap_index.xml -> [{'loc', 'lastmod'}]."""
    root = ElementTree.fromstring(xml_text)
    return [{'loc': _child_text(sm, 'loc'), 'lastmod': _child_text(sm, 'lastmod')}
            for sm in _xml_children(root, 'sitemap') if _child_text(sm, 'loc')]


def parse_urlset(xml_text: str) -> list[dict]:
    """A child sitemap -> [{'loc', 'lastmod'}]."""
    root = ElementTree.fromstring(xml_text)
    return [{'loc': _child_text(u, 'loc'), 'lastmod': _child_text(u, 'lastmod')}
            for u in _xml_children(root, 'url') if _child_text(u, 'loc')]


def e2_in_window(loc: str, lastmod: str, *,
                 month_floor=DEFAULT_URL_MONTH_FLOOR,
                 lastmod_floor: date = DEFAULT_LASTMOD_FLOOR) -> bool:
    """URL month decides when the URL has one; otherwise lastmod; no lastmod keeps.

    A dated URL older than the floor is dropped even if edited recently --
    a pre-2025 release cannot be a Rubio-as-Secretary item.
    """
    ym = url_year_month(loc)
    if ym is not None:
        return ym >= tuple(month_floor)
    if not lastmod:
        return True
    try:
        return date.fromisoformat(lastmod[:10]) >= lastmod_floor
    except ValueError:
        return True


def parse_listing_page(html: str, base_url: str) -> dict:
    """One collection listing page -> items + pagination evidence.

    Tolerant by design: unknown markup yields items=[] plus whatever page-level
    evidence could be read, and the caller logs it rather than crashing.
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
        href = urljoin(base_url, href)
        title = _norm_space(a.get_text(' ', strip=True))
        texts = [_norm_space(t) for t in el.stripped_strings]
        texts = [t for t in texts if t and t != title]
        list_date = ''
        type_line = ''
        for t in texts:
            m = _LIST_DATE_RE.search(t)
            if m and not list_date:
                list_date = m.group(0)
                continue
            if not type_line and len(t) <= 60:
                type_line = t
        items.append({'href': href, 'title': title, 'type': type_line,
                      'list_date': list_date})

    total_pages = ''
    inp = soup.find('input', attrs={'name': 'total_pages'})
    if inp is not None:
        total_pages = (inp.get('value') or '').strip()

    max_link_page = 1
    path = urlsplit(base_url).path.rstrip('/')
    path = re.sub(r'/page/\d+$', '', path)
    page_re = re.compile(re.escape(path) + r'/page/(\d+)/?')
    for a in soup.find_all('a', href=True):
        m = page_re.search(a['href'])
        if m:
            max_link_page = max(max_link_page, int(m.group(1)))

    items_header = ''
    for el in soup.find_all(class_=re.compile(r'collection-info|results?-count|count')):
        m = _ITEMS_HEADER_RE.search(el.get_text(' ', strip=True))
        if m:
            items_header = _norm_space(el.get_text(' ', strip=True))[:80]
            break
    if not items_header:
        m = _ITEMS_HEADER_RE.search(soup.get_text(' ', strip=True)[:20000])
        if m:
            items_header = m.group(0)

    return {'items': items, 'total_pages': total_pages,
            'max_link_page': max_link_page, 'items_header': items_header}


def _items_header_count(text: str) -> int | None:
    m = _ITEMS_HEADER_RE.search(text or '')
    if not m:
        return None
    try:
        return int(m.group(1).replace(',', ''))
    except ValueError:
        return None


def parse_jsonld(soup) -> dict:
    """Same flattening as rubio_backfill.fetch_rubio_page, plus author/dateModified."""
    ld: dict = {}
    for tag in soup.find_all('script', type='application/ld+json'):
        try:
            ld.update(_flatten_jsonld(json.loads(tag.string or '{}')))
        except Exception:  # noqa: BLE001 -- malformed JSON-LD is common
            pass
    # Yoast emits an @graph list; fold the Article/WebPage nodes in too.
    for node in ld.get('@graph', []) if isinstance(ld.get('@graph'), list) else []:
        if isinstance(node, dict):
            for k in ('datePublished', 'dateModified', 'contentLocation', 'author'):
                if k in node and k not in ld:
                    ld[k] = node[k]
    cl = ld.get('contentLocation', '')
    if isinstance(cl, dict):
        cl = cl.get('name', '') or cl.get('addressLocality', '')
    author = ld.get('author', '')
    if isinstance(author, list):
        author = '; '.join(a.get('name', '') if isinstance(a, dict) else str(a) for a in author)
    elif isinstance(author, dict):
        author = author.get('name', '')
    return {'date_published': str(ld.get('datePublished', '') or ''),
            'date_modified': str(ld.get('dateModified', '') or ''),
            'content_location': str(cl or ''),
            'author': str(author or '')}


def parse_block(raw: str) -> dict:
    """Title / type / byline / dateline / date from the hide -> Tags block.

    Generalizes rubio_backfill._parse_block_metadata, which reads only the
    type and a location: here every meta line between the type and the date
    is split into byline lines ("Name, Title" or Office of the Spokesperson)
    and dateline lines (venue, city), because joint items carry several
    bylines.
    """
    m = _HIDE_ANCHOR_RE.search(raw or '')
    if not m:
        return {}
    after = raw[m.end():]
    end = after.find(_TAGS_SENTINEL)
    if end == -1:
        end = after.find('\nBack to Top\n')
    block = after[:end] if end != -1 else after
    lines = [l.strip() for l in block.split('\n') if l.strip()]
    date_idx = next((i for i, l in enumerate(lines[:12]) if _DATE_LINE_RE.match(l)), -1)
    meta = lines[:date_idx] if date_idx != -1 else lines[:2]
    out = {'title': meta[0] if meta else '',
           'block_type': meta[1] if len(meta) > 1 else '',
           'block_date': lines[date_idx] if date_idx != -1 else '',
           'bylines': [], 'datelines': []}
    for line in meta[2:]:
        if _SPOKES_OFFICE_RE.match(line):
            out['bylines'].append(line)
            continue
        name, sep, rest = line.partition(', ')
        if sep and _TITLE_WORDS_RE.search(rest) and len(name) <= 60:
            out['bylines'].append(line)
        else:
            out['datelines'].append(line)
    return out


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


_STRICT_LABEL_NAME_RE = re.compile(
    r"(?:THE )?SECRETARY(?:[- ]DESIGNATE)?(?: OF STATE)?(?: [A-Z.']+)*? RUBIO")


def count_rubio_labels(text: str) -> tuple[int, list[str], str]:
    """(strict own-line Rubio label count, fuzzy candidates, method).

    Fuzzy candidates are label lines the strict pattern did not take but
    which look like a mangled Rubio label: 'RUB' anywhere, or a SECRETARY
    label whose last word is within edit distance 2 of RUBIO ("SECRETARY
    RUBI:", X-06). MRS RUBIO / SENATOR RUBIO are listed (for review) but
    never counted.

    Prefers scripts/rubio_turns.py (plan step 2c): its rubio_text() stats
    give n_rubio_turns (fuzzy-resolved, so "SECRETARY RUBI:" counts) and
    unresolved_labels. Absent or failing, the local strict regex is used.
    """
    text = text or ''
    fuzzy: list[str] = []
    for m in _LABEL_LINE_RE.finditer(text):
        lab = _norm_space(m.group(1))
        if _STRICT_LABEL_NAME_RE.fullmatch(lab) or lab in fuzzy:
            continue
        last = lab.split()[-1] if lab.split() else ''
        if 'RUB' in lab or (lab.startswith('SECRETARY') and _edit_distance(last, 'RUBIO') <= 2):
            fuzzy.append(lab)
    fn = getattr(rubio_turns, 'rubio_text', None) if rubio_turns else None
    if callable(fn):
        try:
            _, st = fn(text)
            for lab in st.get('unresolved_labels', []):
                if lab not in fuzzy:
                    fuzzy.append(lab)
            return int(st['n_rubio_turns']), fuzzy[:10], 'rubio_turns'
        except Exception as e:  # noqa: BLE001
            print(f'{LOG} WARNING: rubio_turns.rubio_text failed ({e}); '
                  f'using local regex', file=sys.stderr)
    return len(_RUBIO_LABEL_RE.findall(text)), fuzzy[:10], 'local_regex'


def classify_page(raw: str, *, url: str = '', final_url: str = '', status: int = 200,
                  error: str = '', jsonld: dict | None = None,
                  scope_floor: date = DEFAULT_SCOPE_FLOOR) -> dict:
    """Derive gt_class / modality / in_scope from flattened page text.

    `raw` is soup.get_text('\\n', strip=True) -- the same form as the corpus
    "Raw Text" column -- so a live probe and an offline corpus row go through
    one code path.

    Precedence (first match wins):
      external_redirect  final host is not state.gov (scope: state.gov only)
      fetch_error        no response or non-2xx
      rubio_video_transcript  third-person intro + transcript marker + video
      rubio_spoken       own-line Rubio labels, or third-person intro +
                         as-prepared/as-delivered marker, or a spoken block
                         type with Rubio as sole byline
      readout            'The below is attributable to', or type Readout
      rubio_joint        Rubio plus another principal in the byline
      spokesperson_third_person  third-person opening (any byline), or an
                         Office of the Spokesperson byline
      rubio_written_byline  Rubio sole byline, first-person text
      other_principal    anything else (briefings by others, other bylines)
    """
    jsonld = jsonld or {}
    final_url = final_url or url
    host = urlsplit(final_url).hostname or ''
    out = {'needs_review': 0, 'reason': ''}
    block = parse_block(raw)
    content = _extract_modern_state_content(raw or '') if raw else ''
    if raw and not content:
        content = raw
        out['needs_review'] = 1
    sample = _norm_space(content[:800])
    labels, fuzzy, method = count_rubio_labels(content)
    flags = {k: int(bool(rx.search(content[:3000]))) for k, rx in _MARKERS.items()}
    third = bool(_THIRD_PERSON_RE.match(sample))
    readout = bool(_READOUT_RE.search(sample[:400])) or block.get('block_type', '').lower() == 'readout'
    bylines = block.get('bylines', [])
    rubio_by = any('Rubio' in b for b in bylines)
    others = [b for b in bylines if 'Rubio' not in b and not _SPOKES_OFFICE_RE.match(b)]
    spokes = any(_SPOKES_OFFICE_RE.match(b) for b in bylines)
    btype = block.get('block_type', '').strip().lower()

    out.update({
        'block_type': block.get('block_type', ''),
        'byline': ' | '.join(bylines),
        'dateline': ' | '.join(block.get('datelines', [])),
        'block_date': block.get('block_date', ''),
        'title': block.get('title', ''),
        'rubio_label_count': labels,
        'fuzzy_label_candidates': ' | '.join(fuzzy),
        'label_method': method,
        'third_person_opener': int(third),
        'readout_opener': int(readout),
        'word_count': len(content.split()),
        'first_600': content[:600],
        **flags,
    })

    pub = _parse_date(jsonld.get('date_published', ''), url or final_url,
                      block.get('block_date', ''))
    event = _iso_list_date(block.get('block_date', '')) or pub
    out['event_date'] = event
    out['slug_date_key'] = slug_date_key(url or final_url, pub)

    transcript_marker = bool(flags['has_translated_transcript'] or flags['has_courtesy_translation']
                             or flags['has_video_message_below'])
    prepared_marker = bool(flags['has_as_prepared'] or flags['has_as_delivered'])
    video = bool(_VIDEO_RE.search(content[:3000]))

    if host and not (host == 'state.gov' or host.endswith('.state.gov')):
        cls, why = 'external_redirect', f'final host {host}'
    elif status == 0 or not (200 <= status < 300) or not raw:
        cls, why = 'fetch_error', error or f'HTTP {status}'
    elif third and video and (transcript_marker or prepared_marker or labels):
        cls, why = 'rubio_video_transcript', 'third-person intro + video + transcript marker'
    elif labels > 0:
        cls, why = 'rubio_spoken', f'{labels} Rubio label(s)'
    elif third and (prepared_marker or transcript_marker):
        cls, why = 'rubio_spoken', 'third-person intro + as-prepared/transcript marker'
        out['needs_review'] = 1
    elif readout:
        cls, why = 'readout', 'readout opener/type'
    elif rubio_by and others:
        cls, why = 'rubio_joint', f'co-bylined with {len(others)} principal(s)'
    elif third:
        cls, why = 'spokesperson_third_person', 'third-person opening'
    elif rubio_by and btype in _SPOKEN_TYPES:
        cls, why = 'rubio_spoken', f'Rubio byline, spoken type "{block.get("block_type")}", no labels'
        out['needs_review'] = 1
    elif rubio_by:
        cls, why = 'rubio_written_byline', f'Rubio byline, type "{block.get("block_type")}"'
    elif spokes:
        cls, why = 'spokesperson_third_person', 'Office of the Spokesperson byline'
        if 'joint statement' in btype and 'Rubio' in content:
            out['needs_review'] = 1
            why += ' (joint statement naming Rubio -- check co-signature)'
    else:
        cls, why = 'other_principal', ('byline: ' + '; '.join(others)) if others else 'no Rubio byline/labels'
        if not bylines:
            out['needs_review'] = 1

    out['gt_class'] = cls
    out['reason'] = why
    out['modality'] = IN_SCOPE_CLASSES.get(cls, '')
    if cls == 'fetch_error':
        out['in_scope'] = ''          # unknown until re-probed
    elif cls in IN_SCOPE_CLASSES:
        try:
            ok = date.fromisoformat(event) >= scope_floor if event else True
        except ValueError:
            ok = True
        if not event:
            out['needs_review'] = 1
        out['in_scope'] = 1 if ok else 0
        if not ok:
            out['reason'] += f'; event_date {event} < {scope_floor} (pre-tenure)'
    else:
        out['in_scope'] = 0
    return out


def probe_page(fetcher: PoliteFetcher, url: str, scope_floor: date) -> dict:
    """E6: fetch one diff-set page and classify it."""
    res = fetcher.get(url)
    resp = res.resp
    final_url = getattr(resp, 'url', '') or url if resp is not None else url
    row = {'url': url, 'url_key': url_key(url), 'final_url': final_url,
           'final_host': urlsplit(final_url).hostname or '', 'status': res.status,
           'error': res.error, 'redirected': int(bool(getattr(resp, 'history', None))),
           'probed_at': _now_iso()}
    raw = ''
    ld = {}
    if resp is not None and res.ok:
        try:
            soup = _make_soup(resp.text)
            raw = soup.get_text('\n', strip=True)
            ld = parse_jsonld(soup)
        except Exception as e:  # noqa: BLE001
            row['error'] = f'parse: {e}'
    row.update(ld)
    row.update(classify_page(raw, url=url, final_url=final_url, status=res.status,
                             error=row['error'], jsonld=ld, scope_floor=scope_floor))
    return row


# ---------------------------------------------------------------------------
# Resumable state
# ---------------------------------------------------------------------------

def new_state() -> dict:
    return {
        'version': 1,
        'runs': [],
        'requests_total': 0,
        'stages': {},              # stage -> 'done' | 'partial'
        'index': None,             # [{'loc','lastmod'}] from sitemap_index.xml
        'sitemaps': {},            # child_loc -> inventory row
        'e2_urls': {},             # url -> {'lastmod','sitemap'}
        'listings': {},            # 'E3:remarks:1' -> {'pages': {n: page record}, 'done': bool, ...}
        'probes': {},              # url_key -> probe row
        'first_seen': {},          # url_key -> YYYY-MM-DD
        'errors': [],
    }


def load_state(path: Path | None) -> dict:
    if not path or not Path(path).exists():
        return new_state()
    with open(path, encoding='utf-8') as f:
        st = json.load(f)
    base = new_state()
    base.update(st)
    print(f'{LOG} resumed state {path}: {base["requests_total"]} prior requests, '
          f'stages {base["stages"]}', file=sys.stderr)
    return base


# ---------------------------------------------------------------------------
# T1: translations (decided 2026-10-22: a /translations/<lang>/ page is in scope
# only when it has NO English counterpart). Every translation page carries a
# "view the original" line in its language -- "Para ver el texto original, ir
# a: https://www.state.gov/...", "Voir le contenu d'origine : ...",
# "欲查看原稿内容: ...", "Для просмотра оригинала: ..." -- whose link is the
# English page. That link, not the language, decides: no original link means a
# unique translation, which goes to hand review.
# ---------------------------------------------------------------------------

_TRANSLATION_RE = re.compile(r'^/translations/([^/]+)/([^/]+)/?$')
TRANSLATION_COLS = ['url', 'url_key', 'lang', 'status', 'error', 'title', 'original_url',
                    'original_key', 'original_known', 't_class', 'needs_review',
                    'first_300', 'probe_runs', 'probed_at']


def translation_lang(url: str) -> str:
    """'spanish', 'العربية', ... for a translation item URL; '' for anything else
    (including the /translations/<lang>/ index pages)."""
    from urllib.parse import unquote
    m = _TRANSLATION_RE.match(unquote(urlsplit(url).path))
    return m.group(1) if m else ''


def _is_english_title(text: str) -> bool:
    """An English page title used as anchor text: five or more words, nearly all
    ASCII letters (navigation and tag links are short or in the page's language)."""
    words = text.split()
    letters = [c for c in text if c.isalpha()]
    return (len(words) >= 5 and bool(letters)
            and sum(c.isascii() for c in letters) / len(letters) > 0.95)


_NON_ITEM_PREFIXES = ('bureau', 'office-', 'countries-areas', 'policy-issues', 'about',
                      'biographies', 'remarks-secretary-', 'releases', 'translations')


def _is_item_path(path: str) -> bool:
    """A release/remarks page, not navigation: /releases/<office>/YYYY/MM/<slug>/ or a
    flat /<long-slug>/ that is not a bureau, office, country or topic page."""
    if re.search(r'^/releases/[^/]+/\d{4}/\d{2}/[^/]+/?$', path):
        return True
    parts = [p for p in path.split('/') if p]
    return (len(parts) == 1 and parts[0].count('-') >= 4
            and not parts[0].startswith(_NON_ITEM_PREFIXES))


def find_original_url(soup) -> str:
    """The English original a translation page links to: the first state.gov
    link in the page body, outside /translations/, whose anchor text is the URL
    itself ("Para ver el texto original, ir a: https://...") or the English
    title (Persian/Urdu pages: "پیوند به محتوای اصلی: Secretary of State Marco
    Rubio Remarks to the Press at ...").

    URL-text anchors win over title anchors; /biographies/ links (a byline such
    as "MARCO RUBIO, SECRETARIO DE ESTADO" can link there) never count.
    """
    body = soup.find('main') or soup
    url_hits, title_hits = [], []
    for a in body.find_all('a', href=True):
        href = a['href'].strip()
        host = (urlsplit(href).hostname or '').lower()
        path = urlsplit(href).path
        if not (host == 'state.gov' or host.endswith('.state.gov')):
            continue
        if '/translations/' in path or path.startswith('/biographies/'):
            continue
        text = a.get_text(' ', strip=True)
        if text.startswith('http'):
            url_hits.append(href)
        elif _is_english_title(text) and _is_item_path(path):
            title_hits.append(href)
    return (url_hits or title_hits or [''])[0]


def probe_translation(fetcher: 'PoliteFetcher', url: str, known_keys: set) -> dict:
    res = fetcher.get(url)
    row = {'url': url, 'url_key': url_key(url), 'lang': translation_lang(url),
           'status': res.status, 'error': res.error, 'probed_at': _now_iso(),
           'title': '', 'original_url': '', 'original_key': '', 'original_known': '',
           'first_300': ''}
    if res.resp is None or not res.ok:
        row.update(t_class='fetch_error', needs_review=0)
        return row
    try:
        soup = _make_soup(res.resp.text)
    except Exception as e:  # noqa: BLE001
        row.update(error=f'parse: {e}', t_class='fetch_error', needs_review=0)
        return row
    h1 = soup.find('h1')
    row['title'] = h1.get_text(' ', strip=True)[:300] if h1 else ''
    raw = soup.get_text('\n', strip=True)
    hide = raw.find('\nhide\n')
    row['first_300'] = raw[hide + 6: hide + 306] if hide >= 0 else raw[:300]
    orig = find_original_url(soup)
    if orig:
        okey = url_key(orig)
        row.update(original_url=orig, original_key=okey,
                   original_known=int(okey in known_keys
                                      or canonical_url_key(orig.split('?')[0]) in known_keys),
                   t_class='translation_of_english', needs_review=0)
    else:
        row.update(t_class='translation_unique', needs_review=1)
    return row


def run_t1(fetcher: 'PoliteFetcher', state: dict, known_keys: set) -> tuple[int, int]:
    """Probe every translation item URL from the sitemaps once (resumable).
    Returns (probed this run, still pending)."""
    done = state.setdefault('translations', {})
    todo = []
    for u in state['e2_urls']:
        if not translation_lang(u):
            continue
        k = url_key(u)
        prev = done.get(k)
        if prev and (prev['t_class'] != 'fetch_error' or int(prev.get('probe_runs') or 1)
                     >= MAX_PROBE_RUNS or int(prev.get('status') or 0) in (404, 410)):
            continue
        todo.append(u)
    # Rubio-named slugs first (any script), then the rest; stable order for resume.
    named = re.compile(r'rubio|рубио|روبيو|روبیو|卢比奥|रुबियो', re.I)
    from urllib.parse import unquote
    todo.sort(key=lambda u: (not named.search(unquote(u)), u))
    print(f'{LOG} T1: {len(todo)} translation page(s) to probe, {fetcher.remaining} '
          f'request(s) left', file=sys.stderr)
    n = 0
    for u in todo:
        k = url_key(u)
        prev = done.get(k, {})
        row = probe_translation(fetcher, u, known_keys)
        row['probe_runs'] = int(prev.get('probe_runs') or 0) + 1
        done[k] = row
        n += 1
        if n % 50 == 0:
            print(f'  [T1 {n}/{len(todo)}]', file=sys.stderr)
    return n, len(todo) - n


def save_state(state: dict, path: Path) -> None:
    tmp = Path(str(path) + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

def stage_sitemaps(fetcher: PoliteFetcher, state: dict, *, do_e1: bool, do_e2: bool,
                   month_floor, lastmod_floor) -> None:
    """E1 (+E2): fetch the index, then each child once; E2 reuses E1's fetches."""
    if state['index'] is None:
        res = fetcher.get(SITEMAP_INDEX)
        if not res.ok:
            raise RuntimeError(f'sitemap index unreachable: {res.error}')
        state['index'] = parse_sitemap_index(res.resp.text)
        print(f'{LOG} E1 sitemap index: {len(state["index"])} children', file=sys.stderr)
    children = state['index']
    if not do_e1:
        children = [c for c in children if _family(c['loc']) in E2_FAMILIES]
    todo = [c for c in children if c['loc'] not in state['sitemaps']]
    for i, child in enumerate(todo, 1):
        loc = child['loc']
        fam = _family(loc)
        res = fetcher.get(loc)
        inv = {'child_loc': loc, 'family': fam, 'index_lastmod': child.get('lastmod', ''),
               'status': res.status, 'error': res.error}
        if res.ok:
            try:
                urls = parse_urlset(res.resp.text)
            except ElementTree.ParseError as e:
                urls = []
                inv['error'] = f'xml: {e}'
            mods = sorted(u['lastmod'][:10] for u in urls if u['lastmod'])
            n_win = 0
            for u in urls:
                if fam in E2_FAMILIES and e2_in_window(u['loc'], u['lastmod'],
                                                       month_floor=month_floor,
                                                       lastmod_floor=lastmod_floor):
                    n_win += 1
                    state['e2_urls'].setdefault(u['loc'], {'lastmod': u['lastmod'], 'sitemap': loc})
            inv.update({'n_urls': len(urls), 'lastmod_min': mods[0] if mods else '',
                        'lastmod_max': mods[-1] if mods else '', 'n_in_window': n_win,
                        'sample_paths': '|'.join(urlsplit(u['loc']).path for u in urls[:3])})
        else:
            print(f'{LOG} WARNING: sitemap {loc} failed: {res.error}', file=sys.stderr)
            state['errors'].append({'stage': 'E1', 'url': loc, 'error': res.error})
        if res.ok or res.status == 404:
            # A 404 child is a real inventory fact; transient failures stay
            # out of state so a resumed run retries them.
            state['sitemaps'][loc] = inv
        if i % 10 == 0:
            print(f'  [sitemaps {i}/{len(todo)}] {len(state["e2_urls"])} E2 URLs so far',
                  file=sys.stderr)


def _page_url(index: str, page: int, buster: str = '') -> str:
    url = index if page == 1 else f'{index}page/{page}/'
    return f'{url}?gt={buster}' if buster else url


def walk_listing(fetcher: PoliteFetcher, state: dict, *, stage: str, listing: str,
                 index: str, pass_no: int = 1, buster: str = '', max_pages: int = 0,
                 stop_before: date | None = None) -> None:
    """Walk one paginated collection, recording every slot of every page.

    E3 (stop_before=None) walks every page. E4 stops after two consecutive
    pages whose dated items are all older than stop_before. Unknown markup
    (0 items on page 1) is logged and ends that listing, never raised.

    The last page is max(input[name=total_pages], highest /page/N/ link),
    re-evaluated on every page so a windowed paginator cannot pin it at 1
    (D-06). Failed pages are kept in rec['failed'] for the snapshot and are
    retried on resume; the listing is 'done' only when no page is missing.
    """
    key = f'{stage}:{listing}:{pass_no}'
    rec = state['listings'].setdefault(key, {
        'pages': {}, 'failed': {}, 'done': False, 'last_page': None,
        'stopped': False, 'stale_streak': 0, 'buster': buster})
    if rec['done']:
        return
    buster = rec.get('buster') or buster
    page = 1
    while True:
        last = rec['last_page']
        if last is not None and page > last:
            break
        if str(page) in rec['pages']:
            page += 1
            continue
        req_url = _page_url(index, page, buster)
        res = fetcher.get(req_url)
        hdr = {k.lower(): v for k, v in (res.resp.headers.items() if res.resp is not None else [])}
        page_rec = {'status': res.status, 'error': res.error, 'request_url': req_url,
                    'x_cache': hdr.get('x-cache', ''), 'age': hdr.get('age', ''),
                    'cf_cache_status': hdr.get('cf-cache-status', ''),
                    'resp_date': hdr.get('date', ''), 'fetched_at': _now_iso(),
                    'items': [], 'total_pages': '', 'items_header': '', 'max_link_page': 1}
        if not res.ok:
            print(f'{LOG} WARNING: {key} page {page} failed: {res.error}', file=sys.stderr)
            state['errors'].append({'stage': stage, 'url': req_url, 'error': res.error})
            rec['failed'][str(page)] = page_rec
            if page == 1:
                # Without page 1 the page count is unknown; resume retries it.
                return
            page += 1
            continue
        rec['failed'].pop(str(page), None)
        page_rec.update(parse_listing_page(res.resp.text, req_url))
        rec['pages'][str(page)] = page_rec
        items = page_rec['items']

        if not rec['stopped']:
            tp = page_rec.get('total_pages') or ''
            seen_last = max(int(tp) if tp.isdigit() else 0, page_rec.get('max_link_page') or 1, page)
            rec['last_page'] = max(rec['last_page'] or 1, seen_last)
            if max_pages:
                rec['last_page'] = min(rec['last_page'], max_pages)
        if not items:
            if page == 1:
                print(f'{LOG} WARNING: {key} page 1 has 0 .collection-result items -- '
                      f'unknown markup? ending this listing', file=sys.stderr)
                state['errors'].append({'stage': stage, 'url': req_url,
                                        'error': 'page 1: 0 collection-result items'})
            rec['last_page'] = page   # ran off the end (or unreadable markup)
            rec['stopped'] = True
        elif stop_before is not None:
            dates = [d for d in (_iso_list_date(it['list_date']) for it in items) if d]
            if dates and max(dates) < stop_before.isoformat():
                rec['stale_streak'] += 1
            elif dates:
                rec['stale_streak'] = 0
            else:
                print(f'{LOG} WARNING: {key} page {page}: no parseable list dates',
                      file=sys.stderr)
            if rec['stale_streak'] >= 2:
                rec['last_page'] = page
                rec['stopped'] = True
                print(f'{LOG} {key}: stop after page {page} (items older than {stop_before})',
                      file=sys.stderr)
        if page % 10 == 0:
            print(f'  [{key} {page}/{rec["last_page"]}]', file=sys.stderr)
        page += 1

    last = rec['last_page'] or 1
    missing = [p for p in range(1, last + 1) if str(p) not in rec['pages']]
    rec['done'] = not missing
    if missing:
        print(f'{LOG} WARNING: {key}: {len(missing)} page(s) unread: {missing[:10]}',
              file=sys.stderr)


def listing_rows(state: dict):
    for key, rec in sorted(state['listings'].items()):
        stage, listing, pass_no = key.split(':')
        pages = {**rec.get('failed', {}), **rec['pages']}
        for pg in sorted(pages, key=int):
            p = pages[pg]
            base = {'stage': stage, 'listing': listing, 'pass': pass_no, 'page': pg,
                    'total_pages': p.get('total_pages', ''),
                    'items_header': p.get('items_header', ''), 'x_cache': p.get('x_cache', ''),
                    'age': p.get('age', ''), 'cf_cache_status': p.get('cf_cache_status', ''),
                    'resp_date': p.get('resp_date', ''), 'status': p.get('status', ''),
                    'request_url': p.get('request_url', '')}
            if not p.get('items'):
                yield {**base, 'pos': ''}
                continue
            for pos, it in enumerate(p['items'], 1):
                iso = _iso_list_date(it['list_date'])
                yield {**base, 'pos': pos, 'href': it['href'], 'url_key': url_key(it['href']),
                       'slug_date_key': slug_date_key(it['href'], iso) if iso else '',
                       'type': it['type'], 'title': it['title'], 'list_date': it['list_date']}


def drift_rows(state: dict):
    """E3 pass 1 vs pass 2: duplicate slots, header vs unique count, set diff."""
    passes = {}
    for key, rec in state['listings'].items():
        stage, listing, pass_no = key.split(':')
        if stage != 'E3':
            continue
        slots = []
        header = ''
        tp = ''
        for pg in sorted(rec['pages'], key=int):
            p = rec['pages'][pg]
            header = header or p.get('items_header', '')
            tp = tp or p.get('total_pages', '')
            for pos, it in enumerate(p.get('items', []), 1):
                slots.append((url_key(it['href']), int(pg), pos))
        passes[pass_no] = {'slots': slots, 'header': header, 'total_pages': tp,
                           'done': rec['done'], 'listing': listing}
    for pass_no, info in sorted(passes.items()):
        keys = [s[0] for s in info['slots']]
        uniq = set(keys)
        n_hdr = _items_header_count(info['header'])
        for name, val in (('slots', len(keys)), ('unique', len(uniq)),
                          ('dup_slots', len(keys) - len(uniq)),
                          ('items_header', n_hdr if n_hdr is not None else ''),
                          ('total_pages', info['total_pages']),
                          ('header_minus_unique', (n_hdr - len(uniq)) if n_hdr is not None else ''),
                          ('walk_complete', int(info['done']))):
            yield {'kind': f'summary_p{pass_no}', 'listing': info['listing'], 'value': val,
                   'detail': name}
        seen = {}
        for k, pg, pos in info['slots']:
            if k in seen:
                yield {'kind': f'dup_slot_p{pass_no}', 'listing': info['listing'], 'url_key': k,
                       'page_p1': seen[k][0], 'pos_p1': seen[k][1], 'page_p2': pg,
                       'pos_p2': pos, 'detail': 'same href in two slots of one pass'}
            else:
                seen[k] = (pg, pos)
    if '1' in passes and '2' in passes:
        first = {}
        for k, pg, pos in passes['1']['slots']:
            first.setdefault(k, (pg, pos))
        second = {}
        for k, pg, pos in passes['2']['slots']:
            second.setdefault(k, (pg, pos))
        for k in sorted(set(first) - set(second)):
            yield {'kind': 'only_in_pass1', 'listing': 'remarks', 'url_key': k,
                   'page_p1': first[k][0], 'pos_p1': first[k][1]}
        for k in sorted(set(second) - set(first)):
            yield {'kind': 'only_in_pass2', 'listing': 'remarks', 'url_key': k,
                   'page_p2': second[k][0], 'pos_p2': second[k][1]}
        moved = sum(1 for k in set(first) & set(second) if first[k][0] != second[k][0])
        yield {'kind': 'summary_diff', 'listing': 'remarks', 'value': moved,
               'detail': 'items on a different page in pass 2'}


# ---------------------------------------------------------------------------
# Candidates, corpus, classification
# ---------------------------------------------------------------------------

_RUBIO_SLUG_RE = re.compile(r'rubio|joint-statement|secretary-of-state-marco')
_SPOKEN_LIST_TYPES = set(_SPOKEN_TYPES) | {'op-ed', 'joint statement', 'statement'}


def build_candidates(state: dict) -> dict:
    """Union of E2/E3/E4 keyed by url_key, first spelling kept."""
    cands: dict[str, dict] = {}

    def add(url, bit, src, **extra):
        k = url_key(url)
        if not k:
            return
        c = cands.setdefault(k, {'url': url, 'url_key': k, 'source_set': 0, 'sources': [],
                                 'list_type': '', 'list_title': '', 'list_date': '',
                                 'lastmod': ''})
        c['source_set'] |= bit
        if src not in c['sources']:
            c['sources'].append(src)
        for f, v in extra.items():
            if v and not c.get(f):
                c[f] = v

    for url, meta in state['e2_urls'].items():
        add(url, SRC_E2, 'E2:' + _family(meta.get('sitemap', '')), lastmod=meta.get('lastmod', ''))
    for key, rec in state['listings'].items():
        stage, listing, _ = key.split(':')
        bit = SRC_E3 if stage == 'E3' else SRC_E4
        for p in rec['pages'].values():
            for it in p.get('items', []):
                add(it['href'], bit, f'{stage}:{listing}', list_type=it['type'],
                    list_title=it['title'], list_date=it['list_date'])
    for c in cands.values():
        c['slug_ym_key'] = slug_ym_key(c['url'])
    return cands


def in_probe_window(c: dict, month_floor=DEFAULT_URL_MONTH_FLOOR,
                    date_floor: date = DEFAULT_LASTMOD_FLOOR) -> bool:
    """False for listing items that are provably older than the Rubio era.

    E4 walks run a page or two past the floor before stopping; probing those
    pre-2025 releases would spend the request budget on certain out-of-scope
    pages. Undated items are always probed.
    """
    ym = url_year_month(c['url'])
    if ym is not None and ym < tuple(month_floor):
        return False
    iso = _iso_list_date(c.get('list_date', ''))
    if iso and iso < date_floor.isoformat():
        return False
    return True


def is_final_probe(row: dict) -> bool:
    """A probe that should not be retried: classified, or a definitive 404/410.

    Timeouts, 5xx, 429 and 403 (state.gov's intermittent bot wall; F-02)
    stay retryable so a resumed run tries them again -- but only for
    MAX_PROBE_RUNS runs, so one dead page cannot keep the crawl "incomplete"
    (and the prerelease unpublished) forever. It stays fetch_error in the
    outputs either way, for hand review.
    """
    if row.get('gt_class') != 'fetch_error':
        return True
    if int(row.get('probe_runs') or 1) >= MAX_PROBE_RUNS:
        return True
    return int(row.get('status') or 0) in (404, 410)


def probe_priority(c: dict) -> int:
    """Lower probes first: collection > Rubio-looking > dated releases > the rest."""
    if c['source_set'] & SRC_E3:
        return 0
    slug = url_slug(c['url'])
    if ('secretary:' in ' '.join(c['sources']) or _RUBIO_SLUG_RE.search(slug)
            or c.get('list_type', '').lower() in _SPOKEN_LIST_TYPES):
        return 1
    if url_year_month(c['url']) is not None:
        return 2
    return 3


def load_corpus_rubio(corpus: Path, secretary: str = 'Rubio') -> list[dict]:
    """Rubio rows (URL, Row Id, Date, Speech Type, Raw Text) for offline classification."""
    rows = []
    with open(corpus, newline='', encoding='utf-8') as f:
        rd = csv.DictReader(f)
        cols = set(rd.fieldnames or [])
        sec_col = 'Secretary' if 'Secretary' in cols else 'secretary'
        url_col = 'URL' if 'URL' in cols else 'url'
        rid_col = 'Row Id' if 'Row Id' in cols else 'row_id'
        date_col = 'Date' if 'Date' in cols else 'date'
        raw_col = 'Raw Text' if 'Raw Text' in cols else 'raw_text'
        for r in rd:
            if secretary.lower() not in (r.get(sec_col) or '').lower():
                continue
            rows.append({'url': (r.get(url_col) or '').strip(), 'row_id': r.get(rid_col, ''),
                         'date': (r.get(date_col) or '').strip(),
                         'raw_text': r.get(raw_col) or ''})
    return rows


def load_classified(path: Path | None) -> dict:
    """--classified CSV (url or url_key, gt_class, [in_scope, modality, event_date, ...])."""
    out: dict[str, dict] = {}
    if not path:
        return out
    if not Path(path).exists():
        print(f'{LOG} WARNING: --classified {path} not found; ignoring', file=sys.stderr)
        return out
    with open(path, newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            k = url_key(r.get('url') or r.get('canonical_url') or '') or (r.get('url_key') or '').strip()
            if not k or not (r.get('gt_class') or '').strip():
                continue
            cls = r['gt_class'].strip()
            r.setdefault('modality', IN_SCOPE_CLASSES.get(cls, ''))
            if (r.get('in_scope') or '') == '':
                r['in_scope'] = 1 if cls in IN_SCOPE_CLASSES else 0
            out[k] = r
    return out


def run_e6(fetcher: PoliteFetcher, state: dict, cands: dict, *, max_probes: int,
           scope_floor: date) -> int:
    # Translation pages are T1's (classified by their English original link).
    todo = [c for c in cands.values() if c['probe_status'] == 'pending'
            and not translation_lang(c['url'])]
    # English originals that translations link to come first within their
    # priority: the 2026-10-22 T1 run showed undated flat pages such as
    # /responding-to-panama-earthquake/ (a Rubio press statement) hide there.
    linked = {r.get('original_key') for r in state.get('translations', {}).values()
              if r.get('original_key')}
    todo.sort(key=lambda c: (c['probe_priority'], c['url_key'] not in linked,
                             tuple(-x for x in (url_year_month(c['url']) or (0, 0))),
                             c['url_key']))
    n = 0
    print(f'{LOG} E6: {len(todo)} URL(s) to probe, {fetcher.remaining} request(s) left',
          file=sys.stderr)
    for c in todo:
        if max_probes and n >= max_probes:
            break
        prev = state['probes'].get(c['url_key'], {})
        row = probe_page(fetcher, c['url'], scope_floor)
        row['probe_runs'] = int(prev.get('probe_runs') or 0) + 1
        if row['gt_class'] == 'fetch_error':
            state['errors'].append({'stage': 'E6', 'url': c['url'], 'error': row['error']})
        state['probes'][c['url_key']] = row
        c['probe_status'] = 'probed' if is_final_probe(row) else 'fetch_error'
        n += 1
        if n % 25 == 0:
            print(f'  [E6 {n}/{len(todo)}]', file=sys.stderr)
    return n


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _git_sha() -> str:
    sha = os.environ.get('GITHUB_SHA', '')
    if sha:
        return sha
    try:
        return subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True,
                              cwd=Path(__file__).parent, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return ''


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description='Read-only ground-truth crawl of state.gov for Rubio items (plan step 7).')
    ap.add_argument('--stages', default=','.join(ALL_STAGES),
                    help='Comma list of E1,E2,E3,E4,E6 (default: all)')
    ap.add_argument('--corpus', type=Path,
                    help='Corpus CSV (latest release). Needed for in_corpus, E6 and ground_truth.csv')
    ap.add_argument('--classified', type=Path,
                    help='CSV of already-classified URLs (url,gt_class[,in_scope,...]); skipped by E6')
    ap.add_argument('--out-dir', type=Path,
                    help='Output directory (default: rubio_gt_<UTC date>)')
    ap.add_argument('--state-in', type=Path, help='gt_state.json from a previous run to resume')
    ap.add_argument('--state-out', type=Path, help='Where to write state (default: <out-dir>/gt_state.json)')
    ap.add_argument('--max-requests', type=int, default=1500, help='Hard HTTP request cap (default 1500)')
    ap.add_argument('--max-minutes', type=float, default=0,
                    help='Stop cleanly after this many minutes (0 = no limit)')
    ap.add_argument('--min-interval', type=float, default=2.0, help='Seconds between requests (default 2)')
    ap.add_argument('--jitter', type=float, default=1.0,
                    help='Extra deterministic per-URL delay in [0, JITTER) seconds (default 1)')
    ap.add_argument('--run-id', default='', help='Run id for the E3 cache-buster (default: $GITHUB_RUN_ID or time)')
    ap.add_argument('--e3-max-pages', type=int, default=0, help='Cap E3 pages per pass (0 = all)')
    ap.add_argument('--e4-max-pages', type=int, default=0, help='Cap E4 pages per listing (0 = until floor)')
    ap.add_argument('--max-probes', type=int, default=0, help='Cap E6 probes this run (0 = until request cap)')
    ap.add_argument('--scope-floor', default=DEFAULT_SCOPE_FLOOR.isoformat(),
                    help='Event-date floor for in_scope and the E4 stop (default 2025-01-21)')
    ap.add_argument('--offline-fixtures', type=Path,
                    help='Serve requests from DIR/manifest.json instead of the network (dry runs, tests)')
    ap.add_argument('--sample-urls', type=Path,
                    help='Fetch only the URLs in this file (one per line) with the polite fetcher, '
                         'save each page\'s full HTML under <out-dir>/samples/, and exit. '
                         'For inspecting page markup the crawl cannot see from here.')
    return ap.parse_args(argv)


def sample_pages(fetcher: 'PoliteFetcher', urls: list[str], out_dir: Path) -> list[dict]:
    """Fetch each URL once and save its HTML; returns an index of what was saved."""
    sdir = out_dir / 'samples'
    sdir.mkdir(parents=True, exist_ok=True)
    index = []
    for i, url in enumerate(urls, 1):
        try:
            res = fetcher.get(url)
        except BudgetExhausted as e:
            print(f'{LOG} sample: {e}; stopping', file=sys.stderr)
            break
        name = f'{i:03d}.html'
        body = getattr(res.resp, 'text', '') or ''
        if body:
            (sdir / name).write_text(body, encoding='utf-8')
        index.append({'n': i, 'url': url, 'status': res.status, 'error': res.error,
                      'final_url': str(getattr(res.resp, 'url', '') or ''),
                      'file': name if body else '', 'bytes': len(body)})
        print(f'{LOG} sample {i}/{len(urls)} {res.status} {url}', file=sys.stderr)
    _write_csv(sdir / 'index.csv', ['n', 'url', 'status', 'error', 'final_url', 'file', 'bytes'],
               index)
    return index


def main(argv=None) -> int:
    args = parse_args(argv)
    stages = [s.strip().upper() for s in args.stages.split(',') if s.strip()]
    bad = [s for s in stages if s not in ALL_STAGES]
    if bad:
        print(f'{LOG} ERROR: unknown stage(s) {bad}; choose from {ALL_STAGES}', file=sys.stderr)
        return 2
    scope_floor = date.fromisoformat(args.scope_floor)
    today = _today()
    out_dir = args.out_dir or Path(f'rubio_gt_{today}')
    out_dir.mkdir(parents=True, exist_ok=True)
    state_out = args.state_out or (out_dir / 'gt_state.json')
    run_id = args.run_id or os.environ.get('GITHUB_RUN_ID', '') or str(int(time.time()))
    if 'E6' in stages and not args.corpus and not args.sample_urls:
        print(f'{LOG} ERROR: E6 needs --corpus (the diff set is "minus corpus keys")',
              file=sys.stderr)
        return 2

    state = load_state(args.state_in)
    started = _now_iso()
    state['runs'].append({'run_id': run_id, 'started': started, 'stages': stages})

    if args.offline_fixtures:
        session = FixtureSession(args.offline_fixtures)
        sleep = lambda s: None  # noqa: E731 -- fixtures need no politeness
    else:
        session = requests.Session()
        sleep = time.sleep
    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes else None
    fetcher = PoliteFetcher(session, max_requests=args.max_requests,
                            min_interval=args.min_interval, jitter=args.jitter,
                            deadline=deadline, sleep=sleep)

    if args.sample_urls:
        urls = [ln.strip() for ln in args.sample_urls.read_text(encoding='utf-8').splitlines()
                if ln.strip() and not ln.lstrip().startswith('#')]
        idx = sample_pages(fetcher, urls, out_dir)
        ok = sum(1 for r in idx if r['status'] == 200)
        print(f'{LOG} sampled {ok}/{len(urls)} pages OK into {out_dir / "samples"}', file=sys.stderr)
        return 0 if idx else 1

    exhausted = ''
    fatal = []
    try:
        if 'E1' in stages or 'E2' in stages:
            try:
                stage_sitemaps(fetcher, state, do_e1='E1' in stages, do_e2='E2' in stages,
                               month_floor=DEFAULT_URL_MONTH_FLOOR,
                               lastmod_floor=DEFAULT_LASTMOD_FLOOR)
                n_idx = len(state['index'] or [])
                want = n_idx if 'E1' in stages else sum(
                    1 for c in state['index'] or [] if _family(c['loc']) in E2_FAMILIES)
                done = len(state['sitemaps'])
                for s in ('E1', 'E2'):
                    if s in stages:
                        state['stages'][s] = 'done' if done >= want else 'partial'
            except RuntimeError as e:
                if isinstance(e, BudgetExhausted):
                    raise
                fatal.append(f'E1/E2: {e}')
                print(f'{LOG} ERROR: {e}', file=sys.stderr)
        if 'E3' in stages:
            for p, buster in ((1, ''), (2, run_id)):
                walk_listing(fetcher, state, stage='E3', listing='remarks', index=REMARKS_INDEX,
                             pass_no=p, buster=buster, max_pages=args.e3_max_pages)
            state['stages']['E3'] = 'done' if all(
                state['listings'].get(f'E3:remarks:{p}', {}).get('done') for p in (1, 2)) else 'partial'
            if not state['listings'].get('E3:remarks:1', {}).get('pages'):
                fatal.append('E3: remarks index page 1 unreadable')
        if 'E4' in stages:
            for name, index in E4_LISTINGS:
                walk_listing(fetcher, state, stage='E4', listing=name, index=index,
                             max_pages=args.e4_max_pages, stop_before=scope_floor)
            state['stages']['E4'] = 'done' if all(
                state['listings'].get(f'E4:{n}:1', {}).get('done') for n, _ in E4_LISTINGS) else 'partial'
    except BudgetExhausted as e:
        exhausted = str(e)

    # Candidates and corpus membership are rebuilt from cumulative state every run.
    cands = build_candidates(state)
    classified = load_classified(args.classified)
    corpus_rows: list[dict] = []
    idx = None
    if args.corpus:
        idx = load_existing_urls(args.corpus)
        corpus_rows = load_corpus_rubio(args.corpus)
    for c in cands.values():
        state['first_seen'].setdefault(c['url_key'], today)
        c['first_seen'] = state['first_seen'][c['url_key']]
        match = ''
        if idx is not None:
            if c['url_key'] in idx.url_keys or canonical_url_key(c['url']) in idx.url_keys:
                match = 'url_key'
            elif c['slug_ym_key'] and c['slug_ym_key'] in idx.slug_ym:
                match = 'slug_ym'
        c['in_corpus'] = (1 if match else 0) if idx is not None else ''
        c['corpus_match'] = match
        c['classified'] = int(c['url_key'] in classified)
        if c['classified']:
            c['source_set'] |= SRC_CLASSIFIED
        c['probe_priority'] = probe_priority(c)
        if match:
            c['probe_status'] = 'skipped_corpus'
        elif c['classified']:
            c['probe_status'] = 'skipped_classified'
        elif c['url_key'] in state['probes'] and is_final_probe(state['probes'][c['url_key']]):
            c['probe_status'] = 'probed'
        elif not in_probe_window(c):
            c['probe_status'] = 'skipped_window'
        else:
            c['probe_status'] = 'pending'

    n_probed = 0
    if 'E6' in stages and not exhausted:
        try:
            n_probed = run_e6(fetcher, state, cands, max_probes=args.max_probes,
                              scope_floor=scope_floor)
        except BudgetExhausted as e:
            exhausted = str(e)
        pending = sum(1 for c in cands.values() if c['probe_status'] in ('pending', 'fetch_error')
                      and not translation_lang(c['url']))
        state['stages']['E6'] = 'done' if not pending else 'partial'

    if 'T1' in stages and not exhausted:
        # An original is "known" when it is a corpus row or any sitemap URL.
        known = {url_key(u) for u in state['e2_urls']}
        if idx is not None:
            known |= set(idx.url_keys)
        t_left = 1
        try:
            _, t_left = run_t1(fetcher, state, known)
        except BudgetExhausted as e:
            exhausted = str(e)
        state['stages']['T1'] = 'done' if not t_left and not exhausted else 'partial'

    state['requests_total'] += fetcher.used
    state['runs'][-1].update({'finished': _now_iso(), 'requests': fetcher.used,
                              'budget_exhausted': exhausted})
    save_state(state, state_out)

    # ── Outputs ──────────────────────────────────────────────────────────
    _write_csv(out_dir / 'sitemap_inventory.csv', INVENTORY_COLS,
               sorted(state['sitemaps'].values(), key=lambda r: r['child_loc']))
    _write_csv(out_dir / 'listing_snapshot.csv', LISTING_COLS, listing_rows(state))
    cand_rows = []
    for c in sorted(cands.values(), key=lambda c: c['url_key']):
        cand_rows.append({**c, 'sources': '|'.join(c['sources'])})
    _write_csv(out_dir / 'candidates.csv', CANDIDATE_COLS, cand_rows)
    _write_csv(out_dir / 'probe.csv', PROBE_COLS,
               sorted(state['probes'].values(), key=lambda r: r['url_key']))
    _write_csv(out_dir / 'drift_report.csv', DRIFT_COLS, drift_rows(state))
    if state.get('translations'):
        trows = sorted(state['translations'].values(), key=lambda r: (r['lang'], r['url_key']))
        _write_csv(out_dir / 'translations.csv', TRANSLATION_COLS, trows)
        _write_csv(out_dir / 'translations_unique.csv', TRANSLATION_COLS,
                   [r for r in trows if r['t_class'] == 'translation_unique'])

    # ground_truth: corpus Rubio rows (classified offline from Raw Text),
    # --classified rows, and probed rows. One row per url_key.
    gt: dict[str, dict] = {}
    slug_ym_to_key: dict[str, str] = {}
    for r in corpus_rows:
        k = url_key(r['url'])
        if not k:
            continue
        cl = classify_page(r['raw_text'], url=r['url'], status=200, scope_floor=scope_floor)
        state['first_seen'].setdefault(k, today)
        gt[k] = {'url_key': k, 'slug_date_key': slug_date_key(r['url'], r['date']),
                 'canonical_url': canonicalize_live_url(r['url']), 'gt_class': cl['gt_class'],
                 'modality': cl['modality'], 'event_date': cl['event_date'] or r['date'],
                 'source_set': SRC_CORPUS, 'in_scope': cl['in_scope'],
                 'first_seen': state['first_seen'][k], 'in_corpus': 1,
                 'corpus_row_id': r['row_id'], 'needs_review': cl['needs_review'],
                 'reason': cl['reason'], 'title': cl['title'], 'block_type': cl['block_type'],
                 'byline': cl['byline'], 'first_600': cl['first_600']}
        sk = slug_ym_key(r['url'], r['date'])
        if sk:
            slug_ym_to_key.setdefault(sk, k)
    for c in cands.values():
        k = c['url_key']
        if c['corpus_match']:
            tgt = k if k in gt else slug_ym_to_key.get(c['slug_ym_key'])
            if tgt in gt:
                gt[tgt]['source_set'] |= c['source_set']
            continue
        src = classified.get(k) or state['probes'].get(k)
        if src is None:
            continue
        pr = state['probes'].get(k, {})
        gt[k] = {'url_key': k, 'slug_date_key': src.get('slug_date_key', ''),
                 'canonical_url': canonicalize_live_url(c['url'].split('?', 1)[0]),
                 'gt_class': src.get('gt_class', ''), 'modality': src.get('modality', ''),
                 'event_date': src.get('event_date', ''), 'source_set': c['source_set'],
                 'in_scope': src.get('in_scope', ''), 'first_seen': c['first_seen'],
                 'in_corpus': 0 if idx is not None else '', 'corpus_row_id': '',
                 'needs_review': src.get('needs_review', ''), 'reason': src.get('reason', ''),
                 'title': pr.get('title', '') or c.get('list_title', ''),
                 'block_type': pr.get('block_type', ''), 'byline': pr.get('byline', ''),
                 'status': pr.get('status', ''), 'first_600': pr.get('first_600', '')}
    # Hand-classified items no crawl source listed this run (e.g. the
    # off-collection supplemental URLs) still belong in the ground truth.
    for k, src in classified.items():
        if k in gt or k in cands:
            continue
        in_c = (k in idx.url_keys) if idx is not None else ''
        state['first_seen'].setdefault(k, today)
        gt[k] = {'url_key': k, 'slug_date_key': src.get('slug_date_key', ''),
                 'canonical_url': src.get('url') or src.get('canonical_url') or k,
                 'gt_class': src.get('gt_class', ''), 'modality': src.get('modality', ''),
                 'event_date': src.get('event_date', ''), 'source_set': SRC_CLASSIFIED,
                 'in_scope': src.get('in_scope', ''),
                 'first_seen': src.get('first_seen') or state['first_seen'][k],
                 'in_corpus': int(in_c) if in_c != '' else '', 'corpus_row_id': '',
                 'needs_review': src.get('needs_review', ''), 'reason': src.get('reason', 'classified CSV'),
                 'title': src.get('title', '')}
    gt_rows = sorted(gt.values(), key=lambda r: (r['event_date'] or '', r['url_key']))
    _write_csv(out_dir / 'ground_truth.csv', GT_COLS, gt_rows)
    diff = [r for r in gt_rows if not r['in_corpus'] and str(r['in_scope']) in ('1', '')]
    _write_csv(out_dir / 'gt_diff.csv', DIFF_COLS, diff)
    save_state(state, state_out)

    class_counts: dict[str, int] = {}
    for r in gt_rows:
        class_counts[r['gt_class']] = class_counts.get(r['gt_class'], 0) + 1
    complete = (not exhausted and not fatal
                and all(state['stages'].get(s) == 'done' for s in stages))
    meta = {
        'run_id': run_id, 'started': started, 'finished': _now_iso(), 'git_sha': _git_sha(),
        'stages_requested': stages, 'stage_status': state['stages'], 'complete': complete,
        'budget_exhausted': exhausted, 'fatal': fatal,
        'requests_this_run': fetcher.used, 'retries_this_run': fetcher.retries,
        'requests_total': state['requests_total'], 'max_requests': args.max_requests,
        'status_counts': fetcher.status_counts, 'offline_fixtures': bool(args.offline_fixtures),
        'counts': {
            'sitemap_children': len(state['index'] or []), 'sitemaps_read': len(state['sitemaps']),
            'e2_urls': len(state['e2_urls']), 'candidates': len(cands),
            'in_corpus': sum(1 for c in cands.values() if c['in_corpus'] == 1),
            'probe_pending': sum(1 for c in cands.values() if c['probe_status'] in ('pending', 'fetch_error') and not translation_lang(c['url'])),
            'probed_this_run': n_probed, 'probed_total': len(state['probes']),
            'corpus_rubio_rows': len(corpus_rows), 'ground_truth_rows': len(gt_rows),
            'in_scope': sum(1 for r in gt_rows if str(r['in_scope']) == '1'),
            'gt_diff_rows': len(diff), 'gt_class': class_counts,
            'translations_probed': len(state.get('translations', {})),
            't_class': dict(sorted(Counter(r['t_class'] for r in
                                           state.get('translations', {}).values()).items())),
            'translations_original_unknown': sum(
                1 for r in state.get('translations', {}).values()
                if r['t_class'] == 'translation_of_english' and str(r['original_known']) == '0'),
        },
        'errors': state['errors'][-200:], 'n_errors': len(state['errors']),
        'args': {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()},
    }
    with open(out_dir / 'run_meta.json', 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=2)

    print(f'{LOG} requests this run {fetcher.used}/{args.max_requests} '
          f'(retries {fetcher.retries}); candidates {len(cands)}; probed {n_probed}; '
          f'ground truth {len(gt_rows)} rows, {meta["counts"]["in_scope"]} in scope, '
          f'{len(diff)} in gt_diff', file=sys.stderr)
    if exhausted:
        print(f'{LOG} WARNING: stopped early ({exhausted}). Resume with '
              f'--state-in {state_out}', file=sys.stderr)
    if fatal:
        print(f'{LOG} ERROR: {fatal}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
