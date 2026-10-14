#!/usr/bin/env python3
"""speech_recover.py — Two-tier boilerplate recovery for the SoS enriched corpus CSV.

Reads the enriched CSV produced by speech_extract.py and applies:
  Step A — Strip trailing State Dept footers from Tier 2 rows (boilerplate after char 500)
  Step B — Footer-truncation + re-extraction for secretary_anchor_fallback Tier 1 rows
  Step C — Corpus duplicate source matching for no_url Tier 1 rows
  Step D — URL normalization + live fetch for no_checkpoint Tier 1 rows
  Step E — Live re-crawl for recovery_failed Tier 1 rows

Usage:
    python3 speech_recover.py [--version v44] [--mode report|recover]
    python3 speech_recover.py --version v44 --mode recover --rate-limit 0.5
"""
import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).parent))

from speech_extract import (
    BASE_DIR,
    _FOOTER_SIGNALS,
    _get_checkpoint,
    _get_raw_text,
    _has_nav_boilerplate,
    _recover_speaker_text,
    _resolve_url,
    make_speaker_text,
)

# DOSFAN-era signals that exclusively mark page footers — safe to strip at pos > 50
# (vs the general pos > 500 threshold used for other signals).
_DOSFAN_FOOTER_SIGNALS: frozenset = frozenset((
    '[End of Document]',
    "Return to the Secretary's Home Page",
    'Return to the DOSFAN',
))
from hybrid_corpus_recovery import extract_secretary_only_text

try:
    import requests
    from bs4 import BeautifulSoup
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False
    print('[speech_recover] WARNING: requests/bs4 not available — Steps D/E will be skipped.',
          file=sys.stderr)

csv.field_size_limit(10 ** 7)

_LIVE_FETCH_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) '
        'Gecko/20100101 Firefox/115.0'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}
_live_fetch_cache: dict = {}

# Modern www.state.gov / 2021-2025.state.gov nav chrome appears at the very start of
# raw_text for any live-fetched page (BeautifulSoup flattens the header/nav into plain text).
# These signals are NOT in _FOOTER_SIGNALS (which targets archive-era footer text), so
# rows with this boilerplate in speech_text bypass all existing boilerplate checks.
_MODERN_STATE_NAV_SIGS: tuple = (
    'Skip to content\nAn official website',
    'Skip to content An official website',  # space-collapsed variant in some rows
    'Skip to Main Content',                  # 2009-2017.state.gov nav chrome
    'Other State Department Archive Sites',  # 1997-2001 DOSFAN archive nav
)

# Modern state.gov page structure anchor: content is sandwiched between 'hide\n{title}\n'
# and '\nTags\n'.  Works for both 2021-2025.state.gov (Blinken) and www.state.gov (Rubio).
_HIDE_ANCHOR_RE = re.compile(r'\nhide\n')
_DATE_LINE_RE = re.compile(
    r'^(?:January|February|March|April|May|June|July|August|'
    r'September|October|November|December)\s+\d{1,2},\s+\d{4}$'
)
_TAGS_SENTINEL = '\nTags\n'

_REPORT_COLS = [
    'row_id', 'tier', 'category', 'secretary', 'date', 'speech_type',
    'url', 'was_recovered', 'method_used', 'note',
]


# ── Utility helpers ────────────────────────────────────────────────────────────

def _live_fetch_url(url: str):
    """Fetch url with Firefox headers using lxml; return checkpoint-compatible dict or None."""
    if url in _live_fetch_cache:
        return _live_fetch_cache[url]
    if not _REQUESTS_AVAILABLE:
        _live_fetch_cache[url] = None
        return None
    try:
        resp = requests.get(url, timeout=20, headers=_LIVE_FETCH_HEADERS)
        if resp.status_code != 200:
            _live_fetch_cache[url] = None
            return None
        try:
            soup = BeautifulSoup(resp.text, 'lxml')
        except Exception:
            soup = BeautifulSoup(resp.text, 'html.parser')
        text = soup.get_text(separator='\n', strip=True)
        result = {'text': text, 'date': '', 'title': ''}
        _live_fetch_cache[url] = result
        return result
    except Exception:
        _live_fetch_cache[url] = None
        return None


def _strip_trailing_footer(text: str) -> str:
    """Remove the earliest footer signal found after a minimum position, and everything after it.

    General signals use pos > 500 to avoid accidental truncation. DOSFAN-era signals
    (exclusively page-footer markers) use pos > 50 since they never appear in speech content.
    """
    best_pos = len(text)
    found = False
    for sig in _FOOTER_SIGNALS:
        pos = text.find(sig)
        threshold = 50 if sig in _DOSFAN_FOOTER_SIGNALS else 200
        if pos != -1 and pos > threshold and pos < best_pos:
            best_pos = pos
            found = True
    if not found:
        return text
    cut = text.rfind('\n\n', 0, best_pos)
    if cut == -1:
        cut = text.rfind('\n', 0, best_pos)
    return text[:cut].strip() if cut != -1 else text[:best_pos].strip()


def _truncate_at_footer(text: str) -> str:
    """Remove everything from the earliest footer signal onwards (any position)."""
    best_pos = len(text)
    for sig in _FOOTER_SIGNALS:
        pos = text.find(sig)
        if pos != -1 and pos < best_pos:
            best_pos = pos
    if best_pos == len(text):
        return text
    cut = text.rfind('\n', 0, best_pos)
    return text[:cut].strip() if cut != -1 else text[:best_pos].strip()


def _try_url_variants(url: str, checkpoint: dict):
    """Try http/https and trailing-slash variants; return first matching checkpoint key or None."""
    parsed = urlparse(url)
    for scheme in ('https', 'http'):
        for trailing in ('', '/'):
            path = parsed.path.rstrip('/') + trailing
            v = urlunparse((scheme, parsed.netloc, path, parsed.params, parsed.query, ''))
            if v in checkpoint:
                return v
    return None


