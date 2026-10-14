#!/usr/bin/env python3
"""rubio_backfill.py — Standalone end-to-end backfill of Rubio speeches from www.state.gov.

Discovers new URLs via state.gov XML sitemaps (optionally supplemented by a provided URL
list), fetches each page live, extracts content, and writes a CSV matching the 17-column
schema of secretaryofstate_speeches_final.fused.v44.patched.enriched.csv.

Three objectives enforced for every row:
  speech_text  — Secretary Rubio's utterances only, no boilerplate
  speaker_text — all natural speakers without boilerplate (full transcript/exchange)
  raw_text     — full page plain text from live fetch

Usage:
    python3 rubio_backfill.py [options]

Options:
    --enriched PATH      Existing enriched CSV (default: secretaryofstate_speeches_final.fused.v44.patched.enriched.csv)
    --output   PATH      Output CSV path (default: rubio_backfill_YYYY-MM-DD.csv)
    --urls     FILE      Optional plaintext file with one supplemental URL per line
    --rate-limit SECS    Seconds between requests (default: 1.0)
    --dry-run            Discover + count URLs only; do not fetch or write output
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit
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
    _run_extraction,
)
from speech_extract import BASE_DIR

try:
    import requests
    from bs4 import BeautifulSoup
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
# Sitemaps are a secondary source: they only surface URLs containing 'rubio',
# and the remarks archive lists those too ("0 from sitemaps" on 2026-10-12).
# In incremental runs they get one attempt each and a shared time budget;
# --full-rescan keeps full retries.
_SITEMAP_BUDGET_MIN = 5.0


class DiscoveryError(RuntimeError):
    """A discovery source could not be read, so "nothing new" is unprovable."""


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

# Sitemap XML namespace
_SITEMAP_NS = '{http://www.sitemaps.org/schemas/sitemap/0.9}'

# State.gov nav boilerplate signals — appear at the start of extracted text when
# extract_secretary_only_text falls back to raw content with no speaker labels found.
_NAV_BOILERPLATE_SIGNALS = (
    'and Resources\nAbout\n',
    'About\nBack\nAbout\n',
    'History\nPrevious Administration Archives\n',
    'Secretary of State\nHome\n',
    'Foreign Relations of the United States\nNational Museum',
)

# Speaker label that must be stripped from the front of speech_text when present.
_SPEAKER_LABEL_RE = re.compile(
    r'^SECRETARY(?:-DESIGNATE)? RUBIO:\s*\n', re.IGNORECASE
)

# Signals that speech_text starts with a reporter question, not Rubio's words.
_QUESTION_START_RE = re.compile(r'^QUESTION:\s*\n', re.IGNORECASE)

# Used by _collect_rubio_turns: any uppercase speaker label on its own line.
_ANY_UPPER_SPEAKER_RE = re.compile(r'(?m)^\s*[A-Z][A-Z .\'\-]{2,40}:\s*$')
# Specifically a SECRETARY RUBIO (or DESIGNATE / OF STATE) label.
_RUBIO_TURN_RE = re.compile(
    r'(?im)^\s*(?:THE\s+)?SECRETARY(?:-DESIGNATE)?(?:\s+OF\s+STATE)?\s+'
    r'(?:[A-Z.\']+\s+)*RUBIO:\s*$'
)

# CSV column order must match the enriched CSV exactly
_FIELDNAMES = [
    'speech_type', 'Location', 'Date', 'secretary', 'Title', 'speech_text',
    'row_id', 'url', 'url_match_score', 'url_match_method', 'url_match_status',
    'match_pass', 'cleaning_profile_used', 'pass3_stage', 'pass3_profile_used',
    'raw_text', 'speaker_text', 'extraction_mode',
]

# Third-person attribution patterns: page is ABOUT Rubio, not BY Rubio.
# Applied as a secondary content check after extraction (catches /releases/ edge cases).
_THIRD_PERSON_OPENINGS = (
    'The below is attributable to Spokesperson',
    'Secretary of State Marco Rubio spoke',
    'Secretary of State Marco Rubio met',
    'Secretary of State Marco Rubio and',
    'Secretary of State Marco Rubio will',
    'Secretary Rubio spoke',
    'Secretary Rubio met',
    'Secretary Rubio will',
    'Secretary Rubio and',
    'Today, Secretary of State Marco Rubio delivered',
)


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

def _is_excluded(url: str) -> bool:
    """True for URLs that are never speech pages (binaries, translations, queries)."""
    low = url.lower()
    if '?' in low:
        return True
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

def _parse_lastmod(text: str) -> date | None:
    """Parse ISO lastmod string to date; return None on failure."""
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def discover_sitemap_urls(session: requests.Session, rate_limit: float,
                          since: date = RUBIO_START, *,
                          attempts: int = _FETCH_ATTEMPTS,
                          budget_min: float | None = None) -> list[str]:
    """Crawl state.gov sitemaps; return Rubio speech candidate URLs.

    `since` filters on the sitemap's own <lastmod>, which is the page EDIT
    date, not the speech date -- so it is only a discovery hint. An entry
    with no parseable lastmod is always kept.

    Best-effort: `attempts` is per sitemap, and once `budget_min` minutes have
    passed the remaining sitemaps are skipped. Whatever is missed is re-covered
    by the --since buffer on the next run.
    """
    candidates: list[str] = []
    deadline = time.monotonic() + budget_min * 60 if budget_min else None

    print(f'[rubio_backfill] Fetching sitemap index: {_SITEMAP_INDEX}')
    try:
        resp = _get_with_retry(session, _SITEMAP_INDEX, attempts=attempts, label='sitemap index')
    except Exception as e:
        print(f'[rubio_backfill] WARNING: Could not fetch sitemap index: {e}', file=sys.stderr)
        return candidates

    try:
        root = ElementTree.fromstring(resp.text)
    except ElementTree.ParseError as e:
        print(f'[rubio_backfill] WARNING: Could not parse sitemap index XML: {e}', file=sys.stderr)
        return candidates

    # Collect page-sitemap*.xml entries
    page_sitemaps: list[str] = []
    for sitemap in root.iter(f'{_SITEMAP_NS}sitemap'):
        loc = (sitemap.findtext(f'{_SITEMAP_NS}loc') or '').strip()
        if 'page-sitemap' in loc:
            page_sitemaps.append(loc)

    print(f'[rubio_backfill] Found {len(page_sitemaps)} page sitemaps to scan')

    for i, sitemap_url in enumerate(page_sitemaps, 1):
        if deadline is not None and time.monotonic() > deadline:
            print(f'[rubio_backfill] WARNING: sitemap budget ({budget_min:g} min) spent -- '
                  f'skipping the remaining {len(page_sitemaps) - i + 1} sitemap(s)',
                  file=sys.stderr)
            break
        time.sleep(rate_limit)
        try:
            resp = _get_with_retry(session, sitemap_url, attempts=attempts, label=sitemap_url)
            resp.raise_for_status()
            root = ElementTree.fromstring(resp.text)
        except Exception as e:
            print(f'[rubio_backfill] WARNING: skipping sitemap {sitemap_url}: {e}',
                  file=sys.stderr)
            continue

        found_in_this = 0
        for url_el in root.iter(f'{_SITEMAP_NS}url'):
            loc = (url_el.findtext(f'{_SITEMAP_NS}loc') or '').strip()
            lastmod_text = (url_el.findtext(f'{_SITEMAP_NS}lastmod') or '').strip()
            if not loc:
                continue
            lastmod = _parse_lastmod(lastmod_text)
            if lastmod and lastmod < since:
                continue
            if _is_excluded(loc):
                continue
            if _is_rubio_speech_url(loc):
                candidates.append(loc)
                found_in_this += 1

        if found_in_this:
            print(f'  [{i}/{len(page_sitemaps)}] {sitemap_url} → {found_in_this} candidates')

    return candidates


def discover_remarks_urls(session: requests.Session, rate_limit: float,
                          since: date = RUBIO_START) -> list[str]:
    """Scrape paginated remarks archive; return article content URLs.

    Two-hop discovery:
      1. Listing pages (/remarks-secretary-rubio/page/N/) — scanned but not kept
      2. Content pages (from .collection-result > a on each listing page) — returned

    These URLs live under /releases/office-of-the-spokesperson/ and don't contain
    'rubio', so they're invisible to the sitemap filter.
    """
    candidates: list[str] = []

    print(f'[rubio_backfill] Fetching remarks index: {_REMARKS_INDEX}')
    try:
        resp = _get_with_retry(session, _REMARKS_INDEX, label='remarks index')
        soup = _make_soup(resp.text)
    except Exception as e:
        # NOT a warning. This index is the ONLY discovery path for
        # current-era speeches: _is_rubio_speech_url requires 'rubio' in the
        # URL, and the /releases/office-of-the-spokes*/YYYY/MM/<slug>/ pages
        # frequently do not contain it ('togo-independence-day'). Returning
        # an empty list here is indistinguishable from "no speeches this
        # week", which is exactly how the 2026-10-12 run reported success
        # while silently skipping a week of speeches.
        raise DiscoveryError(
            f'remarks index {_REMARKS_INDEX} unreachable after '
            f'{_FETCH_ATTEMPTS} attempts: {e}'
        ) from e

    # Determine last page number from pagination links
    max_page = 1
    for a in soup.find_all('a', href=True):
        m = re.search(r'/remarks-secretary-rubio/page/(\d+)/', a['href'])
        if m:
            max_page = max(max_page, int(m.group(1)))

    print(f'[rubio_backfill] Remarks archive: {max_page} pages to scan')

    # Month floor. The listing only exposes a link's date through the
    # /YYYY/MM/ segment of its URL, so comparison is at month granularity --
    # a day-precise test here would drop same-month links we still want. The
    # real date filter runs after fetching, where the actual speech date is
    # known.
    floor_ym = (since.year, since.month)

    def _extract_page_links(s) -> tuple[int, int, tuple | None]:
        """Returns (kept, seen, oldest (year, month) seen on this page)."""
        kept = seen = 0
        oldest: tuple | None = None
        for el in s.find_all(class_='collection-result'):
            a = el.find('a', href=True)
            if not a:
                continue
            href = a['href'].strip()
            if not href or _is_excluded(href):
                continue
            seen += 1
            ym = url_year_month(href)
            if ym is not None and (oldest is None or ym < oldest):
                oldest = ym
            # A link with no parseable month is ALWAYS kept -- the legacy
            # flat-slug form has no date in it, and dropping those silently
            # would be data loss. The post-fetch date gate handles them.
            if ym is None or ym >= floor_ym:
                candidates.append(href)
                kept += 1
        return kept, seen, oldest

    # Page 1 already fetched
    _extract_page_links(soup)

    # Pages 2..max_page, stopping once we have walked past the floor.
    #
    # The archive is expected to be reverse-chronological, but that could NOT
    # be verified from the development network (state.gov 403s every
    # non-browser client), so the stop is deliberately conservative:
    #   * it needs TWO consecutive pages that are both entirely older than the
    #     floor and contributed nothing, so a single pinned/featured item or
    #     one out-of-order insert cannot truncate the walk;
    #   * a page whose links expose no parseable month never counts toward the
    #     stop, so if the listing markup changes we degrade to the old full
    #     walk rather than silently truncating.
    stale_streak = 0
    stopped_at = None
    undated_pages = 0
    failed_pages: list[int] = []
    for pg in range(2, max_page + 1):
        time.sleep(rate_limit)
        url = f'{_REMARKS_INDEX}page/{pg}/'
        try:
            resp = _get_with_retry(session, url, label=f'remarks page {pg}')
            pg_soup = _make_soup(resp.text)
        except Exception as e:
            print(f'[rubio_backfill] WARNING: skipping remarks page {pg}: {e}', file=sys.stderr)
            failed_pages.append(pg)
            # An unreadable page neither counts toward the stop nor resets it.
            # The stop already assumes the archive is reverse-chronological, and
            # under that assumption a page lying between two pages that are
            # entirely older than the floor cannot hold anything newer. Resetting
            # here instead meant every failure bought two more pages of walking:
            # the 2026-10-12 run went to page 19 (vs 9 on a good day), at ~3
            # minutes per failed page.
            continue

        kept, seen, oldest = _extract_page_links(pg_soup)
        if seen and oldest is None:
            undated_pages += 1
            print(f'[rubio_backfill] WARNING: remarks page {pg} had {seen} link(s) but no '
                  f'parseable date in any of them -- not counting it toward early-stop.',
                  file=sys.stderr)

        if oldest is not None and oldest < floor_ym and kept == 0:
            stale_streak += 1
            if stale_streak >= 2:
                stopped_at = pg
                print(f'[rubio_backfill] remarks early-stop after page {pg}/{max_page} '
                      f'(floor {since:%Y-%m}; page oldest {oldest[0]:04d}-{oldest[1]:02d})')
                break
        else:
            stale_streak = 0

        if pg % 10 == 0 or pg == max_page:
            print(f'  [remarks {pg}/{max_page}] {len(candidates)} candidates so far')

    if stopped_at is None and max_page > 1 and floor_ym > (RUBIO_START.year, RUBIO_START.month):
        # Canary: the walk was supposed to be scoped but never stopped. The
        # pipeline still works, just slowly -- surface it rather than hide it.
        print(f'[rubio_backfill] WARNING: early-stop never fired -- walked all {max_page} '
              f'remarks pages despite a {since:%Y-%m} floor. Is the archive still '
              f'reverse-chronological?', file=sys.stderr)
    if undated_pages:
        print(f'[rubio_backfill] WARNING: {undated_pages} remarks page(s) exposed no dates.',
              file=sys.stderr)
    if failed_pages:
        # Page 1 holds the newest items, so losing it is unrecoverable for
        # this run; a deeper page is older and will be re-covered by the
        # --since buffer on the next run.
        print(f'[rubio_backfill] WARNING: {len(failed_pages)} remarks page(s) unreadable: '
              f'{failed_pages[:10]}', file=sys.stderr)
        if not candidates:
            raise DiscoveryError(
                f'remarks archive yielded no candidates and {len(failed_pages)} page(s) '
                f'were unreadable ({failed_pages[:10]}) -- cannot distinguish '
                f'"nothing new" from "crawl failed".'
            )

    return candidates


def collect_candidate_urls(
    session: requests.Session,
    rate_limit: float,
    supplemental_file: Path | None,
    since: date = RUBIO_START,
    *,
    full_rescan: bool = False,
    sitemap_budget_min: float | None = _SITEMAP_BUDGET_MIN,
) -> list[str]:
    """Sitemap + remarks pages + optional supplemental list → deduplicated candidates.

    Raises DiscoveryError when a source could not be read, rather than
    returning a short list. "Nothing new this week" and "the crawl failed"
    look identical downstream, and conflating them is how the 2026-10-12 run
    reported success while skipping a week of speeches.
    """
    if full_rescan:
        candidates = discover_sitemap_urls(session, rate_limit, since)
    else:
        candidates = discover_sitemap_urls(session, rate_limit, since, attempts=1,
                                           budget_min=sitemap_budget_min)
    n_sitemap = len(candidates)
    # discover_remarks_urls raises DiscoveryError if its index is unreachable
    # -- that index is the only path to current-era URLs, so a failure there
    # is fatal, not degraded.
    candidates += discover_remarks_urls(session, rate_limit, since)
    print(f'[rubio_backfill] discovery: {n_sitemap} from sitemaps, '
          f'{len(candidates) - n_sitemap} from the remarks archive')

    if supplemental_file:
        with open(supplemental_file, encoding='utf-8') as f:
            for line in f:
                url = line.strip()
                if url and not url.startswith('#'):
                    candidates.append(url)
        print(f'[rubio_backfill] Added supplemental URLs from {supplemental_file.name}')

    # Deduplicate within candidates (preserve first occurrence order)
    seen: set[str] = set()
    unique: list[str] = []
    for url in candidates:
        key = canonical_url_key(url)
        if key and key not in seen:
            seen.add(key)
            unique.append(url)

    return unique


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


def fetch_rubio_page(url: str, session: requests.Session) -> dict | None:
    """Fetch a www.state.gov page; return metadata + raw_text dict, or None on error."""
    try:
        resp = session.get(url, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f'  [fetch] ERROR {url}: {e}', file=sys.stderr)
        return None

    try:
        soup = _make_soup(resp.text)
    except Exception as e:
        print(f'  [fetch] Parse ERROR {url}: {e}', file=sys.stderr)
        return None

    raw_text = soup.get_text('\n', strip=True)

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

    return {
        'raw_text': raw_text,
        'jsonld_date_published': str(ld.get('datePublished', '')),
        'jsonld_location_name': jsonld_location,
        'article_meta_doctype': str(ld.get('@type', '')),
        'title': title,
    }


# ---------------------------------------------------------------------------
# Step 3c: Metadata extraction from page block
# ---------------------------------------------------------------------------

def _parse_block_metadata(raw: str) -> dict:
    """Extract speech_type, location, date from the hide→Tags content block.

    Block structure (after the hide anchor):
        {Title}
        {Speech type}                ← e.g. "Press Statement", "Remarks"
        {Name}, Secretary of State
        {Location}                   ← optional
        {Date}                       ← standalone Month DD, YYYY line
        {speech content}
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

    # Speech type: 2nd meta line (index 1), after title (index 0)
    if len(meta_lines) >= 2:
        result['block_speech_type'] = meta_lines[1]

    # Location: line just before the date, if it doesn't name the Secretary
    if date_idx >= 1:
        candidate = meta_lines[-1] if meta_lines else ''
        if candidate and 'Secretary of State' not in candidate and 'Rubio' not in candidate:
            result['block_location'] = candidate

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
    'Office of the Spokesperson': 'Washington, D.C.',
    'Aboard Air Force One': 'Washington, D.C.',
}