def _run_extraction(raw_text: str, url: str, secretary: str) -> tuple:
    """Full extraction pipeline; returns (speaker_text, speech_text, mode)."""
    speaker = make_speaker_text(raw_text, url, secretary)
    if speaker and _has_nav_boilerplate(speaker):
        speaker = _recover_speaker_text(raw_text)
    text_for_ext = speaker or raw_text
    speech, mode = extract_secretary_only_text(text_for_ext, secretary)
    if mode == 'fallback_full':
        if speaker:
            speech = speaker.strip()[:120000]
        else:
            speech = ''
            mode = 'recovery_failed'
    # Strip trailing footer from live-fetched content so Steps D/E/F don't
    # introduce footer after Step A has already run.
    speech = _strip_trailing_footer(speech)
    return speaker, speech, mode


def _log_entry(row: dict, tier: str, category: str, url: str,
               was_recovered: bool, method_used: str, note: str) -> dict:
    return {
        'row_id': row.get('row_id', ''),
        'tier': tier,
        'category': category,
        'secretary': row.get('secretary', ''),
        'date': row.get('date', ''),
        'speech_type': row.get('speech_type', ''),
        'url': url,
        'was_recovered': was_recovered,
        'method_used': method_used,
        'note': note,
    }


# ── Data loading ───────────────────────────────────────────────────────────────

def _load_audit(ver: str) -> dict:
    path = BASE_DIR / f'corpus_fusion_full_audit.{ver}.csv'
    if not path.exists():
        print(f'[speech_recover] WARNING: Audit CSV not found: {path.name}', file=sys.stderr)
        return {}
    audit: dict = {}
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            audit[row['row_id']] = row
    print(f'[speech_recover] Loaded audit: {len(audit):,} rows from {path.name}')
    return audit


def _select_enriched_csv(ver: str) -> Path:
    for name in [
        f'secretaryofstate_speeches_final.fused.{ver}.patched.enriched.csv',
        f'secretaryofstate_speeches_final.fused.{ver}.enriched.csv',
    ]:
        p = BASE_DIR / name
        if p.exists():
            print(f'[speech_recover] Input: {p.name}')
            return p
    raise FileNotFoundError(f'No enriched CSV found for version {ver}')


def _load_enriched(ver: str) -> tuple:
    path = _select_enriched_csv(ver)
    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    print(f'[speech_recover] Loaded {len(rows):,} rows')
    return path, fieldnames, rows


# ── Report mode ────────────────────────────────────────────────────────────────

def run_report(ver: str) -> None:
    """Print per-tier/per-category counts and recovery forecast without writing files."""
    audit = _load_audit(ver)
    _, _, rows = _load_enriched(ver)

    total = len(rows)
    full_bp = sum(1 for r in rows if any(sig in r.get('speech_text', '') for sig in _FOOTER_SIGNALS))
    start_bp = sum(1 for r in rows if _has_nav_boilerplate(r.get('speech_text', '')))

    # Tier 2: footer after char 500 (speech present, trailing footer only)
    tier2 = [r for r in rows
             if any(sig in r.get('speech_text', '') for sig in _FOOTER_SIGNALS)
             and not _has_nav_boilerplate(r.get('speech_text', ''))]

    # Tier 1 categories (boilerplate in first 500 chars)
    t1_b, t1_c, t1_d, t1_e, t1_other = [], [], [], [], []
    for row in rows:
        if not _has_nav_boilerplate(row.get('speech_text', '')):
            continue
        mode = row.get('extraction_mode', '')
        if mode in _STEP_B_MODES:
            t1_b.append(row)
        elif 'no_url' in mode:
            t1_c.append(row)
        elif 'no_checkpoint' in mode:
            t1_d.append(row)
        elif 'recovery_failed' in mode:
            t1_e.append(row)
        else:
            t1_other.append(row)

    distinct_d_urls = len({_resolve_url(r, audit) for r in t1_d} - {''})

    print(f'\nSpeech recovery report — {ver}')
    print(f'  Total rows:                       {total:,}')
    print(f'  Boilerplate anywhere in text:     {full_bp:,}  ({full_bp/total*100:.1f}%)')
    print(f'  Boilerplate in first 500 chars:   {start_bp:,}  ({start_bp/total*100:.1f}%)')
    print(f'  Boilerplate only after char 500:  {len(tier2):,}  ({len(tier2)/total*100:.1f}%)')

    print(f'\nTier 2 — trailing footer strip (Step A):')
    print(f'  Rows with footer after char 500:  {len(tier2):,}  → all fixable by _strip_trailing_footer')

    print(f'\nTier 1 — extraction failures (Step B–E):')
    print(f'  Step B  secretary_anchor_fallback  {len(t1_b):5,}  [footer-truncation + re-extract]')
    print(f'  Step C  no_url / corpus_dup        {len(t1_c):5,}  [duplicate source matching]')
    print(f'  Step D  no_checkpoint              {len(t1_d):5,}  [{distinct_d_urls} distinct URLs — norm + live fetch]')
    print(f'  Step E  recovery_failed            {len(t1_e):5,}  [live re-crawl @ 0.5 s/req]')
    if t1_other:
        other_modes = {}
        for r in t1_other:
            m = r.get('extraction_mode', 'unknown')
            other_modes[m] = other_modes.get(m, 0) + 1
        print(f'  Other modes ({len(t1_other)} rows): {other_modes}')

    tier2_modes = {}
    for r in tier2:
        m = r.get('extraction_mode', 'unknown')
        tier2_modes[m] = tier2_modes.get(m, 0) + 1
    print(f'\nTier 2 mode breakdown:')
    for m, cnt in sorted(tier2_modes.items(), key=lambda x: -x[1]):
        print(f'  {m:<40} {cnt:,}')

    step_f_candidates = sum(
        1 for r in rows
        if _has_nav_boilerplate(r.get('speech_text', ''))
        and any(m in r.get('extraction_mode', '') for m in ('recovery_failed', 'no_checkpoint'))
    )
    print(f'\nStep F — Wayback Machine candidates:    {step_f_candidates:5,}  [run with --include-wayback]')

    step_g_candidates = sum(
        1 for r in rows
        if 'recovery_failed' in r.get('extraction_mode', '')
        and r.get('raw_text', '').strip()
    )
    print(f'Step G — Share-anchor re-extraction:    {step_g_candidates:5,}  [runs automatically]')


# ── Recovery steps ─────────────────────────────────────────────────────────────

def _step_a_strip_footers(rows: list) -> tuple:
    """Step A: Strip trailing footer from all rows where footer appears after char 500."""
    changed = 0
    log: list = []
    for row in rows:
        st = row.get('speech_text', '')
        stripped = _strip_trailing_footer(st)
        if stripped != st:
            row['speech_text'] = stripped
            old_mode = row.get('extraction_mode', '')
            row['extraction_mode'] = (old_mode + '+footer_stripped') if old_mode else 'footer_stripped'
            changed += 1
            log.append(_log_entry(row, '2', 'trailing_footer', row.get('url', ''),
                                  True, 'footer_strip', ''))
    return changed, log


_STEP_B_MODES = {'secretary_anchor_fallback', 'secretary_anchor'}


def _step_b_reextract(rows: list) -> tuple:
    """Step B: Footer-truncation + re-extraction for anchor-mode Tier 1 rows."""
    changed = 0
    log: list = []
    for row in rows:
        mode = row.get('extraction_mode', '')
        if not any(m in mode for m in _STEP_B_MODES):
            continue
        if not _has_nav_boilerplate(row.get('speech_text', '')):
            continue
        url = row.get('url', '')
        secretary = row.get('secretary', '')
        # Prefer raw_text already stored in the enriched CSV
        raw_text = row.get('raw_text', '') or (_get_raw_text(url) if url else '')
        if not raw_text:
            log.append(_log_entry(row, '1', 'sec_anchor_fallback', url,
                                  False, 'footer_truncate_reextract', 'no_raw_text'))
            continue
        truncated = _truncate_at_footer(raw_text)
        if len(truncated) < 50:
            log.append(_log_entry(row, '1', 'sec_anchor_fallback', url,
                                  False, 'footer_truncate_reextract', 'truncated_too_short'))
            continue
        speaker, speech, new_mode = _run_extraction(truncated, url, secretary)
        ok = len(speech) >= 50 and not _has_nav_boilerplate(speech)
        log.append(_log_entry(row, '1', 'sec_anchor_fallback', url,
                              ok, 'footer_truncate_reextract',
                              new_mode if ok else 'extraction_failed'))
        if ok:
            row['speech_text'] = speech
            row['speaker_text'] = speaker
            row['extraction_mode'] = 'footertrunc_' + new_mode
            changed += 1
    return changed, log


def _step_c_dup_match(rows: list) -> tuple:
    """Step C: Copy speech_text from a source row for no_url corpus-duplicate rows."""
    # Build lookup: (secretary, date, speech_type) → first clean source row
    key_to_source: dict = {}
    for row in rows:
        mode = row.get('extraction_mode', '')
        if 'no_url' in mode:
            continue
        if _has_nav_boilerplate(row.get('speech_text', '')):
            continue
        k = (row.get('secretary', ''), row.get('date', ''), row.get('speech_type', ''))
        if k not in key_to_source:
            key_to_source[k] = row

    changed = 0
    log: list = []
    for row in rows:
        if 'no_url' not in row.get('extraction_mode', ''):
            continue
        if not _has_nav_boilerplate(row.get('speech_text', '')):
            continue
        k = (row.get('secretary', ''), row.get('date', ''), row.get('speech_type', ''))
        src = key_to_source.get(k)
        src_speech = src.get('speech_text', '') if src else ''
        ok = bool(src) and len(src_speech) >= 50
        log.append(_log_entry(row, '1', 'no_url_dup', row.get('url', ''),
                              ok, 'dup_source_match',
                              src.get('row_id', '') if ok else 'no_matching_source'))
        if ok:
            row['speech_text'] = src_speech
            row['extraction_mode'] = 'dup_recovered'
            changed += 1
    return changed, log


def _step_d_no_checkpoint(rows: list, audit: dict, rate_limit: float) -> tuple:
    """Step D: URL normalization + live fetch for no_checkpoint Tier 1 rows."""
    changed = 0
    log: list = []
    for row in rows:
        mode = row.get('extraction_mode', '')
        if 'no_checkpoint' not in mode:
            continue
        if not _has_nav_boilerplate(row.get('speech_text', '')):
            continue
        url = _resolve_url(row, audit)
        secretary = row.get('secretary', '')
        if not url:
            log.append(_log_entry(row, '1', 'no_checkpoint', '',
                                  False, 'url_norm_live_fetch', 'no_url'))
            continue

        domain = urlparse(url).hostname or ''
        checkpoint = _get_checkpoint(domain)

        # Try URL normalization variants first (no network request)
        norm_url = _try_url_variants(url, checkpoint)
        fresh_raw = ''
        note = ''
        if norm_url:
            entry = checkpoint.get(norm_url)
            if isinstance(entry, dict):
                fresh_raw = entry.get('text', '')
            elif entry:
                fresh_raw = str(entry)
            note = f'url_normalized'

        # Fall back to live fetch
        if not fresh_raw:
            if not _REQUESTS_AVAILABLE:
                log.append(_log_entry(row, '1', 'no_checkpoint', url,
                                      False, 'url_norm_live_fetch', 'requests_unavailable'))
                continue
            result = _live_fetch_url(url)
            time.sleep(rate_limit)
            if result and result.get('text'):
                fresh_raw = result['text']
                note = 'live_fetch'

        if not fresh_raw:
            log.append(_log_entry(row, '1', 'no_checkpoint', url,
                                  False, 'url_norm_live_fetch', 'fetch_failed'))
            continue

        speaker, speech, new_mode = _run_extraction(fresh_raw, url, secretary)
        ok = len(speech) >= 50 and not _has_nav_boilerplate(speech)
        log.append(_log_entry(row, '1', 'no_checkpoint', url,
                              ok, 'url_norm_live_fetch',
                              f'{note}:{new_mode}' if ok else f'{note}:extraction_failed'))
        if ok:
            row['raw_text'] = fresh_raw
            row['speaker_text'] = speaker
            row['speech_text'] = speech
            row['extraction_mode'] = 'no_checkpoint_recovered_' + new_mode
            changed += 1
    return changed, log