def _resolve_location(page_meta: dict, block_meta: dict) -> str:
    """Resolve Location from JSON-LD, block, or default — normalized to corpus conventions."""
    loc = page_meta.get('jsonld_location_name', '').strip()
    if not loc:
        loc = block_meta.get('block_location', '').strip()
    return _LOC_NORMALIZE.get(loc, loc) or 'Washington, D.C.'


# ---------------------------------------------------------------------------
# Step 3b + 3d: Extract + validate
# ---------------------------------------------------------------------------

def _collect_rubio_turns(speaker_text: str) -> str:
    """Concatenate every SECRETARY RUBIO: turn in a multi-speaker transcript.

    Handles cases like swearing-in where VICE PRESIDENT VANCE: interjections
    appear between Rubio's turns, causing extract_secretary_only_text to stop
    after only the first sentence.
    """
    boundaries = [
        (m.start(), m.end(), m.group().strip())
        for m in _ANY_UPPER_SPEAKER_RE.finditer(speaker_text)
    ]
    turns = []
    for i, (start, end, label) in enumerate(boundaries):
        if _RUBIO_TURN_RE.match(label):
            next_start = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(speaker_text)
            turn = speaker_text[end:next_start].strip()
            if len(turn) > 20:
                turns.append(turn)
    return '\n\n'.join(turns)