def _step_e_live_refetch(rows: list, audit: dict, rate_limit: float) -> tuple:
    """Step E: Live re-crawl for recovery_failed Tier 1 rows using Firefox/lxml fetcher."""
    targets = [r for r in rows
               if 'recovery_failed' in r.get('extraction_mode', '')
               and _has_nav_boilerplate(r.get('speech_text', ''))]

    print(f'[speech_recover] Step E: live re-fetch for {len(targets):,} recovery_failed rows...')
    if not _REQUESTS_AVAILABLE:
        print('[speech_recover] Step E skipped — requests/bs4 not available.', file=sys.stderr)
        return 0, []

    changed = 0
    log: list = []
    for idx, row in enumerate(targets, 1):
        if idx % 50 == 0:
            print(f'  ... {idx:,}/{len(targets):,}', flush=True)

        url = _resolve_url(row, audit)
        secretary = row.get('secretary', '')

        if not url:
            log.append(_log_entry(row, '1', 'recovery_failed', '',
                                  False, 'live_refetch', 'no_url'))
            continue

        result = _live_fetch_url(url)
        time.sleep(rate_limit)

        if not result or not result.get('text'):
            log.append(_log_entry(row, '1', 'recovery_failed', url,
                                  False, 'live_refetch', 'live_fetch_returned_empty'))
            continue

        fresh_raw = result['text']
        speaker, speech, new_mode = _run_extraction(fresh_raw, url, secretary)
        ok = len(speech) >= 50 and not _has_nav_boilerplate(speech)
        log.append(_log_entry(row, '1', 'recovery_failed', url,
                              ok, 'live_refetch',
                              'live_refetched_' + new_mode if ok else 'extraction_failed'))
        if ok:
            row['raw_text'] = fresh_raw
            row['speaker_text'] = speaker
            row['speech_text'] = speech
            row['extraction_mode'] = 'live_refetched_' + new_mode
            changed += 1
    return changed, log


# ── Step F: Wayback Machine recovery ──────────────────────────────────────────

def _strip_wayback_toolbar(soup) -> None:
    """Remove Wayback Machine toolbar elements from parsed HTML in-place."""
    for wm_id in ('wm-ipp-base', 'wm-ipp', 'donato'):
        el = soup.find(id=wm_id)
        if el:
            el.decompose()
    for script in soup.find_all('script'):
        src = script.get('src', '') or ''
        text = script.string or ''
        if 'web.archive.org' in src or '__wm.' in text or 'RufflePlayer' in text:
            script.decompose()


def _wayback_fetch_url(url: str, rate_limit: float = 1.0):
    """Fetch a Wayback Machine snapshot via CDX API; return checkpoint-compatible dict or None."""
    import urllib.parse
    cdx_url = (
        'https://web.archive.org/cdx/search/cdx'
        f'?url={urllib.parse.quote(url, safe=":/?=&#")}'
        '&output=json&limit=1&filter=statuscode:200&fl=timestamp'
    )
    try:
        r = requests.get(cdx_url, timeout=15, headers=_LIVE_FETCH_HEADERS)
        time.sleep(rate_limit)
        if r.status_code != 200 or not r.text.strip():
            return None
        cdx_rows = json.loads(r.text)
        # cdx_rows[0] is the header ['timestamp'], cdx_rows[1] is the first result
        if len(cdx_rows) < 2:
            return None
        timestamp = cdx_rows[1][0]
    except Exception:
        return None

    wayback_url = f'https://web.archive.org/web/{timestamp}/{url}'
    try:
        resp = requests.get(wayback_url, timeout=30, headers=_LIVE_FETCH_HEADERS)
        time.sleep(rate_limit)
        if resp.status_code != 200:
            return None
        try:
            soup = BeautifulSoup(resp.text, 'lxml')
        except Exception:
            soup = BeautifulSoup(resp.text, 'html.parser')
        _strip_wayback_toolbar(soup)
        text = soup.get_text(separator='\n', strip=True)
        return {'text': text, 'date': '', 'title': ''} if text else None
    except Exception:
        return None


def _step_f_wayback(rows: list, audit: dict, rate_limit: float) -> tuple:
    """Step F: Fetch Wayback Machine snapshots for rows that still have boilerplate after Steps D/E."""
    targets = [r for r in rows
               if _has_nav_boilerplate(r.get('speech_text', ''))
               and any(m in r.get('extraction_mode', '')
                       for m in ('recovery_failed', 'no_checkpoint'))]

    print(f'[speech_recover] Step F: Wayback fetch for {len(targets):,} rows...')
    if not _REQUESTS_AVAILABLE:
        print('[speech_recover] Step F skipped — requests/bs4 not available.', file=sys.stderr)
        return 0, []

    changed = 0
    log: list = []
    for idx, row in enumerate(targets, 1):
        if idx % 50 == 0:
            print(f'  ... {idx:,}/{len(targets):,}', flush=True)

        rid = row.get('row_id', '')
        audit_row = audit.get(rid, {})
        new_url = audit_row.get('new_url', '') or row.get('url', '') or ''

        if not new_url:
            log.append(_log_entry(row, '1', 'wayback', '', False, 'wayback_fetch', 'no_url'))
            continue

        secretary = row.get('secretary', '')
        fresh_raw = ''
        source_note = ''

        # Stage 1: try new_url live (fast; works if archive is ever restored)
        live_result = _live_fetch_url(new_url)
        if live_result and live_result.get('text') and not _has_nav_boilerplate(live_result['text'][:500]):
            fresh_raw = live_result['text']
            source_note = 'live'

        # Stage 2: Wayback CDX + fetch of new_url (expected nav-only for 2009-2017.state.gov)
        if not fresh_raw:
            wb2 = _wayback_fetch_url(new_url, rate_limit)
            if wb2 and wb2.get('text') and not _has_nav_boilerplate(wb2['text'][:500]):
                fresh_raw = wb2['text']
                source_note = 'wayback_archive'

        # Stage 3: Wayback CDX + fetch of www.state.gov equivalent (pre-2017 clean crawls)
        if not fresh_raw and '2009-2017.state.gov' in new_url:
            www_url = new_url.replace('2009-2017.state.gov', 'www.state.gov', 1)
            wb3 = _wayback_fetch_url(www_url, rate_limit)
            if wb3 and wb3.get('text') and not _has_nav_boilerplate(wb3['text'][:500]):
                fresh_raw = wb3['text']
                source_note = 'wayback_www'

        if not fresh_raw:
            log.append(_log_entry(row, '1', 'wayback', new_url,
                                  False, 'wayback_fetch', 'all_stages_failed'))
            continue

        speaker, speech, new_mode = _run_extraction(fresh_raw, new_url, secretary)
        ok = len(speech) >= 50 and not _has_nav_boilerplate(speech)
        log.append(_log_entry(row, '1', 'wayback', new_url, ok, 'wayback_fetch',
                              f'{source_note}_{new_mode}' if ok else f'{source_note}_extraction_failed'))
        if ok:
            row['raw_text'] = fresh_raw
            row['speaker_text'] = speaker
            row['speech_text'] = speech
            row['extraction_mode'] = f'wayback_{source_note}_{new_mode}'
            changed += 1
    return changed, log


# ── Step H: Re-extract labeled speech_text through extract_secretary_only_text ──

_LABEL_RE = re.compile(r'SECRETARY [A-Z]+:')
_STEP_H_MODES = ('no_checkpoint', 'fallback_full')


def _strip_secretary_labels(speech: str, secretary: str) -> str:
    """Remove 'SECRETARY [LAST_NAME]:' speaker labels from speech text.

    These labels are speaker-turn markers that should not appear in speech_text
    since the speaker is already identified by the 'secretary' column. Applied
    after extract_secretary_only_text to clean up any mid-line labels that the
    line-anchored regex couldn't detect.
    """
    last_name = secretary.upper().split()[-1] if secretary else ''
    if not last_name:
        return speech
    label_pat = re.compile(rf'SECRETARY {last_name}:[ Â ]*', re.IGNORECASE)
    return label_pat.sub('', speech).strip()


def _step_h_relabel_strip(rows: list) -> int:
    """Step H: Strip speaker labels from no_checkpoint / fallback_full speech_text.

    These rows either have no raw_text (no_checkpoint) or had footer-contaminated
    speaker_text that caused extract_secretary_only_text to reject the whole chunk
    as boilerplate. After Step A stripped the footer from speech_text, re-running
    extract_secretary_only_text on the clean speech_text should succeed.
    """
    targets = [r for r in rows
               if any(m in r.get('extraction_mode', '') for m in _STEP_H_MODES)
               and _LABEL_RE.search(r.get('speech_text', '')[:500])]
    print(f'[speech_recover] Step H: Relabel strip for {len(targets):,} rows...')

    changed = 0
    for row in targets:
        speech = row.get('speech_text', '')
        secretary = row.get('secretary', '')
        new_speech, mode = extract_secretary_only_text(speech, secretary)
        if mode == 'secretary_anchor' and len(new_speech) >= 50:
            new_speech = _strip_secretary_labels(new_speech, secretary)
            if len(new_speech) >= 50:
                row['speech_text'] = new_speech
                row['extraction_mode'] = 'relabeled_secretary_anchor'
                changed += 1
    return changed


def _step_i_label_cleanup(rows: list) -> int:
    """Step I: Strip residual mid-line SECRETARY labels from any remaining rows.

    Step H re-extracted using line-anchored regex, but inline Q&A formatting
    (no newlines between speakers) leaves mid-line labels in the result. This
    step removes any remaining 'SECRETARY [NAME]:' patterns from speech_text
    regardless of mode. Safe because the secretary is always identified by the
    'secretary' column.
    """
    targets = [r for r in rows if _LABEL_RE.search(r.get('speech_text', '')[:500])]
    print(f'[speech_recover] Step I: Residual label cleanup for {len(targets):,} rows...')

    changed = 0
    for row in targets:
        speech = row.get('speech_text', '')
        secretary = row.get('secretary', '')
        cleaned = _strip_secretary_labels(speech, secretary)
        if cleaned != speech and len(cleaned) >= 10:
            row['speech_text'] = cleaned
            changed += 1
    return changed


# ── Step G: Share-anchor re-extraction from existing raw_text ─────────────────

_DATE_RE = re.compile(
    r'(?:January|February|March|April|May|June|July|August|September|'
    r'October|November|December)\s+\d{1,2},\s+\d{4}'
)
_SHARE_END_MARKERS = ('\nIn This Section:', '\nContact Us\n', '\nSubject Index\n')


def _extract_share_anchor(raw_text: str) -> str:
    """Extract speech content using the WaterFall archive Share-button anchor.

    Page structure: ~2000-char nav | Share | type | name | role | location | date
    | SPEECH | In This Section: sidebar. The social Share button renders as
    the literal string '\\nShare\\n' and appears exactly once per page.
    """
    share_idx = raw_text.find('\nShare\n')
    if share_idx == -1:
        return ''
    after_share = raw_text[share_idx:]
    dm = _DATE_RE.search(after_share)
    if not dm:
        return ''
    date_end = share_idx + dm.end()
    content_start = raw_text.find('\n', date_end) + 1
    content_end = len(raw_text)
    for marker in _SHARE_END_MARKERS:
        idx = raw_text.find(marker, content_start)
        if idx != -1 and idx < content_end:
            content_end = idx
    return raw_text[content_start:content_end].strip()