def _is_active_speaker(url: str, speech_text: str, extraction_mode: str = '') -> bool:
    """False for pages where Rubio is discussed but not actively speaking."""
    if url.rstrip('/').endswith('/remarks-secretary-rubio'):
        return False
    # Normalize whitespace and smart quotes before matching so \xa0, curly apostrophes,
    # and multiple spaces don't prevent pattern matches.
    sample = ' '.join(speech_text[:500].split())
    sample = sample.replace('\u2018', "'").replace('\u2019', "'")
    if any(sample.startswith(pat) for pat in _THIRD_PERSON_OPENINGS):
        return False
    return True


def _is_nav_boilerplate(text: str) -> bool:
    """True when extracted text starts with state.gov nav menu rather than speech content."""
    sample = text[:500]
    return any(sig in sample for sig in _NAV_BOILERPLATE_SIGNALS)


def _has_non_rubio_label(text: str) -> bool:
    """True if text contains any speaker label that is not a SECRETARY RUBIO label."""
    for m in _ANY_UPPER_SPEAKER_RE.finditer(text):
        if not _RUBIO_TURN_RE.match(m.group().strip()):
            return True
    return False


def extract_row(url: str, page: dict) -> dict | None:
    """Extract (speaker_text, speech_text, mode) and validate; return None to skip."""
    raw = page['raw_text']

    speaker_text, speech_text, mode = _run_extraction(raw, url, SECRETARY)
    extraction_mode = 'no_checkpoint+raw_populated'
    content_block: str = ''

    # --- Pass 1: content-block fallback ---
    # Fire when extraction returned empty OR nav boilerplate. Two failure modes of
    # extract_secretary_only_text when no speaker labels exist on the page:
    #   (a) returns empty string
    #   (b) falls back to raw nav text (non-empty, so the empty check alone misses it)
    needs_speech_fallback  = not speech_text.strip()  or _is_nav_boilerplate(speech_text)
    needs_speaker_fallback = not speaker_text.strip() or _is_nav_boilerplate(speaker_text)

    if needs_speech_fallback or needs_speaker_fallback:
        content_block = _extract_modern_state_content(raw)
        if len(content_block.strip()) >= 100:
            if needs_speech_fallback:
                speech_text = content_block
                extraction_mode = 'no_checkpoint+content_block_fallback'
            if needs_speaker_fallback:
                speaker_text = content_block

    # --- Pass 2: strip speaker label prefix from speech_text ---
    # extract_secretary_only_text sometimes leaves "SECRETARY RUBIO:\n" at the front.
    speech_text = _SPEAKER_LABEL_RE.sub('', speech_text, count=1)

    # --- Pass 3: reporter question as first content ---
    # For press briefing transcripts where a reporter speaks before the Secretary,
    # extraction may start at the wrong point. Skip forward to Rubio's first turn.
    # If no Rubio turn exists (briefing is by staff, not Secretary), use content block.
    if _QUESTION_START_RE.match(speech_text):
        m = re.search(
            r'(?:^|\n)SECRETARY(?:-DESIGNATE)? RUBIO:\s*\n',
            speech_text, re.IGNORECASE,
        )
        if m:
            speech_text = speech_text[m.end():]
        else:
            # No Rubio utterance on this page — briefing by staff about his travel.
            # Use content block so speech_text is at least boilerplate-free.
            if not content_block:
                content_block = _extract_modern_state_content(raw)
            if len(content_block.strip()) >= 100:
                speech_text = content_block
                extraction_mode = 'no_checkpoint+content_block_fallback'

    # --- Pass 4: truncation recovery ---
    # When speech_text is much shorter than speaker_text, extract_secretary_only_text
    # stopped early because UPPER_SPEAKER_LINE_RE matched an interjector (e.g.
    # "VICE PRESIDENT VANCE:") right after Rubio's first sentence.
    if (len(speech_text.strip()) < 500
            and len(speaker_text.strip()) > len(speech_text.strip()) * 3
            and extraction_mode == 'no_checkpoint+raw_populated'):
        # Try 1: re-run on nav-stripped speaker_text
        recovered, _ = extract_secretary_only_text(speaker_text, SECRETARY)
        recovered = _SPEAKER_LABEL_RE.sub('', recovered, count=1)
        if len(recovered.strip()) > len(speech_text.strip()):
            speech_text = recovered
            extraction_mode = 'no_checkpoint+raw_populated+truncation_recovery'
        else:
            # Try 2: multi-turn — collect ALL Rubio turns across interjecting speakers
            multi_turn = _collect_rubio_turns(speaker_text)
            if len(multi_turn.strip()) > len(speech_text.strip()):
                speech_text = multi_turn
                extraction_mode = 'no_checkpoint+raw_populated+multi_turn_recovery'

    # --- Pass 5: Tags sentinel truncation ---
    # state.gov pages end content with a "\nTags\n" section marker before the page footer.
    # Strip it and everything after to remove cookie dialogs and nav from both fields.
    _TAGS_SENTINEL = '\nTags\n'
    idx = speech_text.find(_TAGS_SENTINEL)
    if idx != -1:
        speech_text = speech_text[:idx].rstrip()
    idx = speaker_text.find(_TAGS_SENTINEL)
    if idx != -1:
        speaker_text = speaker_text[:idx].rstrip()

    # --- Pass 6: other-speaker contamination cleanup ---
    # If speech_text still contains non-Rubio speaker labels, extract_secretary_only_text
    # didn't isolate his turns (e.g., Q&A testimony where senators interject between answers).
    # Re-extract using _collect_rubio_turns() which respects ALL speaker boundaries.
    if _has_non_rubio_label(speech_text):
        clean = _collect_rubio_turns(speaker_text)
        if clean.strip():
            speech_text = clean
            extraction_mode += '+other_speaker_cleaned'

    # --- Validation ---
    if len(speech_text.strip()) < 100 and len(raw.strip()) < 200:
        print(f'  [skip] Too short — speech_text={len(speech_text)}, raw={len(raw)}: {url}',
              file=sys.stderr)
        return None

    return {
        'speaker_text':    speaker_text,
        'speech_text':     speech_text,
        'extraction_mode': extraction_mode,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def resolve_since(args: argparse.Namespace, index: "CorpusIndex") -> date:
    """Work out how far back discovery should reach.

    Order: explicit --since > (latest corpus date for this secretary minus
    --since-buffer-days) > RUBIO_START. --full-rescan forces RUBIO_START.
    RUBIO_START is the hard floor in every case.

    Why a buffer rather than resuming exactly at the latest stored date:
      1. state.gov publishes transcripts late -- a Thursday speech can appear
         the following Tuesday, after Monday's run has already moved past it.
      2. The corpus Date is the SPEECH date; the sitemap lastmod is the EDIT
         date. They diverge.
      3. One failed weekly run (403, rate limit, runner outage) must not leave
         a permanent hole. A 14-day buffer self-heals across a missed run; a
         zero buffer does not.
    At roughly 8-12 new URLs a week, the re-fetch cost of the buffer is
    trivial next to the risk of silent permanent data loss.
    """
    if getattr(args, 'full_rescan', False):
        return RUBIO_START
    if getattr(args, 'since', None):
        try:
            explicit = date.fromisoformat(args.since)
        except ValueError:
            raise SystemExit(f'ERROR: --since must be YYYY-MM-DD, got {args.since!r}')
        return max(explicit, RUBIO_START)
    if index.max_date is not None:
        buffer_days = int(getattr(args, 'since_buffer_days', 14) or 14)
        return max(index.max_date - timedelta(days=buffer_days), RUBIO_START)
    return RUBIO_START


def run(args: argparse.Namespace) -> None:
    enriched_path = Path(args.enriched)
    output_path = Path(args.output)
    supplemental = Path(args.urls) if args.urls else None
    rate_limit = float(args.rate_limit)
    dry_run = args.dry_run

    # Step 1: Load existing corpus
    print(f'[rubio_backfill] Loading existing corpus: {enriched_path.name}')
    index = load_existing_urls(enriched_path)
    max_row_id = index.max_row_id
    since = resolve_since(args, index)
    print(f'[rubio_backfill] {len(index.url_keys):,} existing URL keys, '
          f'{len(index.slug_ym):,} slug+month keys, max row_id={max_row_id}, '
          f'latest Rubio date={index.max_date}')

    # Step 2: Discover candidates
    session = requests.Session()
    print(f'[rubio_backfill] Discovery floor: {since} '
          f'({"full re-scan" if getattr(args, "full_rescan", False) else "incremental"})')
    try:
        all_candidates = collect_candidate_urls(
            session, rate_limit, supplemental, since,
            full_rescan=getattr(args, 'full_rescan', False),
            sitemap_budget_min=getattr(args, 'sitemap_budget_min', _SITEMAP_BUDGET_MIN))
    except DiscoveryError as e:
        # Exit non-zero so the workflow step FAILS and GitHub notifies, rather
        # than the job going green with "No new speeches found". The --since
        # buffer means the next successful run re-covers this window, so a
        # transient outage costs a delay, not data.
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
    new_urls = []
    skipped_url_key = skipped_slug_ym = 0
    for u in all_candidates:
        if canonical_url_key(u) in index.url_keys:
            skipped_url_key += 1
            continue
        k = slug_ym_key(u)
        if k and k in index.slug_ym:
            skipped_slug_ym += 1
            continue
        new_urls.append(u)
    print(f'[rubio_backfill] {skipped_url_key + skipped_slug_ym:,} already in corpus '
          f'(url key: {skipped_url_key:,}, slug+month: {skipped_slug_ym:,})')
    if skipped_slug_ym:
        print(f'[rubio_backfill] NOTE: {skipped_slug_ym:,} candidate(s) matched only on '
              f'slug+month, not on URL -- state.gov has likely renamed a path segment. '
              f'If this count is large, add the rename to PATH_RENAMES in corpus_keys.py.')
    print(f'[rubio_backfill] {len(new_urls):,} new URLs to fetch')

    if dry_run:
        print('[rubio_backfill] --dry-run: stopping before fetch.')
        for u in new_urls[:20]:
            print(f'  {u}')
        if len(new_urls) > 20:
            print(f'  ... and {len(new_urls) - 20} more')
        return

    if not new_urls:
        print('[rubio_backfill] Nothing new to fetch. Exiting.')
        return

    # Step 3–4: Fetch, extract, build rows
    rows_out: list[dict] = []
    skipped = 0
    failed = 0
    next_row_id = max_row_id + 1

    for i, url in enumerate(new_urls, 1):
        time.sleep(rate_limit)
        print(f'  [{i}/{len(new_urls)}] {url}')

        page = fetch_rubio_page(url, session)
        if page is None:
            failed += 1
            continue

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
                    print(f'  [skip] Date {resolved_date} before Rubio term: {url}',
                          file=sys.stderr)
                    skipped += 1
                    continue
            except ValueError:
                pass

        # Content extraction
        extracted = extract_row(url, page)
        if extracted is None:
            skipped += 1
            continue

        # Active-speaker gate: skip pages where Rubio is discussed but not speaking
        if not _is_active_speaker(normalize_url(url), extracted['speech_text'],
                                  extracted['extraction_mode']):
            print(f'  [skip] Not active speaker: {url}', file=sys.stderr)
            skipped += 1
            continue

        # Post-fetch dedup: now that the real speech date is known, apply the
        # full-strength (slug, Date) key. This catches a rename that ALSO
        # changed the URL's /YYYY/MM/ segment, which the pre-fetch slug+month
        # filter cannot see. Verified collision-free across all 17,617 corpus
        # rows, so a hit here is a genuine duplicate.
        sd_key = slug_date_key(url, resolved_date or '')
        if sd_key and sd_key in index.slug_date:
            print(f'  [skip] Already in corpus by (slug, date): {url}', file=sys.stderr)
            skipped += 1
            continue

        raw_title = (page.get('title') or '').strip()
        # Strip "- United States Department of State" suffix.
        title = re.sub(
            r'\s*[-|–]\s*(?:United\s+States\s+|U\.S\.\s+)?Department\s+of\s+State\s*$',
            '', raw_title, flags=re.IGNORECASE,
        ).strip()

        row = {
            'speech_type':  stype,
            'Location':     location,
            'Date':         resolved_date,
            'secretary':    SECRETARY,
            'Title':        title,
            'speech_text':  extracted['speech_text'],
            'row_id':       str(next_row_id),
            'url':          normalize_url(url),
            'raw_text':     page['raw_text'],
            'speaker_text': extracted['speaker_text'],
            'extraction_mode': extracted['extraction_mode'],
            **_PIPELINE_META,
        }
        rows_out.append(row)
        next_row_id += 1

        if i % 10 == 0:
            print(f'  ... {i}/{len(new_urls)} fetched, {len(rows_out)} written so far')

    # Step 5: Write output
    print(f'\n[rubio_backfill] Writing {len(rows_out):,} rows to {output_path.name}...')
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows_out)

    print(f'[rubio_backfill] Done.')
    print(f'  Rows written:  {len(rows_out):,}')
    print(f'  Skipped:       {skipped:,}  (too short / no speech content)')
    print(f'  Failed:        {failed:,}  (fetch/parse error)')

    # Quick quality check
    empty_speech = sum(1 for r in rows_out if not r['speech_text'].strip())
    empty_raw = sum(1 for r in rows_out if not r['raw_text'].strip())
    empty_spkr = sum(1 for r in rows_out if not r['speaker_text'].strip())
    dates = [r['Date'] for r in rows_out if r['Date']]
    print(f'\nQuality check:')
    print(f'  Empty speech_text:  {empty_speech}  (target: 0)')
    print(f'  Empty raw_text:     {empty_raw}  (target: 0)')
    print(f'  Empty speaker_text: {empty_spkr}  (target: ~0)')
    if dates:
        print(f'  Date range: {min(dates)} – {max(dates)}')


def main() -> None:
    default_output = BASE_DIR / f'rubio_backfill_{date.today().isoformat()}.csv'

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--enriched', default=str(_ENRICHED_DEFAULT),
                        help='Existing enriched CSV (default: %(default)s)')
    parser.add_argument('--output', default=str(default_output),
                        help='Output CSV path (default: %(default)s)')
    parser.add_argument('--urls', default=None,
                        help='Plaintext file of supplemental URLs (one per line)')
    parser.add_argument('--rate-limit', default=1.0, type=float,
                        help='Seconds between HTTP requests (default: %(default)s)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Discover URLs only; do not fetch or write output')
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--since', default=None, metavar='YYYY-MM-DD',
                       help='Only discover pages dated on or after this. Default: '
                            'the latest date already in --enriched for this secretary, '
                            'minus --since-buffer-days.')
    scope.add_argument('--full-rescan', action='store_true',
                       help='Ignore the incremental cutoff and walk the whole archive '
                            'back to the start of the term. Use after a normalization '
                            'change, or to re-add quarantined rows.')
    parser.add_argument('--since-buffer-days', type=int, default=14, metavar='N',
                        help='Days of slack below the latest stored date (default: '
                             '%(default)s), so a late-published transcript or one '
                             'missed weekly run does not leave a permanent hole.')
    parser.add_argument('--sitemap-budget-min', type=float, default=_SITEMAP_BUDGET_MIN, metavar='MIN',
                        help='Incremental runs only: stop scanning sitemaps after this many '
                             'minutes (default: %(default)s; 0 = no limit). Sitemaps are a '
                             'secondary source; the remarks archive is always walked in full.')
    parser.add_argument('--print-since', action='store_true',
                        help='Print the resolved discovery floor and exit. Used by CI to '
                             'compute the cutoff without duplicating the logic in YAML.')
    args = parser.parse_args()

    if args.print_since:
        index = load_existing_urls(Path(args.enriched))
        since = resolve_since(args, index)
        print(f'since={since.isoformat()}')
        gh_out = os.environ.get('GITHUB_OUTPUT')
        if gh_out:
            with open(gh_out, 'a', encoding='utf-8') as fh:
                fh.write(f'since={since.isoformat()}\n')
        return

    run(args)


if __name__ == '__main__':
    main()