def _step_g_share_anchor(rows: list, audit: dict) -> tuple:
    """Step G: Re-extract speech using Share-anchor for recovery_failed rows with raw_text."""
    targets = [r for r in rows
               if 'recovery_failed' in r.get('extraction_mode', '')
               and r.get('raw_text', '').strip()]
    print(f'[speech_recover] Step G: Share-anchor re-extraction for {len(targets):,} rows...')

    changed = 0
    log: list = []
    for row in targets:
        raw_text = row.get('raw_text', '')
        secretary = row.get('secretary', '')
        rid = row.get('row_id', '')
        url = row.get('url', '') or audit.get(rid, {}).get('new_url', '')

        extracted = _extract_share_anchor(raw_text)
        if len(extracted) < 50:
            log.append(_log_entry(row, '1', 'share_anchor', url,
                                  False, 'share_anchor', 'no_content_found'))
            continue

        speech, mode = extract_secretary_only_text(extracted, secretary)
        if mode == 'fallback_full':
            speech = extracted.strip()[:120000]

        if len(speech) < 50 or _has_nav_boilerplate(speech):
            log.append(_log_entry(row, '1', 'share_anchor', url,
                                  False, 'share_anchor', 'extraction_failed'))
            continue

        row['speaker_text'] = extracted
        row['speech_text'] = speech
        row['extraction_mode'] = f'share_anchor_{mode}'
        log.append(_log_entry(row, '1', 'share_anchor', url,
                              True, 'share_anchor', f'share_anchor_{mode}'))
        changed += 1
    return changed, log


# ── Step K: Drop no_url rows ──────────────────────────────────────────────────

def _step_k_drop_nourl(rows: list, ver: str) -> list:
    """Step K: Remove all no_url rows from the corpus.

    These rows originated from the naive corpus without URL resolution. All are
    flagged excluded_corpus_dup, excluded_phantom, or kept_no_change in the v44 audit.
    Their content (where legitimate) exists in URL-resolved duplicate rows.
    Dropped rows are written to a separate CSV for audit trail.
    """
    keep = []
    dropped = []
    for row in rows:
        if 'no_url' in row.get('extraction_mode', ''):
            dropped.append(row)
        else:
            keep.append(row)

    drop_path = BASE_DIR / f'speech_dropped_nourl.{ver}.csv'
    if dropped:
        fieldnames = list(dropped[0].keys())
        with open(drop_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(dropped)

    print(f'[speech_recover] Step K: removed {len(dropped):,} no_url rows '
          f'({len(keep):,} remain). Dropped rows → {drop_path.name}')
    return keep


# ── Step L: Re-extract modern www.state.gov nav-chrome rows ───────────────────

def _has_modern_state_nav(text: str, limit: int = 500) -> bool:
    """Return True if text opens with modern www.state.gov navigation chrome."""
    head = text[:limit]
    return any(sig in head for sig in _MODERN_STATE_NAV_SIGS)


_DOSFAN_LOOSE_DATE_RE = re.compile(
    r'(?:January|February|March|April|May|June|July|August|September|'
    r'October|November|December)\s+\d{1,2},\s+\d{4}'
)
# End sentinels for DOSFAN/2001-2009 archive pages (no Share or hide anchor).
# '(###)' is the section-end marker used by 1997-2001.state.gov pages that lack
# '[End of Document]'. '\nReturn\n' covers 'Return\nto the DOSFAN...' newline wrapping.
_DOSFAN_CONTENT_END_SIGS: tuple = (
    '[End of Document]', '(###)', '\nReturn\n',
    "Return to the Secretary's Home Page", 'Return to the DOSFAN',
    '\nUpdates\n|',
)
# Short metadata lines that follow the date in DOSFAN/2001-2009 headers — skip these
# to find where the speech body begins.
_DOSFAN_HEADER_SKIP_SIGS: tuple = (
    'As released by', 'U.S. Department of State', 'View Video',
    'As Prepared for Delivery', 'Released on',
)


def _extract_dosfan_content(raw: str) -> str:
    """Extract speech from 1997-2009 DOSFAN-era archive pages (no Share or hide anchor).

    The date is embedded in a location+date line like 'Washington, D.C., January 8, 1997'
    (not a standalone date line). We anchor on the first 'Secretary' byline after the
    DOSFAN nav signal, then find the first date that follows — this is always the metadata
    date, not the footer's 'Released on' repeat.  Content ends at a DOSFAN footer signal.
    """
    dosfan_idx = raw.find('Other State Department Archive Sites')
    if dosfan_idx == -1:
        return ''

    # Find the secretary byline — first '\nSecretary' after the nav signal skips the
    # archive-notice block (which contains no "Secretary" text).
    sec_m = re.search(r'\nSecretary\b', raw[dosfan_idx:])
    if not sec_m:
        return ''
    search_from = dosfan_idx + sec_m.start()

    date_m = _DOSFAN_LOOSE_DATE_RE.search(raw, search_from)
    if not date_m:
        return ''

    date_line_end = raw.find('\n', date_m.end())
    if date_line_end == -1:
        return ''

    lines_after = raw[date_line_end + 1:].split('\n')
    skip = 0
    for line in lines_after[:5]:
        stripped = line.strip()
        if (not stripped or len(stripped) < 60
                or any(s in stripped for s in _DOSFAN_HEADER_SKIP_SIGS)):
            skip += 1
        else:
            break

    content = '\n'.join(lines_after[skip:])

    end_pos = len(content)
    for sig in _DOSFAN_CONTENT_END_SIGS:
        p = content.find(sig)
        if p != -1 and p < end_pos:
            end_pos = p
    # Document number pattern (e.g. 2007/1009, 2003/983) signals end of body on 2001-2009 pages.
    doc_num = re.search(r'\n\d{4}/\d+\n', content)
    if doc_num and doc_num.start() < end_pos:
        end_pos = doc_num.start()

    result = content[:end_pos].strip()
    return result if len(result) >= 100 else ''


def _extract_modern_state_content(raw: str) -> str:
    """Extract speech content from modern/archive www.state.gov raw page text.

    Anchor priority: 'hide\\n' (2021-2025.state.gov / www.state.gov) → 'Share\\n'
    (2017-2021.state.gov) → DOSFAN fallback (1997-2009, no anchor, uses last date).
    End sentinel priority: '\\nTags\\n' → '\\nBack to Top\\n' → _SHARE_END_MARKERS.

    Two content positions are handled for anchored pages:
    - Standard: date is a header → content follows the date line
    - Advisory/Framework: date is a footer → content precedes the date line

    Returns empty string if no usable anchor or content is found.
    """
    m = _HIDE_ANCHOR_RE.search(raw)
    anchor_end = m.end() if m else -1
    if anchor_end == -1:
        share_idx = raw.find('\nShare\n')
        if share_idx == -1:
            return _extract_dosfan_content(raw)
        anchor_end = share_idx + len('\nShare\n')

    after_anchor = raw[anchor_end:]
    # Try end sentinels in priority order: Tags (modern), Back to Top (modern without tags),
    # then legacy Share-era markers for 2009-2017.state.gov pages.
    end_pos = after_anchor.find(_TAGS_SENTINEL)
    if end_pos == -1:
        end_pos = after_anchor.find('\nBack to Top\n')
    if end_pos == -1:
        for marker in _SHARE_END_MARKERS:
            p = after_anchor.find(marker)
            if p != -1 and (end_pos == -1 or p < end_pos):
                end_pos = p
    if end_pos == -1:
        return ''

    block = after_anchor[:end_pos]
    lines = block.split('\n')
    content_start = 0
    date_line = -1
    for i, line in enumerate(lines):
        if _DATE_LINE_RE.match(line.strip()):
            date_line = i
            content_start = i + 1
            break

    content = '\n'.join(lines[content_start:]).strip()

    if not content and date_line > 0:
        # Date is a footer (appears at end of block, not as a header before the content).
        # Take lines before the date line, skipping 'Share this page on:\nClose\n' prefix.
        prefix_skip = 2 if lines and 'Share this page on' in lines[0] else 0
        content = '\n'.join(lines[prefix_skip:date_line]).strip()

    return content


def _step_l_fix_modern_nav(rows: list) -> tuple:
    """Step L: Re-extract speech for rows whose speech_text is modern www.state.gov nav chrome.

    These rows have valid speech in raw_text but speech_text is stuck as the original
    nav boilerplate from the naive corpus (Step J populated raw_text but did not update
    speech_text). Content is extracted via the 'hide' anchor and 'Tags' sentinel pattern,
    then metadata header lines (speech_type, byline, location, date) are stripped.

    For Remarks pages with SECRETARY: labels the extracted block is passed through
    _run_extraction to isolate SoS-only speech_text.  For Press Statements the Secretary
    is the sole speaker, so speaker_text = speech_text = extracted content.

    Returns (updated_rows, fix_count, failed_count).
    """
    fixed = 0
    failed = []
    result = []

    for row in rows:
        if not _has_modern_state_nav(row.get('speech_text', '')):
            result.append(row)
            continue

        raw = row.get('raw_text', '')
        url = row.get('url', '')
        secretary = row.get('secretary', '')

        content = _extract_modern_state_content(raw) if raw else ''

        if not content:
            row['extraction_mode'] = row.get('extraction_mode', '') + '+step_l_failed'
            result.append(row)
            failed.append(row.get('row_id', '?'))
            continue

        last_name = secretary.upper().split()[-1] if secretary else ''
        secretary_anchor_present = bool(re.search(
            rf'(?im)^\s*SECRETARY\s+{re.escape(last_name)}\b',
            content
        )) if last_name else False

        if secretary_anchor_present:
            speaker, speech, _mode = _run_extraction(content, url, secretary)
            if not speech:
                speech = content
        else:
            speaker = content
            speech = content

        row['speaker_text'] = speaker
        row['speech_text'] = speech
        row['extraction_mode'] = row.get('extraction_mode', '') + '+step_l_reextracted'
        result.append(row)
        fixed += 1

    if failed:
        print(f'[speech_recover] Step L: WARNING — {len(failed)} rows failed '
              f'(hide anchor not found): {failed}')
    print(f'[speech_recover] Step L: {fixed} rows re-extracted '
          f'({len(result):,} total rows remain)')
    return result, fixed, len(failed)


# ── Step J: Populate raw_text/speaker_text for no_checkpoint rows ─────────────

def _step_j_populate_no_checkpoint(rows: list, audit: dict, rate_limit: float) -> int:
    """Step J: Populate raw_text/speaker_text for any row with empty raw_text and a URL.

    Step D only runs for rows with boilerplate; many rows have clean speech_text from
    the original fused corpus (or were relabeled by Step H) but empty raw/speaker columns.
    Populate via URL normalization first (no network), then live-fetch.
    Does NOT touch speech_text.
    """
    targets = [r for r in rows
               if not r.get('raw_text', '').strip()
               and _resolve_url(r, audit)]

    print(f'[speech_recover] Step J: populate raw_text for {len(targets):,} rows with empty raw_text...')
    if not _REQUESTS_AVAILABLE:
        print('[speech_recover] Step J: live-fetch unavailable — trying URL normalization only.',
              file=sys.stderr)

    changed = 0
    norm_hits = 0
    for idx, row in enumerate(targets, 1):
        if idx % 200 == 0:
            print(f'  ... {idx:,}/{len(targets):,} ({changed:,} populated so far)', flush=True)

        url = _resolve_url(row, audit)
        if not url:
            continue
        secretary = row.get('secretary', '')
        domain = urlparse(url).hostname or ''
        checkpoint = _get_checkpoint(domain)

        fresh_raw = ''
        # Try URL normalization variants first (no network)
        norm_url = _try_url_variants(url, checkpoint)
        if norm_url:
            entry = checkpoint.get(norm_url)
            fresh_raw = entry.get('text', '') if isinstance(entry, dict) else str(entry or '')
            if fresh_raw:
                norm_hits += 1

        # Fall back to live fetch
        if not fresh_raw and _REQUESTS_AVAILABLE:
            result = _live_fetch_url(url)
            time.sleep(rate_limit)
            if result:
                fresh_raw = result.get('text', '')

        if not fresh_raw:
            continue

        speaker = make_speaker_text(fresh_raw, url, secretary)
        if speaker and _has_nav_boilerplate(speaker):
            speaker = _recover_speaker_text(fresh_raw)

        row['raw_text'] = fresh_raw
        row['speaker_text'] = speaker
        old_mode = row.get('extraction_mode', '')
        row['extraction_mode'] = old_mode + '+raw_populated'
        changed += 1

    print(f'  → {changed:,} rows updated ({norm_hits:,} via URL normalization, '
          f'{changed - norm_hits:,} via live-fetch)')
    return changed


# ── Main recover pipeline ──────────────────────────────────────────────────────

def run_recover(ver: str, rate_limit: float = 0.5, include_wayback: bool = False,
                include_populate_raw: bool = False) -> None:
    """Apply all recovery steps and overwrite the enriched CSV in-place."""
    audit = _load_audit(ver)
    enriched_path, fieldnames, rows = _load_enriched(ver)
    report_path = BASE_DIR / f'speech_recovery_report.{ver}.csv'

    # Ensure all output columns are present
    for col in ('raw_text', 'speaker_text', 'extraction_mode'):
        if col not in fieldnames:
            fieldnames.append(col)

    all_log: list = []

    print(f'[speech_recover] Step A: strip trailing footers...')
    cnt_a, log_a = _step_a_strip_footers(rows)
    all_log.extend(log_a)
    print(f'  → {cnt_a:,} rows updated')

    print(f'[speech_recover] Step H: speaker label strip via re-extraction...')
    cnt_h = _step_h_relabel_strip(rows)
    print(f'  → {cnt_h:,} rows updated')

    print(f'[speech_recover] Step B: footer-truncation re-extraction...')
    cnt_b, log_b = _step_b_reextract(rows)
    all_log.extend(log_b)
    print(f'  → {cnt_b:,} rows updated')

    print(f'[speech_recover] Step C: corpus duplicate source matching...')
    cnt_c, log_c = _step_c_dup_match(rows)
    all_log.extend(log_c)
    print(f'  → {cnt_c:,} rows updated')

    print(f'[speech_recover] Step D: no_checkpoint URL normalization + live fetch...')
    cnt_d, log_d = _step_d_no_checkpoint(rows, audit, rate_limit)
    all_log.extend(log_d)
    print(f'  → {cnt_d:,} rows updated')

    cnt_e, log_e = _step_e_live_refetch(rows, audit, rate_limit)
    all_log.extend(log_e)
    print(f'  → {cnt_e:,} rows updated')

    cnt_f = 0
    if include_wayback:
        cnt_f, log_f = _step_f_wayback(rows, audit, rate_limit)
        all_log.extend(log_f)
        print(f'  → {cnt_f:,} rows updated')

    print(f'[speech_recover] Step G: Share-anchor re-extraction...')
    cnt_g, log_g = _step_g_share_anchor(rows, audit)
    all_log.extend(log_g)
    print(f'  → {cnt_g:,} rows updated')

    print(f'[speech_recover] Step I: Residual label cleanup...')
    cnt_i = _step_i_label_cleanup(rows)
    print(f'  → {cnt_i:,} rows updated')

    cnt_j = 0
    if include_populate_raw:
        cnt_j = _step_j_populate_no_checkpoint(rows, audit, rate_limit)

    print(f'[speech_recover] Step K: drop no_url rows...')
    rows = _step_k_drop_nourl(rows, ver)

    print(f'[speech_recover] Step L: re-extract modern-nav rows...')
    rows, cnt_l_fixed, cnt_l_failed = _step_l_fix_modern_nav(rows)

    total_changed = cnt_a + cnt_b + cnt_c + cnt_d + cnt_e + cnt_f + cnt_g + cnt_h + cnt_i + cnt_j
    print(f'\n[speech_recover] Total rows updated: {total_changed:,} / {len(rows):,}')

    # Write updated enriched CSV
    print(f'[speech_recover] Writing {len(rows):,} rows to {enriched_path.name}...')
    with open(enriched_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f'[speech_recover] Wrote enriched CSV.')

    # Write recovery report
    with open(report_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=_REPORT_COLS)
        writer.writeheader()
        writer.writerows(all_log)
    print(f'[speech_recover] Wrote recovery report: {report_path.name} ({len(all_log):,} rows)')

    # Post-recovery boilerplate counts
    total = len(rows)
    full_bp = sum(1 for r in rows if any(sig in r.get('speech_text', '') for sig in _FOOTER_SIGNALS))
    start_bp = sum(1 for r in rows if _has_nav_boilerplate(r.get('speech_text', '')))
    modern_nav_bp = sum(1 for r in rows if _has_modern_state_nav(r.get('speech_text', '')))
    print(f'\nPost-recovery boilerplate check:')
    print(f'  Full-text boilerplate:   {full_bp:,} ({full_bp/total*100:.1f}%)  [was ~9,136]')
    print(f'  First-500-char:          {start_bp:,} ({start_bp/total*100:.1f}%)  [was ~609]')
    print(f'  Modern-nav boilerplate:  {modern_nav_bp:,} ({modern_nav_bp/total*100:.1f}%)  [was ~18]')


def main() -> None:
    ap = argparse.ArgumentParser(
        description='Recover speech_text boilerplate in the SoS enriched corpus CSV.')
    ap.add_argument('--version', default='v44', help='Corpus version (default: v44)')
    ap.add_argument('--mode', choices=['report', 'recover'], default='report',
                    help='report: print counts (default); recover: apply all fixes and update CSV')
    ap.add_argument('--rate-limit', type=float, default=0.5,
                    help='Seconds between live fetch requests for Steps D/E (default: 0.5)')
    ap.add_argument('--include-wayback', action='store_true',
                    help='Step F: fetch Wayback Machine snapshots for still-failing rows (~18 min)')
    ap.add_argument('--populate-raw', action='store_true',
                    help='Step J: live-fetch to populate empty raw_text/speaker_text for '
                         'no_checkpoint rows (~50 min at default rate limit)')
    args = ap.parse_args()

    if args.mode == 'report':
        run_report(args.version)
    else:
        run_recover(args.version, args.rate_limit, include_wayback=args.include_wayback,
                    include_populate_raw=args.populate_raw)


if __name__ == '__main__':
    main()
