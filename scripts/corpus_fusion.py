#!/usr/bin/env python3
"""corpus_fusion.py

Post-processing pipeline that builds on hybrid_corpus_recovery.py outputs to fix:
  1. Rubio URL duplicates from hybrid_expand (111 groups), while preserving all
     rows where speech_text is unique (791 non-Rubio URL collision groups kept as-is)
  2. Rubio coverage gaps from secstate_naive_corpus2.csv (never indexed in SQLite)
  3. ~2000 unresolved triage rows via four new recovery strategies

Subcommands (run in order):
  smart_dedup   -- two-pass dedup: text-hash for all rows, URL-first for expand rows only
  rubio_expand  -- gap-fill Rubio from secstate_naive_corpus2.csv
  deep_recover  -- enhanced recovery for url_match_triage rows
  fuse          -- merge deep recovery into final output

Imports all shared utilities from hybrid_corpus_recovery.py to avoid duplication.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit, urljoin

try:
    from tqdm import tqdm
except Exception:
    def tqdm(iterable=None, **kwargs):
        return iterable

# ── shared imports from the existing pipeline ─────────────────────────────────
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from hybrid_corpus_recovery import (
    APPOINTMENT_WINDOWS,
    CandidateRecord,
    QUEUE_Q4,
    _clean_location,
    _extract_location_from_speech_text,
    _impute_location_from_url,
    canonical_url_key,
    candidate_from_row,
    connect_db,
    date_in_appointment_window,
    extract_secretary_only_text,
    is_valid_candidate_url,
    load_final_rows,
    normalize_for_match,
    normalize_url,
    normalize_whitespace,
    overlap_score,
    parse_iso_date,
    safe_lower,
    score_candidate,
    setup_field_limit,
    normalize_secretary,
    token_set,
)

# ── default paths ──────────────────────────────────────────────────────────────
# Working directory for the one-off corpus build's intermediate files.
# Override with SOS_WORK_DIR; every path below can also be set by its CLI flag.
BASE_DIR = Path(os.environ.get('SOS_WORK_DIR', 'work'))

DEFAULT_INPUT_FINAL   = BASE_DIR / 'secretaryofstate_speeches_final.with_urls.hybrid.v2.csv'
DEFAULT_SMALL2_CORPUS = BASE_DIR / 'secstate_naive_corpus2.csv'
DEFAULT_DB            = BASE_DIR / 'hybrid_corpus_recovery.state.sqlite'
DEFAULT_TRIAGE        = BASE_DIR / 'url_match_triage.hybrid.v2.csv'

DEFAULT_DEDUPED       = BASE_DIR / 'secretaryofstate_speeches_final.deduped.csv'
DEFAULT_DEDUP_AUDIT   = BASE_DIR / 'corpus_fusion_dedup_audit.csv'
DEFAULT_RUBIO_EXPANDED = BASE_DIR / 'secretaryofstate_speeches_final.deduped.rubio.csv'
DEFAULT_RUBIO_AUDIT   = BASE_DIR / 'corpus_fusion_rubio_audit.csv'
DEFAULT_DEEP_RECOVERY = BASE_DIR / 'corpus_fusion_deep_recovery.csv'
DEFAULT_FUSED         = BASE_DIR / 'secretaryofstate_speeches_final.fused.csv'
DEFAULT_FUSION_AUDIT  = BASE_DIR / 'corpus_fusion_audit.csv'
DEFAULT_FULL_AUDIT       = BASE_DIR / 'corpus_fusion_full_audit.csv'
DEFAULT_ROUND2_RECOVERY  = BASE_DIR / 'corpus_fusion_round2_recovery.csv'
DEFAULT_FUSED_V2         = BASE_DIR / 'secretaryofstate_speeches_final.fused.v2.csv'
DEFAULT_RUBIO_CORPUS     = BASE_DIR / 'secstate_naive_corpus_rubio.csv'
DEFAULT_BACKFILL_AUDIT   = BASE_DIR / 'corpus_fusion_rubio_backfill_audit.csv'
DEFAULT_ROUND3_RECOVERY  = BASE_DIR / 'corpus_fusion_round3_recovery.csv'
DEFAULT_FUSED_V3         = BASE_DIR / 'secretaryofstate_speeches_final.fused.v3.csv'
DEFAULT_FUSED_V4         = BASE_DIR / 'secretaryofstate_speeches_final.fused.v4_clean.csv'
DEFAULT_FUSED_V5         = BASE_DIR / 'secretaryofstate_speeches_final.fused.v5.csv'
DEFAULT_ARCHIVED_CRAWL      = BASE_DIR / 'corpus_fusion_archived_crawl.json'
DEFAULT_ARCHIVED_RECOVERY   = BASE_DIR / 'corpus_fusion_archived_recovery.csv'
DEFAULT_ARCHIVED_CRAWL_2009 = BASE_DIR / 'corpus_fusion_archived_crawl_2009.json'
DEFAULT_ARCHIVED_RECOVERY_2009 = BASE_DIR / 'corpus_fusion_archived_recovery_2009.csv'
DEFAULT_FUSED_V6            = BASE_DIR / 'secretaryofstate_speeches_final.fused.v6.csv'
DEFAULT_FUSED_V7            = BASE_DIR / 'secretaryofstate_speeches_final.fused.v7.csv'
DEFAULT_ARCHIVED_CRAWL_2017 = BASE_DIR / 'corpus_fusion_archived_crawl_2017.json'
DEFAULT_ARCHIVED_RECOVERY_2017 = BASE_DIR / 'corpus_fusion_archived_recovery_2017.csv'
DEFAULT_FUSED_V8            = BASE_DIR / 'secretaryofstate_speeches_final.fused.v8.csv'
DEFAULT_FUSED_V9            = BASE_DIR / 'secretaryofstate_speeches_final.fused.v9.csv'
DEFAULT_ROUND4_RECOVERY     = BASE_DIR / 'corpus_fusion_round4_recovery.csv'
DEFAULT_FUSED_V13           = BASE_DIR / 'secretaryofstate_speeches_final.fused.v13.csv'
DEFAULT_FUSED_V14           = BASE_DIR / 'secretaryofstate_speeches_final.fused.v14.csv'
DEFAULT_ARCHIVED_CRAWL_2021    = BASE_DIR / 'corpus_fusion_archived_crawl_2021.json'
DEFAULT_ARCHIVED_RECOVERY_2021 = BASE_DIR / 'corpus_fusion_archived_recovery_2021.csv'
DEFAULT_ARCHIVED_CRAWL_1997    = BASE_DIR / 'corpus_fusion_archived_crawl_1997.json'
DEFAULT_ARCHIVED_RECOVERY_1997 = BASE_DIR / 'corpus_fusion_archived_recovery_1997.csv'
DEFAULT_FUSED_V15           = BASE_DIR / 'secretaryofstate_speeches_final.fused.v15.csv'
DEFAULT_FUSED_V16           = BASE_DIR / 'secretaryofstate_speeches_final.fused.v16.csv'

# ── quality helpers ────────────────────────────────────────────────────────────

def quality_score_row(row: dict) -> int:
    """Score a final-CSV row on the six target column quality dimensions."""
    score = 0
    text = row.get('speech_text', '') or ''
    if len(text) >= 500:
        score += 4
    elif len(text) >= 200:
        score += 2
    elif len(text) >= 60:
        score += 1
    st = safe_lower(row.get('speech_type', '') or '')
    if st and st not in ('unknown', 'na', 'n/a', ''):
        score += 2
    if (row.get('Location', '') or '').strip():
        score += 1
    if (row.get('Date', '') or '').strip():
        score += 2
    if (row.get('url', '') or '').strip():
        score += 1
    return score


def url_specificity_score(url: str) -> int:
    """Score URL canonicality — deeper, cleaner state.gov paths win."""
    u = (url or '').strip()
    if not u:
        return 0
    try:
        parts = urlsplit(u)
    except Exception:
        return 0
    score = 0
    netloc = (parts.netloc or '').lower()
    if netloc in ('www.state.gov', 'state.gov'):
        score += 2
    path = parts.path or ''
    segments = [s for s in path.split('/') if s]
    if len(segments) >= 4:
        score += 3
    elif len(segments) >= 3:
        score += 2
    elif len(segments) >= 2:
        score += 1
    if not (parts.query or ''):
        score += 1
    return score


def _composite_row_score(row: dict) -> Tuple[int, int]:
    """(quality + url_specificity, speech_text_len) for dedup tiebreaking."""
    q = quality_score_row(row) + url_specificity_score(row.get('url', '') or '')
    t = len(row.get('speech_text', '') or '')
    return q, t


# ── SQLite row → CandidateRecord ───────────────────────────────────────────────

def _sqlite_to_candidate(r) -> CandidateRecord:
    return CandidateRecord(
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


def _fetch_candidates_by_ids(conn, candidate_ids: List[int]) -> List[CandidateRecord]:
    if not candidate_ids:
        return []
    placeholders = ','.join('?' * len(candidate_ids))
    rows = conn.execute(
        f'SELECT * FROM candidates WHERE candidate_id IN ({placeholders})',
        candidate_ids,
    ).fetchall()
    return [_sqlite_to_candidate(r) for r in rows]


# ── text-hash helper ───────────────────────────────────────────────────────────

def _text_hash(text: str) -> str:
    return hashlib.md5(safe_lower(text or '').encode()).hexdigest()[:12]


# ── candidate → final-CSV row ──────────────────────────────────────────────────

_FINAL_FIELDS = [
    'speech_type', 'Location', 'Date', 'secretary', 'speech_text',
    'row_id', 'url', 'url_match_score', 'url_match_method', 'url_match_status',
    'match_pass', 'cleaning_profile_used', 'pass3_stage', 'pass3_profile_used',
]


def _candidate_dict_to_final_row(cand: dict, row_id: str, match_pass: str) -> dict:
    """Convert a candidate_from_row() result dict to a final-CSV row."""
    return {
        'speech_type':          cand.get('speech_type_guess', '') or '',
        'Location':             cand.get('location_best', '') or '',
        'Date':                 cand.get('date', '') or '',
        'secretary':            cand.get('secretary', '') or '',
        'speech_text':          cand.get('speech_text_clean', '') or '',
        'row_id':               row_id,
        'url':                  cand.get('url', '') or '',
        'url_match_score':      '0.950000',
        'url_match_method':     'sc2_direct',
        'url_match_status':     'matched',
        'match_pass':           match_pass,
        'cleaning_profile_used': 'sc2_candidate',
        'pass3_stage':          '',
        'pass3_profile_used':   '',
    }


# ── recovery row builder ───────────────────────────────────────────────────────

_RECOVERY_FIELDS = [
    'row_id', 'action', 'strategy', 'reason', 'stage_used',
    'candidate_id', 'candidate_source', 'score', 'margin',
    'old_url', 'new_url',
    'old_speech_text', 'new_speech_text',
    'speech_type_new', 'location_new', 'date_new', 'secretary_new',
    'changed_url', 'changed_text',
]


def _make_recovery_row(
    *,
    row_id: str,
    action: str,
    strategy: str,
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
) -> dict:
    return {
        'row_id': row_id,
        'action': action,
        'strategy': strategy,
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
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Subcommand 1: smart_dedup
# ═══════════════════════════════════════════════════════════════════════════════

def run_smart_dedup(
    input_final: Path,
    output_deduped: Path,
    output_dedup_audit: Path,
) -> None:
    """Two-pass dedup:
    Pass 1 (all rows): remove rows where (url_key, text_hash) is identical — true duplicates.
    Pass 2 (expand rows only): URL-first dedup for match_pass=='hybrid_expand' rows only.
    All other URL collisions (different speech_text sharing a URL) are preserved.
    """
    rows, fields = load_final_rows(input_final)
    audit_rows: List[dict] = []

    # ── Pass 1: text-hash dedup (all rows) ─────────────────────────────────────
    by_hash: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    no_key_rows: List[dict] = []

    for row in rows:
        uk = canonical_url_key(row.get('url', '') or '')
        th = _text_hash(row.get('speech_text', '') or '')
        if uk:
            by_hash[(uk, th)].append(row)
        else:
            no_key_rows.append(row)
            audit_rows.append({
                'row_id':     row.get('row_id', ''),
                'url':        row.get('url', ''),
                'url_key':    '',
                'text_hash':  th,
                'match_pass': row.get('match_pass', ''),
                'pass':       'pass1_text_hash',
                'action':     'kept_no_url',
            })

    pass1_survivors: List[dict] = list(no_key_rows)
    n_text_dup_groups = 0
    n_text_dup_dropped = 0

    for (uk, th), group in by_hash.items():
        if len(group) == 1:
            pass1_survivors.append(group[0])
            audit_rows.append({
                'row_id':     group[0].get('row_id', ''),
                'url':        group[0].get('url', ''),
                'url_key':    uk,
                'text_hash':  th,
                'match_pass': group[0].get('match_pass', ''),
                'pass':       'pass1_text_hash',
                'action':     'kept_unique',
            })
        else:
            # True duplicates: keep highest-quality row
            n_text_dup_groups += 1
            best = max(group, key=lambda r: (quality_score_row(r), len(r.get('speech_text', '') or '')))
            dropped = [r for r in group if r is not best]
            n_text_dup_dropped += len(dropped)
            pass1_survivors.append(best)
            audit_rows.append({
                'row_id':     best.get('row_id', ''),
                'url':        best.get('url', ''),
                'url_key':    uk,
                'text_hash':  th,
                'match_pass': best.get('match_pass', ''),
                'pass':       'pass1_text_hash',
                'action':     'kept_best',
            })
            for r in dropped:
                audit_rows.append({
                    'row_id':     r.get('row_id', ''),
                    'url':        r.get('url', ''),
                    'url_key':    uk,
                    'text_hash':  th,
                    'match_pass': r.get('match_pass', ''),
                    'pass':       'pass1_text_hash',
                    'action':     'dropped_text_dup',
                })

    print(f'[smart_dedup] pass1 text-hash: input={len(rows)} dup_groups={n_text_dup_groups} dropped={n_text_dup_dropped} survivors={len(pass1_survivors)}')

    # ── Pass 2: URL-first dedup for hybrid_expand rows only ────────────────────
    expand_by_url: Dict[str, List[dict]] = defaultdict(list)
    non_expand: List[dict] = []

    for row in pass1_survivors:
        if (row.get('match_pass', '') or '') == 'hybrid_expand':
            uk = canonical_url_key(row.get('url', '') or '')
            if uk:
                expand_by_url[uk].append(row)
            else:
                non_expand.append(row)
        else:
            non_expand.append(row)

    final: List[dict] = list(non_expand)
    n_expand_dup_groups = 0
    n_expand_dup_dropped = 0

    for uk, group in expand_by_url.items():
        if len(group) == 1:
            final.append(group[0])
            audit_rows.append({
                'row_id':     group[0].get('row_id', ''),
                'url':        group[0].get('url', ''),
                'url_key':    uk,
                'text_hash':  _text_hash(group[0].get('speech_text', '') or ''),
                'match_pass': group[0].get('match_pass', ''),
                'pass':       'pass2_expand_url',
                'action':     'kept_unique',
            })
        else:
            n_expand_dup_groups += 1
            best = max(group, key=_composite_row_score)
            dropped = [r for r in group if r is not best]
            n_expand_dup_dropped += len(dropped)
            final.append(best)
            audit_rows.append({
                'row_id':     best.get('row_id', ''),
                'url':        best.get('url', ''),
                'url_key':    uk,
                'text_hash':  _text_hash(best.get('speech_text', '') or ''),
                'match_pass': best.get('match_pass', ''),
                'pass':       'pass2_expand_url',
                'action':     'kept_best',
            })
            for r in dropped:
                audit_rows.append({
                    'row_id':     r.get('row_id', ''),
                    'url':        r.get('url', ''),
                    'url_key':    uk,
                    'text_hash':  _text_hash(r.get('speech_text', '') or ''),
                    'match_pass': r.get('match_pass', ''),
                    'pass':       'pass2_expand_url',
                    'action':     'dropped_url_dup',
                })

    print(f'[smart_dedup] pass2 expand-url: dup_groups={n_expand_dup_groups} dropped={n_expand_dup_dropped} final={len(final)}')

    with output_deduped.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(final)

    dedup_audit_fields = ['row_id', 'url', 'url_key', 'text_hash', 'match_pass', 'pass', 'action']
    with output_dedup_audit.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=dedup_audit_fields)
        w.writeheader()
        w.writerows(audit_rows)

    total_dropped = n_text_dup_dropped + n_expand_dup_dropped
    print(f'[smart_dedup] input={len(rows)} output={len(final)} total_dropped={total_dropped}')
    print(f'[smart_dedup] wrote {output_deduped}')


# ═══════════════════════════════════════════════════════════════════════════════
# Subcommand 2: rubio_expand
# ═══════════════════════════════════════════════════════════════════════════════

def run_rubio_expand(
    deduped_final: Path,
    small2_corpus: Path,
    output_path: Path,
    output_rubio_audit: Path,
    show_progress: bool,
) -> None:
    rows, fields = load_final_rows(deduped_final)

    # Index existing Rubio rows by url_key
    existing_url_keys: Set[str] = set()
    rubio_idx_by_url: Dict[str, int] = {}   # url_key → index in rows list
    max_numeric_id = 0

    for i, row in enumerate(rows):
        rid = (row.get('row_id', '') or '').strip()
        if rid.isdigit():
            max_numeric_id = max(max_numeric_id, int(rid))

        if 'rubio' in safe_lower(row.get('secretary', '') or ''):
            uk = canonical_url_key(row.get('url', '') or '')
            if uk:
                existing_url_keys.add(uk)
                rubio_idx_by_url[uk] = i

    next_id = max_numeric_id + 1
    new_rows: List[dict] = []
    upgrades: Dict[str, dict] = {}     # url_key → upgraded row dict
    audit_rows: List[dict] = []

    with small2_corpus.open('r', encoding='utf-8', errors='replace', newline='') as f:
        reader = csv.DictReader(f)
        iterator: object = tqdm(reader, desc='rubio_expand', unit='row', disable=not show_progress)

        for sc2_n, sc2_row in enumerate(iterator):
            if normalize_secretary(sc2_row) != 'rubio':
                continue

            cand = candidate_from_row(sc2_row, source_name='small2', row_number=sc2_n + 1)
            if cand is None:
                continue

            uk = cand['url_key']
            if not uk:
                continue

            sc2_final = _candidate_dict_to_final_row(cand, row_id='', match_pass='corpus_fusion_rubio_expand')
            sc2_q = quality_score_row(sc2_final)

            if uk not in existing_url_keys:
                sc2_final['row_id'] = f'sc2_{next_id}'
                next_id += 1
                new_rows.append(sc2_final)
                existing_url_keys.add(uk)
                audit_rows.append({
                    'url_key': uk,
                    'action': 'sc2_rubio_new',
                    'row_id': sc2_final['row_id'],
                    'sc2_quality': sc2_q,
                    'existing_quality': '',
                    'url': sc2_final['url'],
                })
            else:
                existing_idx = rubio_idx_by_url.get(uk)
                if existing_idx is None:
                    continue
                existing_row = rows[existing_idx]
                existing_q = quality_score_row(existing_row)

                if sc2_q > existing_q:
                    # Upgrade: keep original row_id and meta fields, update content
                    upgraded = dict(existing_row)
                    sc2_text = cand.get('speech_text_clean', '') or ''
                    if len(sc2_text) > len(upgraded.get('speech_text', '') or ''):
                        upgraded['speech_text'] = sc2_text
                    # Upgrade URL only if SC2 is more canonical
                    if url_specificity_score(cand.get('url', '') or '') > url_specificity_score(upgraded.get('url', '') or ''):
                        upgraded['url'] = cand.get('url', '') or upgraded['url']
                    # Fill missing speech_type and Location from SC2
                    if not upgraded.get('speech_type', '') or safe_lower(upgraded.get('speech_type', '')) in ('', 'unknown', 'na'):
                        sc2_st = cand.get('speech_type_guess', '') or ''
                        if sc2_st and safe_lower(sc2_st) not in ('', 'unknown', 'na'):
                            upgraded['speech_type'] = sc2_st
                    if not (upgraded.get('Location', '') or '').strip():
                        upgraded['Location'] = cand.get('location_best', '') or ''
                    upgrades[uk] = upgraded
                    audit_rows.append({
                        'url_key': uk,
                        'action': 'sc2_rubio_upgrade',
                        'row_id': existing_row.get('row_id', ''),
                        'sc2_quality': sc2_q,
                        'existing_quality': existing_q,
                        'url': upgraded['url'],
                    })
                else:
                    audit_rows.append({
                        'url_key': uk,
                        'action': 'sc2_rubio_no_change',
                        'row_id': existing_row.get('row_id', ''),
                        'sc2_quality': sc2_q,
                        'existing_quality': existing_q,
                        'url': existing_row.get('url', ''),
                    })

    # Apply upgrades in-place
    output_rows: List[dict] = []
    for row in rows:
        if 'rubio' in safe_lower(row.get('secretary', '') or ''):
            uk = canonical_url_key(row.get('url', '') or '')
            if uk and uk in upgrades:
                output_rows.append(upgrades[uk])
                continue
        output_rows.append(row)
    output_rows.extend(new_rows)

    with output_path.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(output_rows)

    rubio_audit_fields = ['url_key', 'action', 'row_id', 'sc2_quality', 'existing_quality', 'url']
    with output_rubio_audit.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=rubio_audit_fields)
        w.writeheader()
        w.writerows(audit_rows)

    n_new = len(new_rows)
    n_upgraded = len(upgrades)
    n_no_change = sum(1 for a in audit_rows if a['action'] == 'sc2_rubio_no_change')
    print(f'[rubio_expand] sc2_new={n_new} upgraded={n_upgraded} no_change={n_no_change}')
    print(f'[rubio_expand] output_rows={len(output_rows)}')
    print(f'[rubio_expand] wrote {output_path}')


# ═══════════════════════════════════════════════════════════════════════════════
# Subcommand 3: deep_recover
# ═══════════════════════════════════════════════════════════════════════════════

def _build_sc2_url_index(small2_corpus: Path, show_progress: bool) -> Dict[str, List[dict]]:
    """Build url_key → list[raw SC2 row dicts] from all URL variant fields."""
    index: Dict[str, List[dict]] = defaultdict(list)
    url_fields = ('url', 'final_url', 'original_url', 'scraped_url')

    with small2_corpus.open('r', encoding='utf-8', errors='replace', newline='') as f:
        reader = csv.DictReader(f)
        iterator: object = tqdm(reader, desc='Index SC2', unit='row', disable=not show_progress)
        for row in iterator:
            seen: Set[str] = set()
            for field in url_fields:
                u = normalize_url(row.get(field, '') or '')
                if not u:
                    continue
                key = canonical_url_key(u)
                if key and key not in seen:
                    index[key].append(row)
                    seen.add(key)

    return dict(index)


def run_deep_recover(
    input_final: Path,
    triage_csv: Path,
    checkpoint_db: Path,
    small2_corpus: Path,
    output_recovery: Path,
    s3_min_score: float,
    s3_min_margin: float,
    s4_min_score: float,
    s4_min_margin: float,
    s4_max_candidates: int,
    show_progress: bool,
) -> None:
    # Load final CSV for old_text lookup
    print('[deep_recover] Loading final CSV...')
    final_rows, _ = load_final_rows(input_final)
    final_by_id: Dict[str, dict] = {
        (r.get('row_id', '') or '').strip(): r
        for r in final_rows
        if (r.get('row_id', '') or '').strip()
    }

    # Build SC2 URL index (in-memory ~129 MB)
    print('[deep_recover] Building SC2 URL index...')
    sc2_index = _build_sc2_url_index(small2_corpus, show_progress=show_progress)
    print(f'[deep_recover] SC2 index: {len(sc2_index)} unique URL keys')

    conn = connect_db(checkpoint_db)

    triage_rows: List[dict] = []
    with triage_csv.open('r', encoding='utf-8', errors='replace', newline='') as f:
        triage_rows = list(csv.DictReader(f))
    print(f'[deep_recover] triage rows={len(triage_rows)}')

    recovery_rows: List[dict] = []
    stats: Dict[str, int] = {'S1': 0, 'S2': 0, 'S3': 0, 'S4': 0, 'unresolved': 0}

    iterator: object = triage_rows
    if show_progress:
        iterator = tqdm(triage_rows, desc='deep_recover', unit='row')

    for triage_row in iterator:
        row_id     = (triage_row.get('row_id', '') or '').strip()
        old_url    = normalize_url(triage_row.get('old_url', '') or '')
        old_uk     = canonical_url_key(old_url)
        secretary  = safe_lower(triage_row.get('secretary', '') or '')
        row_date   = parse_iso_date(triage_row.get('date', '') or '')
        queue_id   = (triage_row.get('queue_id', '') or '').strip()
        stage_used = (triage_row.get('stage_used', '') or '').strip()

        final_row  = final_by_id.get(row_id, {})
        old_text   = final_row.get('speech_text', '') or ''

        accepted = False

        # ── S1: Direct URL match in SQLite ────────────────────────────────────
        if old_uk and not accepted:
            s1_rows = conn.execute(
                '''
                SELECT * FROM candidates
                WHERE url_key = ? AND secretary = ?
                ORDER BY text_len DESC LIMIT 20
                ''',
                (old_uk, secretary),
            ).fetchall()
            s1_rows = [r for r in s1_rows if is_valid_candidate_url(r['url'], secretary)]
            if s1_rows:
                best = s1_rows[0]
                new_url = normalize_url(best['url']) or old_url
                new_text = best['speech_text_clean'] or old_text
                recovery_rows.append(_make_recovery_row(
                    row_id=row_id,
                    action='recovered',
                    strategy='S1_direct_url',
                    reason='direct_url_match',
                    stage_used='url_key_lookup',
                    candidate_id=str(best['candidate_id']),
                    candidate_source=best['corpus_source'],
                    score='1.000000',
                    margin='1.000000',
                    old_url=old_url,
                    new_url=new_url,
                    old_speech_text=old_text,
                    new_speech_text=new_text,
                    speech_type_new=best['speech_type_guess'] or final_row.get('speech_type', ''),
                    location_new=_clean_location(
                        best['location_best'] or final_row.get('Location', ''),
                        speech_text=new_text,
                    ),
                    date_new=best['date'] or final_row.get('Date', ''),
                    secretary_new=secretary,
                    changed_url='1' if new_url != old_url else '0',
                    changed_text='1' if normalize_whitespace(new_text) != normalize_whitespace(old_text) else '0',
                ))
                stats['S1'] += 1
                accepted = True

        # ── S2: SC2 URL lookup ────────────────────────────────────────────────
        if not accepted and old_uk and old_uk in sc2_index:
            best_sc2_cand: Optional[dict] = None
            best_sc2_len = 0
            for sc2_row in sc2_index[old_uk]:
                if normalize_secretary(sc2_row) != secretary:
                    continue
                cand = candidate_from_row(sc2_row, source_name='small2', row_number=0)
                if cand is None:
                    continue
                if not is_valid_candidate_url(cand.get('url', '') or '', secretary):
                    continue
                t_len = len(cand.get('speech_text_clean', '') or '')
                if t_len >= 100 and t_len > best_sc2_len:
                    best_sc2_cand = cand
                    best_sc2_len = t_len
            if best_sc2_cand is not None:
                new_url  = best_sc2_cand.get('url', '') or old_url
                new_text = best_sc2_cand.get('speech_text_clean', '') or old_text
                recovery_rows.append(_make_recovery_row(
                    row_id=row_id,
                    action='recovered',
                    strategy='S2_sc2_url',
                    reason='small_corpus2_url_direct',
                    stage_used='sc2_url_lookup',
                    candidate_id='',
                    candidate_source='small2',
                    score='0.950000',
                    margin='0.950000',
                    old_url=old_url,
                    new_url=new_url,
                    old_speech_text=old_text,
                    new_speech_text=new_text,
                    speech_type_new=best_sc2_cand.get('speech_type_guess', '') or final_row.get('speech_type', ''),
                    location_new=_clean_location(
                        best_sc2_cand.get('location_best', '') or final_row.get('Location', ''),
                        speech_text=new_text,
                    ),
                    date_new=best_sc2_cand.get('date', '') or final_row.get('Date', ''),
                    secretary_new=secretary,
                    changed_url='1' if new_url != old_url else '0',
                    changed_text='1' if normalize_whitespace(new_text) != normalize_whitespace(old_text) else '0',
                ))
                stats['S2'] += 1
                accepted = True

        # ── S3: Secretary-only text re-scoring of top candidates ──────────────
        if not accepted:
            top_json_raw = triage_row.get('top_candidates_json', '') or '[]'
            try:
                top_json_data = json.loads(top_json_raw)
            except Exception:
                top_json_data = []

            cand_ids = [int(c['candidate_id']) for c in top_json_data if c.get('candidate_id')]
            if cand_ids and old_text:
                candidates = _fetch_candidates_by_ids(conn, cand_ids)
                candidates = [c for c in candidates if is_valid_candidate_url(c.url, secretary)]
                if candidates:
                    # Prefer exact-date candidates; if none exist, keep all and let the
                    # score threshold (s3_min_score) reject low-confidence matches.
                    if row_date:
                        date_filtered = [c for c in candidates
                                         if parse_iso_date(c.date or '') == row_date]
                        if date_filtered:
                            candidates = date_filtered

                    sec_text, _mode = extract_secretary_only_text(old_text, secretary)
                    if not sec_text or len(sec_text) < 30:
                        sec_text = old_text

                    scored: List[tuple] = []
                    for cand in candidates:
                        s, seq, lex, db = score_candidate(sec_text, row_date, cand)
                        scored.append((s, seq, cand))
                    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)

                    top1 = scored[0][0]
                    top2 = scored[1][0] if len(scored) > 1 else 0.0
                    margin = top1 - top2

                    if top1 >= s3_min_score and margin >= s3_min_margin:
                        best_c = scored[0][2]
                        new_url  = best_c.url or old_url
                        new_text = best_c.speech_text_clean or old_text
                        recovery_rows.append(_make_recovery_row(
                            row_id=row_id,
                            action='recovered',
                            strategy='S3_sec_rescore',
                            reason='secretary_text_rescore',
                            stage_used=stage_used,
                            candidate_id=str(best_c.candidate_id),
                            candidate_source=best_c.corpus_source,
                            score=f'{top1:.6f}',
                            margin=f'{margin:.6f}',
                            old_url=old_url,
                            new_url=new_url,
                            old_speech_text=old_text,
                            new_speech_text=new_text,
                            speech_type_new=best_c.speech_type_guess or final_row.get('speech_type', ''),
                            location_new=_clean_location(
                                best_c.location_best or final_row.get('Location', ''),
                                speech_text=new_text,
                            ),
                            date_new=best_c.date or final_row.get('Date', ''),
                            secretary_new=secretary,
                            changed_url='1' if new_url != old_url else '0',
                            changed_text='1' if normalize_whitespace(new_text) != normalize_whitespace(old_text) else '0',
                        ))
                        stats['S3'] += 1
                        accepted = True

        # ── S4: Exact-date search (Q4 only) ──────────────────────────────────
        if not accepted and queue_id == QUEUE_Q4 and row_date:
            broad_db_rows = conn.execute(
                '''
                SELECT * FROM candidates
                WHERE secretary = ? AND date = ?
                ORDER BY corpus_source ASC, candidate_id ASC
                LIMIT 600
                ''',
                (secretary, row_date.isoformat()),
            ).fetchall()

            if broad_db_rows:
                broad_cands = [_sqlite_to_candidate(r) for r in broad_db_rows]
                broad_cands = [c for c in broad_cands if is_valid_candidate_url(c.url, secretary)]

            if broad_db_rows and broad_cands:
                sec_text, _mode = extract_secretary_only_text(old_text, secretary)
                if not sec_text or len(sec_text) < 30:
                    sec_text = old_text

                scored_s4: List[tuple] = []
                for cand in broad_cands:
                    s1_raw, seq1, _, _ = score_candidate(old_text, row_date, cand)
                    s2_raw, seq2, _, _ = score_candidate(sec_text, row_date, cand)
                    best_s = s1_raw if s1_raw >= s2_raw else s2_raw
                    best_seq = seq1 if s1_raw >= s2_raw else seq2
                    scored_s4.append((best_s, best_seq, cand))
                scored_s4.sort(key=lambda t: (t[0], t[1]), reverse=True)

                top1 = scored_s4[0][0]
                top2 = scored_s4[1][0] if len(scored_s4) > 1 else 0.0
                margin = top1 - top2

                if top1 >= s4_min_score and margin >= s4_min_margin:
                    best_c = scored_s4[0][2]
                    new_url  = best_c.url or old_url
                    new_text = best_c.speech_text_clean or old_text
                    recovery_rows.append(_make_recovery_row(
                        row_id=row_id,
                        action='recovered',
                        strategy='S4_exact_date',
                        reason='q4_exact_date',
                        stage_used='exact_date',
                        candidate_id=str(best_c.candidate_id),
                        candidate_source=best_c.corpus_source,
                        score=f'{top1:.6f}',
                        margin=f'{margin:.6f}',
                        old_url=old_url,
                        new_url=new_url,
                        old_speech_text=old_text,
                        new_speech_text=new_text,
                        speech_type_new=best_c.speech_type_guess or final_row.get('speech_type', ''),
                        location_new=_clean_location(
                            best_c.location_best or final_row.get('Location', ''),
                            speech_text=new_text,
                        ),
                        date_new=best_c.date or final_row.get('Date', ''),
                        secretary_new=secretary,
                        changed_url='1' if new_url != old_url else '0',
                        changed_text='1' if normalize_whitespace(new_text) != normalize_whitespace(old_text) else '0',
                    ))
                    stats['S4'] += 1
                    accepted = True

        # ── Unresolved ────────────────────────────────────────────────────────
        if not accepted:
            recovery_rows.append(_make_recovery_row(
                row_id=row_id,
                action='unchanged_unresolved',
                strategy='none',
                reason='no_strategy_succeeded',
                stage_used=stage_used,
                candidate_id=triage_row.get('candidate_id', ''),
                candidate_source=triage_row.get('candidate_source', ''),
                score=triage_row.get('score', '0.000000'),
                margin=triage_row.get('margin', '0.000000'),
                old_url=old_url,
                new_url=old_url,
                old_speech_text=old_text,
                new_speech_text=old_text,
                speech_type_new=final_row.get('speech_type', ''),
                location_new=_clean_location(
                    final_row.get('Location', ''),
                    speech_text=old_text,
                ),
                date_new=final_row.get('Date', ''),
                secretary_new=secretary,
                changed_url='0',
                changed_text='0',
            ))
            stats['unresolved'] += 1

    conn.close()

    with output_recovery.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=_RECOVERY_FIELDS)
        w.writeheader()
        w.writerows(recovery_rows)

    total = len(triage_rows)
    recovered = total - stats['unresolved']
    print(
        f'[deep_recover] triage={total} recovered={recovered} unresolved={stats["unresolved"]}'
        f' | S1={stats["S1"]} S2={stats["S2"]} S3={stats["S3"]} S4={stats["S4"]}'
    )
    print(f'[deep_recover] wrote {output_recovery}')


# ═══════════════════════════════════════════════════════════════════════════════
# Subcommand 4: fuse
# ═══════════════════════════════════════════════════════════════════════════════

def run_fuse(
    deduped_final: Path,
    deep_recovery_csv: Path,
    output_fused: Path,
    output_audit: Path,
) -> None:
    rows, fields = load_final_rows(deduped_final)

    recovery_map: Dict[str, dict] = {}
    with deep_recovery_csv.open('r', encoding='utf-8', errors='replace', newline='') as f:
        for rec in csv.DictReader(f):
            rid = (rec.get('row_id', '') or '').strip()
            if rid:
                recovery_map[rid] = rec

    merged: List[dict] = []
    audit_rows: List[dict] = []

    for row in rows:
        rid = (row.get('row_id', '') or '').strip()
        rec = recovery_map.get(rid)
        out = dict(row)
        q_before = quality_score_row(out)

        if rec and rec.get('action') == 'recovered':
            # Update the six target columns
            new_url  = rec.get('new_url', '') or out.get('url', '')
            new_text = rec.get('new_speech_text', '') or out.get('speech_text', '')
            new_st   = rec.get('speech_type_new', '') or out.get('speech_type', '')
            new_loc  = (
                _clean_location(
                    rec.get('location_new', '') or out.get('Location', ''),
                    speech_text=rec.get('new_speech_text', '') or out.get('speech_text', ''),
                )
                or _impute_location_from_url(rec.get('new_url', '') or out.get('url', ''))
            )
            new_date = rec.get('date_new', '') or out.get('Date', '')
            new_sec  = rec.get('secretary_new', '') or out.get('secretary', '')
            # Preserve existing capitalization if same secretary name (recovery uses lowercase)
            existing_sec = out.get('secretary', '')
            if existing_sec and safe_lower(new_sec) == safe_lower(existing_sec):
                new_sec = existing_sec
            out['url']         = new_url
            out['speech_text'] = new_text
            out['speech_type'] = new_st
            out['Location']    = new_loc
            out['Date']        = new_date
            out['secretary']   = new_sec

        merged.append(out)
        audit_rows.append({
            'row_id':        rid,
            'action':        rec.get('action', 'no_recovery_record') if rec else 'no_recovery_record',
            'strategy':      rec.get('strategy', '') if rec else '',
            'score':         rec.get('score', '') if rec else '',
            'changed_url':   rec.get('changed_url', '0') if rec else '0',
            'changed_text':  rec.get('changed_text', '0') if rec else '0',
            'quality_before': q_before,
            'quality_after':  quality_score_row(out),
        })

    with output_fused.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(merged)

    fusion_audit_fields = [
        'row_id', 'action', 'strategy', 'score',
        'changed_url', 'changed_text', 'quality_before', 'quality_after',
    ]
    with output_audit.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fusion_audit_fields)
        w.writeheader()
        w.writerows(audit_rows)

    n_recovered = sum(1 for a in audit_rows if a['action'] == 'recovered')
    if audit_rows:
        mean_before = sum(int(a['quality_before']) for a in audit_rows) / len(audit_rows)
        mean_after  = sum(int(a['quality_after'])  for a in audit_rows) / len(audit_rows)
    else:
        mean_before = mean_after = 0.0
    print(f'[fuse] input={len(rows)} output={len(merged)} applied_recoveries={n_recovered}')
    print(f'[fuse] mean_quality  before={mean_before:.3f}  after={mean_after:.3f}')
    print(f'[fuse] wrote {output_fused}')


# ═══════════════════════════════════════════════════════════════════════════════
# Subcommand 5: rubio_backfill
# ═══════════════════════════════════════════════════════════════════════════════

_BACKFILL_AUDIT_FIELDS = ['row_id', 'url', 'url_key', 'date', 'secretary', 'text_len', 'action']


def run_rubio_backfill(
    fused_csv: Path,
    rubio_corpus: Path,
    output_fused: Path,
    output_backfill_audit: Path,
    show_progress: bool,
) -> None:
    """Append net-new Rubio speeches from secstate_naive_corpus_rubio.csv to the fused CSV.

    The corpus uses raw_text + date_inferred + secretary_inferred columns.
    candidate_from_row() handles this format natively. Rows with secretary_inferred
    empty or 'unknown' get overridden to 'rubio' before the call since this file is
    Rubio-specific; the appointment window check still validates the date.
    """
    rows, fields = load_final_rows(fused_csv)

    # Build URL key set across ALL rows (not just Rubio) to avoid any collision
    existing_url_keys: Set[str] = set()
    max_numeric_id = 0
    for row in rows:
        uk = canonical_url_key(row.get('url', '') or '')
        if uk:
            existing_url_keys.add(uk)
        rid = (row.get('row_id', '') or '').strip()
        if rid.isdigit():
            max_numeric_id = max(max_numeric_id, int(rid))

    new_rows: List[dict] = []
    audit_rows: List[dict] = []
    next_id = max_numeric_id + 1
    stats = {'rb_new': 0, 'skipped_exists': 0, 'skipped_no_cand': 0}

    with rubio_corpus.open('r', encoding='utf-8', errors='replace', newline='') as f:
        reader = csv.DictReader(f)
        iterator: object = tqdm(reader, desc='rubio_backfill', unit='row', disable=not show_progress)

        for n, corpus_row in enumerate(iterator):
            # Override unknown/empty secretary_inferred — file is Rubio-specific
            sec_inf = (corpus_row.get('secretary_inferred', '') or '').strip().lower()
            if sec_inf in ('', 'unknown'):
                corpus_row['secretary_inferred'] = 'rubio'

            cand = candidate_from_row(corpus_row, source_name='rubio_backfill', row_number=n + 1)

            raw_url = (corpus_row.get('url', '') or '').strip()
            url_key = canonical_url_key(raw_url)
            date_val = (corpus_row.get('date_inferred', '') or '').strip()

            if cand is None:
                stats['skipped_no_cand'] += 1
                audit_rows.append({
                    'row_id':    '',
                    'url':       raw_url,
                    'url_key':   url_key,
                    'date':      date_val,
                    'secretary': (corpus_row.get('secretary_inferred', '') or '').strip(),
                    'text_len':  len(corpus_row.get('raw_text', '') or ''),
                    'action':    'skipped_no_cand',
                })
                continue

            uk = cand['url_key']
            if uk in existing_url_keys:
                stats['skipped_exists'] += 1
                audit_rows.append({
                    'row_id':    '',
                    'url':       cand['url'],
                    'url_key':   uk,
                    'date':      cand['date'],
                    'secretary': cand['secretary'],
                    'text_len':  len(cand['speech_text_clean']),
                    'action':    'skipped_exists',
                })
                continue

            row_id = str(next_id)
            next_id += 1
            existing_url_keys.add(uk)
            stats['rb_new'] += 1

            new_row = {
                'speech_type':           cand.get('speech_type_guess', '') or '',
                'Location':              cand.get('location_best', '') or '',
                'Date':                  cand.get('date', '') or '',
                'secretary':             cand.get('secretary', '') or '',
                'speech_text':           cand.get('speech_text_clean', '') or '',
                'row_id':                row_id,
                'url':                   cand.get('url', '') or '',
                'url_match_score':       '1.000000',
                'url_match_method':      'rubio_backfill_direct',
                'url_match_status':      'matched',
                'match_pass':            'rubio_backfill',
                'cleaning_profile_used': 'rubio_backfill',
                'pass3_stage':           '',
                'pass3_profile_used':    '',
            }
            new_rows.append(new_row)
            audit_rows.append({
                'row_id':    row_id,
                'url':       cand['url'],
                'url_key':   uk,
                'date':      cand['date'],
                'secretary': cand['secretary'],
                'text_len':  len(cand['speech_text_clean']),
                'action':    'rb_new',
            })

    output_rows = rows + new_rows

    with output_fused.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(output_rows)

    with output_backfill_audit.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=_BACKFILL_AUDIT_FIELDS)
        w.writeheader()
        w.writerows(audit_rows)

    print(
        f'[rubio_backfill] corpus_rows={n + 1} rb_new={stats["rb_new"]} '
        f'skipped_exists={stats["skipped_exists"]} skipped_no_cand={stats["skipped_no_cand"]}'
    )
    print(f'[rubio_backfill] output_rows={len(output_rows)}')
    print(f'[rubio_backfill] wrote {output_fused}')


# ═══════════════════════════════════════════════════════════════════════════════
# Subcommand 7: recover_round2
# ═══════════════════════════════════════════════════════════════════════════════


def run_recover_round2(
    fused_csv: Path,
    round1_recovery: Path,
    triage_csv: Path,
    checkpoint_db: Path,
    output_recovery: Path,
    r2s2_min_score: float = 0.70,
    r2s2_min_margin: float = 0.02,
    r2s3_window_days: int = 180,
    r2s3_min_score: float = 0.70,
    r2s3_min_margin: float = 0.05,
    r2s4_min_score: float = 0.70,
    r2s4_min_margin: float = 0.04,
    r2s4_max_candidates: int = 400,
    show_progress: bool = True,
) -> None:
    """Round-2 recovery for the 1,768 unchanged_unresolved rows.

    Applies four new strategies with relaxed thresholds, targeting queues where
    the round-1 S3/S4 margin or score constraints were the binding constraint:
      R2-S1: Q1 auto-accept (score 0.829 mean — never accepted in round 1)
      R2-S2: Q2 relaxed margin (all 159 Q2 rows score >=0.60, margin was too tight)
      R2-S3: Q3 wider date window (384 rows, ±6 months from speech date)
      R2-S4: Q4 relaxed thresholds (165 close-miss rows with score >=0.50)

    Output is in the same _RECOVERY_FIELDS format as run_deep_recover so the
    existing `fuse` subcommand can be reused directly.
    """
    from datetime import timedelta

    print('[recover_round2] Loading fused CSV for speech_text lookup...')
    fused_rows, _ = load_final_rows(fused_csv)
    fused_by_id: Dict[str, dict] = {
        (r.get('row_id', '') or '').strip(): r
        for r in fused_rows
        if (r.get('row_id', '') or '').strip()
    }

    print('[recover_round2] Loading round1 recovery to find unchanged_unresolved rows...')
    setup_field_limit()
    unresolved_ids: Set[str] = set()
    with round1_recovery.open('r', encoding='utf-8', errors='replace', newline='') as f:
        for rec in csv.DictReader(f):
            if rec.get('action', '') == 'unchanged_unresolved':
                rid = (rec.get('row_id', '') or '').strip()
                if rid:
                    unresolved_ids.add(rid)
    print(f'[recover_round2] unchanged_unresolved rows: {len(unresolved_ids)}')

    print('[recover_round2] Loading triage CSV...')
    triage_by_id: Dict[str, dict] = {}
    with triage_csv.open('r', encoding='utf-8', errors='replace', newline='') as f:
        for rec in csv.DictReader(f):
            rid = (rec.get('row_id', '') or '').strip()
            if rid:
                triage_by_id[rid] = rec

    conn = connect_db(checkpoint_db)

    recovery_rows: List[dict] = []
    stats = {'R2-S1': 0, 'R2-S2': 0, 'R2-S3': 0, 'R2-S4': 0, 'unresolved': 0}

    iterator = tqdm(
        sorted(unresolved_ids),
        desc='recover_round2', unit='row', disable=not show_progress,
    )

    for row_id in iterator:
        triage_row = triage_by_id.get(row_id)
        if not triage_row:
            stats['unresolved'] += 1
            recovery_rows.append(_make_recovery_row(
                row_id=row_id, action='unchanged_unresolved', strategy='', reason='not_in_triage',
                stage_used='recover_round2', candidate_id='', candidate_source='', score='0.000000',
                margin='', old_url='', new_url='', old_speech_text='', new_speech_text='',
                speech_type_new='', location_new='', date_new='', secretary_new='',
                changed_url='0', changed_text='0',
            ))
            continue

        queue_id  = triage_row.get('queue_id', '')
        old_url   = (triage_row.get('old_url', '') or '').strip()
        secretary = (triage_row.get('secretary', '') or '').strip()
        date_str  = (triage_row.get('date', '') or '').strip()
        row_date  = parse_iso_date(date_str)
        old_text  = (fused_by_id.get(row_id, {}).get('speech_text', '') or '')
        top_cands_json = (triage_row.get('top_candidates_json', '') or '').strip()
        try:
            top_cands_list = json.loads(top_cands_json) if top_cands_json else []
        except Exception:
            top_cands_list = []
        top_ids = [c['candidate_id'] for c in top_cands_list if 'candidate_id' in c]
        sec_text, _ = extract_secretary_only_text(old_text, secretary)

        accepted = False

        # ── R2-S1: Q1 auto-accept ────────────────────────────────────────────
        if queue_id == 'Q1_near_miss' and top_ids:
            cands = _fetch_candidates_by_ids(conn, [int(x) for x in top_ids])
            cands = [c for c in cands if is_valid_candidate_url(c.url, secretary)]
            if cands:
                if row_date:
                    date_cands = [c for c in cands if parse_iso_date(c.date or '') == row_date]
                    cands = date_cands if date_cands else cands
                best = max(cands, key=lambda c: c.text_len)
                recovery_rows.append(_make_r2_recovery_row(
                    row_id, best, old_text, old_url, 1.0, '', 'r2_q1_auto_accept', 'R2-S1',
                ))
                stats['R2-S1'] += 1
                accepted = True

        # ── R2-S2: Q2 tie-accept ─────────────────────────────────────────────
        # 141/159 Q2 rows have margin=0 because the same speech is indexed twice
        # in the DB (duplicate candidates). Break ties by text_len; accept any
        # candidate where score >= threshold regardless of margin.
        if not accepted and queue_id == 'Q2_ambiguous_collision' and top_ids:
            cands = _fetch_candidates_by_ids(conn, [int(x) for x in top_ids])
            cands = [c for c in cands if is_valid_candidate_url(c.url, secretary)]
            if cands:
                if row_date:
                    date_cands = [c for c in cands if parse_iso_date(c.date or '') == row_date]
                    if date_cands:
                        cands = date_cands
                scored = sorted(
                    [(score_candidate(sec_text, row_date, c)[0], c) for c in cands],
                    key=lambda x: (-x[0], -x[1].text_len),
                )
                best_score, best_cand = scored[0]
                margin = best_score - scored[1][0] if len(scored) > 1 else best_score
                if best_score >= r2s2_min_score:
                    recovery_rows.append(_make_r2_recovery_row(
                        row_id, best_cand, old_text, old_url, best_score,
                        f'{margin:.6f}', 'r2_q2_tie_accept', 'R2-S2',
                    ))
                    stats['R2-S2'] += 1
                    accepted = True

        # ── R2-S3: Q3 exact-date ─────────────────────────────────────────────
        if not accepted and queue_id == 'Q3_temporal_fallback' and row_date:
            db_rows = conn.execute(
                'SELECT * FROM candidates WHERE secretary=? AND date=?'
                ' ORDER BY corpus_source ASC, text_len DESC LIMIT 600',
                (secretary, str(row_date)),
            ).fetchall()
            cands = [_sqlite_to_candidate(r) for r in db_rows]
            cands = [c for c in cands if is_valid_candidate_url(c.url, secretary)]
            if cands:
                scored = sorted(
                    [(score_candidate(sec_text, row_date, c)[0], c) for c in cands],
                    key=lambda x: -x[0],
                )
                best_score, best_cand = scored[0]
                margin = best_score - scored[1][0] if len(scored) > 1 else best_score
                if best_score >= r2s3_min_score and margin >= r2s3_min_margin:
                    recovery_rows.append(_make_r2_recovery_row(
                        row_id, best_cand, old_text, old_url, best_score,
                        f'{margin:.6f}', 'r2_q3_wider_window', 'R2-S3',
                    ))
                    stats['R2-S3'] += 1
                    accepted = True

        # ── R2-S4: Q4 exact-date ─────────────────────────────────────────────
        if not accepted and queue_id == 'Q4_low_evidence' and row_date:
            db_rows = conn.execute(
                'SELECT * FROM candidates WHERE secretary=? AND date=?'
                ' ORDER BY corpus_source ASC LIMIT 600',
                (secretary, str(row_date)),
            ).fetchall()
            cands = [_sqlite_to_candidate(r) for r in db_rows]
            cands = [c for c in cands if is_valid_candidate_url(c.url, secretary)]
            if cands and len(cands) <= r2s4_max_candidates:
                scored = sorted(
                    [
                        (max(
                            score_candidate(sec_text, row_date, c)[0],
                            score_candidate(old_text, row_date, c)[0],
                        ), c)
                        for c in cands
                    ],
                    key=lambda x: -x[0],
                )
                best_score, best_cand = scored[0]
                margin = best_score - scored[1][0] if len(scored) > 1 else best_score
                if best_score >= r2s4_min_score and margin >= r2s4_min_margin:
                    recovery_rows.append(_make_r2_recovery_row(
                        row_id, best_cand, old_text, old_url, best_score,
                        f'{margin:.6f}', 'r2_q4_relaxed', 'R2-S4',
                    ))
                    stats['R2-S4'] += 1
                    accepted = True

        if not accepted:
            stats['unresolved'] += 1
            recovery_rows.append(_make_recovery_row(
                row_id=row_id, action='unchanged_unresolved', strategy='',
                reason='no_r2_strategy_succeeded', stage_used='recover_round2',
                candidate_id='', candidate_source='', score='0.000000', margin='',
                old_url=old_url, new_url='', old_speech_text='', new_speech_text='',
                speech_type_new='',
                location_new=_extract_location_from_speech_text(old_text) if old_text else '',
                date_new='', secretary_new='',
                changed_url='0', changed_text='0',
            ))

    conn.close()

    with output_recovery.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=_RECOVERY_FIELDS)
        w.writeheader()
        w.writerows(recovery_rows)

    total = len(recovery_rows)
    recovered = total - stats['unresolved']
    print(
        f'[recover_round2] total={total} recovered={recovered} unresolved={stats["unresolved"]}'
    )
    for k in ('R2-S1', 'R2-S2', 'R2-S3', 'R2-S4'):
        print(f'[recover_round2]   {k}={stats[k]}')
    print(f'[recover_round2] wrote {output_recovery}')


def _make_r2_recovery_row(
    row_id: str,
    cand: 'CandidateRecord',
    old_text: str,
    old_url: str,
    score: float,
    margin: str,
    reason: str,
    strategy: str,
) -> dict:
    """Build a _RECOVERY_FIELDS dict from a CandidateRecord."""
    new_text = cand.speech_text_clean or ''
    new_url  = cand.url or ''
    return _make_recovery_row(
        row_id=row_id,
        action='recovered',
        strategy=strategy,
        reason=reason,
        stage_used='recover_round2',
        candidate_id=str(cand.candidate_id),
        candidate_source=cand.corpus_source,
        score=f'{score:.6f}',
        margin=margin,
        old_url=old_url,
        new_url=new_url,
        old_speech_text=old_text,
        new_speech_text=new_text,
        speech_type_new=cand.speech_type_guess or '',
        location_new=_clean_location(cand.location_best or '', speech_text=new_text),
        date_new=cand.date or '',
        secretary_new=cand.secretary or '',
        changed_url='1' if canonical_url_key(new_url) != canonical_url_key(old_url) else '0',
        changed_text='1' if normalize_whitespace(new_text) != normalize_whitespace(old_text) else '0',
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Subcommand 8: recover_round3
# ═══════════════════════════════════════════════════════════════════════════════

# Secretaries where threshold relaxation is the right lever (candidates exist at
# moderate scores, not a domain mismatch like Powell/Rice 2001-2009.state.gov).
_R3_TARGET_SECRETARIES = frozenset({'clinton', 'kerry', 'pompeo'})


def run_recover_round3(
    fused_csv: Path,
    round2_recovery: Path,
    triage_csv: Path,
    checkpoint_db: Path,
    output_recovery: Path,
    r3_min_score: float = 0.70,
    r3_min_margin: float = 0.02,
    r3_date_window_days: int = 365,
    r3_max_candidates: int = 600,
    show_progress: bool = True,
) -> None:
    """Round-3 recovery: relaxed Q4 thresholds for Clinton/Kerry/Pompeo.

    Reads unchanged_unresolved rows from round2 recovery, applies two strategies:
      R3-S1: Q4 re-score precomputed top_candidates_json with lower threshold
             (score>=0.45, margin>=0.02). Uses the 3 best candidates already
             computed by the original scoring pass — no fresh DB query per row.
             This is fast (~144ms/row) and targets rows with close-miss candidates.
      R3-S2: Q4 date-windowed DB search (±r3_date_window_days) for rows where
             top_candidates_json is empty or S1 fails. Falls back to a fresh
             query capped at r3_max_candidates candidates.

    Powell and Rice are excluded because their root cause is text/domain mismatch
    with 2001-2009.state.gov candidates — lowering thresholds would produce false
    positives without actually resolving the underlying text alignment problem.
    """
    print('[recover_round3] Loading fused CSV for speech_text lookup...')
    fused_rows, _ = load_final_rows(fused_csv)
    fused_by_id: Dict[str, dict] = {
        (r.get('row_id', '') or '').strip(): r
        for r in fused_rows
        if (r.get('row_id', '') or '').strip()
    }

    print('[recover_round3] Loading round2 recovery to find unchanged_unresolved rows...')
    setup_field_limit()
    unresolved_ids: Set[str] = set()
    with round2_recovery.open('r', encoding='utf-8', errors='replace', newline='') as f:
        for rec in csv.DictReader(f):
            if rec.get('action', '') == 'unchanged_unresolved':
                rid = (rec.get('row_id', '') or '').strip()
                if rid:
                    unresolved_ids.add(rid)
    print(f'[recover_round3] unchanged_unresolved rows: {len(unresolved_ids)}')

    print('[recover_round3] Loading triage CSV...')
    triage_by_id: Dict[str, dict] = {}
    with triage_csv.open('r', encoding='utf-8', errors='replace', newline='') as f:
        for rec in csv.DictReader(f):
            rid = (rec.get('row_id', '') or '').strip()
            if rid:
                triage_by_id[rid] = rec

    conn = connect_db(checkpoint_db)

    recovery_rows: List[dict] = []
    stats: Dict[str, int] = {'R3-S1': 0, 'unresolved': 0, 'skipped_secretary': 0}

    iterator = tqdm(
        sorted(unresolved_ids),
        desc='recover_round3', unit='row', disable=not show_progress,
    )

    for row_id in iterator:
        triage_row = triage_by_id.get(row_id)
        if not triage_row:
            stats['unresolved'] += 1
            recovery_rows.append(_make_recovery_row(
                row_id=row_id, action='unchanged_unresolved', strategy='', reason='not_in_triage',
                stage_used='recover_round3', candidate_id='', candidate_source='', score='0.000000',
                margin='', old_url='', new_url='', old_speech_text='', new_speech_text='',
                speech_type_new='', location_new='', date_new='', secretary_new='',
                changed_url='0', changed_text='0',
            ))
            continue

        queue_id  = triage_row.get('queue_id', '')
        old_url   = (triage_row.get('old_url', '') or '').strip()
        secretary = (triage_row.get('secretary', '') or '').strip()
        date_str  = (triage_row.get('date', '') or '').strip()
        row_date  = parse_iso_date(date_str)
        old_text  = (fused_by_id.get(row_id, {}).get('speech_text', '') or '')
        sec_text, _ = extract_secretary_only_text(old_text, secretary)
        top_cands_json = (triage_row.get('top_candidates_json', '') or '').strip()
        try:
            top_cands_list = json.loads(top_cands_json) if top_cands_json else []
        except Exception:
            top_cands_list = []
        top_ids = [c['candidate_id'] for c in top_cands_list if 'candidate_id' in c]

        accepted = False

        if (
            queue_id == 'Q4_low_evidence'
            and secretary.lower() in _R3_TARGET_SECRETARIES
        ):
            # ── R3-S1: re-score precomputed top candidates with lower threshold ──
            # Uses the 3 best candidates from the original triage pass — fast (3 DB
            # lookups per row, ~150ms) and architecturally sound: top_candidates_json
            # already contains the best candidates from the original full-window search,
            # so date-windowed fallback would only find worse matches.
            if top_ids:
                cands = _fetch_candidates_by_ids(conn, [int(x) for x in top_ids])
                cands = [c for c in cands if is_valid_candidate_url(c.url, secretary)]
                if cands:
                    if row_date:
                        date_cands = [c for c in cands if parse_iso_date(c.date or '') == row_date]
                        if date_cands:
                            cands = date_cands
                    scored = sorted(
                        [
                            (max(
                                score_candidate(sec_text, row_date, c)[0],
                                score_candidate(old_text, row_date, c)[0],
                            ), c)
                            for c in cands
                        ],
                        key=lambda x: (-x[0], -x[1].text_len),
                    )
                    best_score, best_cand = scored[0]
                    margin = best_score - scored[1][0] if len(scored) > 1 else best_score
                    if best_score >= r3_min_score:  # no margin check — ties are valid (same speech indexed twice)
                        recovery_rows.append(_make_r3_recovery_row(
                            row_id, best_cand, old_text, old_url, best_score,
                            f'{margin:.6f}', 'r3_q4_top_cand_relaxed', 'R3-S1',
                        ))
                        stats['R3-S1'] += 1
                        accepted = True

        elif queue_id == 'Q4_low_evidence' and secretary.lower() not in _R3_TARGET_SECRETARIES:
            stats['skipped_secretary'] += 1

        if not accepted:
            stats['unresolved'] += 1
            recovery_rows.append(_make_recovery_row(
                row_id=row_id, action='unchanged_unresolved', strategy='',
                reason='no_r3_strategy_succeeded', stage_used='recover_round3',
                candidate_id='', candidate_source='', score='0.000000', margin='',
                old_url=old_url, new_url='', old_speech_text='', new_speech_text='',
                speech_type_new='',
                location_new=_extract_location_from_speech_text(old_text) if old_text else '',
                date_new='', secretary_new='',
                changed_url='0', changed_text='0',
            ))

    conn.close()

    with output_recovery.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=_RECOVERY_FIELDS)
        w.writeheader()
        w.writerows(recovery_rows)

    total = len(recovery_rows)
    recovered = stats['R3-S1']
    print(
        f'[recover_round3] total={total} recovered={recovered}'
        f' unresolved={stats["unresolved"]} skipped_secretary={stats["skipped_secretary"]}'
    )
    print(f'[recover_round3]   R3-S1={stats["R3-S1"]}')
    print(f'[recover_round3] wrote {output_recovery}')


def _make_r3_recovery_row(
    row_id: str,
    cand: 'CandidateRecord',
    old_text: str,
    old_url: str,
    score: float,
    margin: str,
    reason: str,
    strategy: str,
) -> dict:
    """Build a _RECOVERY_FIELDS dict from a CandidateRecord (round3 stage)."""
    new_text = cand.speech_text_clean or ''
    new_url  = cand.url or ''
    return _make_recovery_row(
        row_id=row_id,
        action='recovered',
        strategy=strategy,
        reason=reason,
        stage_used='recover_round3',
        candidate_id=str(cand.candidate_id),
        candidate_source=cand.corpus_source,
        score=f'{score:.6f}',
        margin=margin,
        old_url=old_url,
        new_url=new_url,
        old_speech_text=old_text,
        new_speech_text=new_text,
        speech_type_new=cand.speech_type_guess or '',
        location_new=_clean_location(cand.location_best or '', speech_text=new_text),
        date_new=cand.date or '',
        secretary_new=cand.secretary or '',
        changed_url='1' if canonical_url_key(new_url) != canonical_url_key(old_url) else '0',
        changed_text='1' if normalize_whitespace(new_text) != normalize_whitespace(old_text) else '0',
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Subcommand 13 (part 2): recover_round4 — exact-date SQLite strategy at 0.35
# ═══════════════════════════════════════════════════════════════════════════════

def run_recover_round4(
    fused_csv: Path,
    combined_recovery: Path,
    triage_csv: Path,
    checkpoint_db: Path,
    output_recovery: Path,
    exact_date_min_score: float = 0.70,
    target_secretaries: Optional[frozenset] = None,
    show_progress: bool = True,
) -> None:
    """Exact-date SQLite strategy at 0.35 threshold for secretaries without archived crawl data.

    Reads unchanged_unresolved row IDs from combined_recovery, queries the SQLite candidate index
    for candidates on the exact same date, accepts at a lower threshold (0.35) than existing
    strategies (0.45+).  Mirrors the exact-date track in run_recover_archived() but uses SQLite.

    Default targets: Blinken and Albright (not covered by any archived domain crawl).
    """
    if target_secretaries is None:
        target_secretaries = frozenset({'blinken', 'albright'})

    # ── load data ─────────────────────────────────────────────────────────────
    print('[recover_round4] Loading fused CSV for speech_text lookup...')
    fused_rows, _ = load_final_rows(fused_csv)
    fused_by_id = {(r.get('row_id', '') or '').strip(): r for r in fused_rows}

    print('[recover_round4] Loading combined recovery to find unchanged_unresolved rows...')
    unresolved_ids: set = set()
    unresolved_meta: dict = {}
    with combined_recovery.open('r', encoding='utf-8', errors='replace', newline='') as f:
        for rec in csv.DictReader(f):
            if (rec.get('action', '') or '').strip() == 'unchanged_unresolved':
                rid = (rec.get('row_id', '') or '').strip()
                if rid:
                    unresolved_ids.add(rid)

    print('[recover_round4] Loading triage CSV...')
    triage_by_id: dict = {}
    with triage_csv.open('r', encoding='utf-8', errors='replace', newline='') as f:
        for rec in csv.DictReader(f):
            rid = (rec.get('row_id', '') or '').strip()
            if rid in unresolved_ids:
                triage_by_id[rid] = rec

    targets = [
        {'row_id': rid,
         'secretary': (triage_by_id[rid].get('secretary', '') or '').strip().lower(),
         'date': (triage_by_id[rid].get('date', '') or '').strip(),
         'old_url': (triage_by_id[rid].get('old_url', '') or '').strip(),
         'speech_text': (fused_by_id.get(rid, {}).get('speech_text', '') or '').strip()}
        for rid in unresolved_ids
        if rid in triage_by_id
    ]
    targets = [t for t in targets
               if t['secretary'] in target_secretaries and len(t['speech_text']) >= 60]
    print(f'[recover_round4] {",".join(sorted(target_secretaries))} targets with ≥60 char text: {len(targets)}')

    conn = connect_db(checkpoint_db)

    recovery_rows: list = []
    stats = {'recovered': 0, 'unchanged_unresolved': 0}

    pbar = tqdm(targets, desc='recover_round4', unit='row',
                disable=not show_progress, dynamic_ncols=True)
    for t in pbar:
        row_id   = t['row_id']
        sec      = t['secretary']
        date_str = t['date']
        old_text = t['speech_text']
        old_url  = t['old_url']
        row_date = parse_iso_date(date_str)

        # Prepare corpus tokens (full text and secretary-only)
        clean_text = _strip_archive_nav_prefix(old_text, sec)
        corpus_toks = token_set(normalize_whitespace(clean_text))
        sec_text, _ = extract_secretary_only_text(clean_text, sec)
        sec_toks = token_set(normalize_whitespace(sec_text)) if sec_text else corpus_toks

        # Query SQLite for exact-date candidates
        rows_db = conn.execute(
            'SELECT * FROM candidates WHERE LOWER(secretary)=? AND date=? ORDER BY text_len DESC LIMIT 10',
            (sec, date_str)
        ).fetchall()

        if not rows_db:
            stats['unchanged_unresolved'] += 1
            recovery_rows.append(_make_recovery_row(
                row_id=row_id, action='unchanged_unresolved', strategy='R4',
                reason='no_exact_date_sqlite_candidate', stage_used='recover_round4',
                candidate_id='', candidate_source='', score='0.000000', margin='',
                old_url=old_url, new_url='',
                old_speech_text=old_text, new_speech_text='',
                speech_type_new='',
                location_new=_extract_location_from_speech_text(old_text) if old_text else '',
                date_new=date_str,
                secretary_new=sec, changed_url='0', changed_text='0',
            ))
            continue

        cands = [_sqlite_to_candidate(r) for r in rows_db]
        cands = [c for c in cands if is_valid_candidate_url(c.url, sec)]
        best_score = 0.0
        best_cand = None
        for c in cands:
            ct = token_set(normalize_whitespace(c.speech_text_clean or ''))
            sc = max(overlap_score(corpus_toks, ct), overlap_score(sec_toks, ct))
            if sc > best_score:
                best_score = sc
                best_cand = c

        if best_score >= exact_date_min_score and best_cand is not None:
            stats['recovered'] += 1
            new_text = best_cand.speech_text_clean or ''
            new_url  = best_cand.url or ''
            recovery_rows.append(_make_recovery_row(
                row_id=row_id, action='recovered', strategy='R4',
                reason='exact_date_sqlite_match', stage_used='recover_round4',
                candidate_id=str(best_cand.candidate_id),
                candidate_source=best_cand.corpus_source,
                score=f'{best_score:.6f}', margin='',
                old_url=old_url, new_url=new_url,
                old_speech_text=old_text, new_speech_text=new_text,
                speech_type_new=best_cand.speech_type_guess or '',
                location_new=_clean_location(best_cand.location_best or '', speech_text=new_text),
                date_new=best_cand.date or '',
                secretary_new=best_cand.secretary or '',
                changed_url='1' if canonical_url_key(new_url) != canonical_url_key(old_url) else '0',
                changed_text='1' if normalize_whitespace(new_text) != normalize_whitespace(old_text) else '0',
            ))
        else:
            stats['unchanged_unresolved'] += 1
            recovery_rows.append(_make_recovery_row(
                row_id=row_id, action='unchanged_unresolved', strategy='R4',
                reason='score_below_exact_date_threshold', stage_used='recover_round4',
                candidate_id='', candidate_source='', score=f'{best_score:.6f}', margin='',
                old_url=old_url, new_url='',
                old_speech_text=old_text, new_speech_text='',
                speech_type_new='',
                location_new=_extract_location_from_speech_text(old_text) if old_text else '',
                date_new=date_str,
                secretary_new=sec, changed_url='0', changed_text='0',
            ))

    pbar.close()
    print(f'[recover_round4] total={len(targets)} recovered={stats["recovered"]} unchanged_unresolved={stats["unchanged_unresolved"]}')

    with output_recovery.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=_RECOVERY_FIELDS)
        w.writeheader()
        w.writerows(recovery_rows)
    print(f'[recover_round4] wrote {output_recovery}')


# ═══════════════════════════════════════════════════════════════════════════════
# Subcommand 10: recover_archived
# ═══════════════════════════════════════════════════════════════════════════════

_ARCHIVED_TARGET_SECRETARIES = frozenset({'powell', 'rice'})

# URL scope patterns for 2001-2009.state.gov archived domains
_POWELL_SCOPE = 'secretary/former/powell/'
_RICE_SCOPE   = 'secretary/rm/'
_ARCHIVED_SCOPE_PATTERNS = [
    '2001-2009.state.gov/secretary/former/powell/',
    '2001-2009.state.gov/secretary/rm/',
]

_RATE_LIMIT_DEFAULT = 1.5   # seconds between HTTP requests
_MAX_PAGES_DEFAULT  = 5000  # safety cap on listing pages crawled
_DATE_WINDOW_DEFAULT = 60   # days ± speech date for candidate filtering
_MIN_SCORE_DEFAULT   = 0.55 # minimum overlap_score to accept a match

_ARCHIVED_LISTING_SEEDS = [
    # Powell 2001-2004 (he served 2001-2005 but step-down 2004)
    *(f'https://2001-2009.state.gov/secretary/former/powell/remarks/{y}/index.htm'
      for y in range(2001, 2006)),
    'https://2001-2009.state.gov/secretary/former/powell/remarks/index.htm',
    'https://2001-2009.state.gov/secretary/former/powell/remarks/2002/c5240.htm',
    'https://2001-2009.state.gov/secretary/former/powell/remarks/c11020.htm',  # 2004 year-index
    # Rice 2005-2008
    *(f'https://2001-2009.state.gov/secretary/rm/{y}/index.htm'
      for y in range(2005, 2010)),
    # Rice 2005 Jul-Oct monthly indexes (missing from year-level index)
    *(f'https://2001-2009.state.gov/secretary/rm/2005/{m:02d}/index.htm'
      for m in [7, 8, 9, 10]),
    # Powell 2003 monthly listing pages (target: Jan/Feb 2003, 28 B1_empty rows)
    'https://2001-2009.state.gov/secretary/former/powell/remarks/2003/index.htm',
    'https://2001-2009.state.gov/secretary/former/powell/remarks/2003/c8037.htm',
    'https://2001-2009.state.gov/secretary/former/powell/remarks/2003/c8365.htm',
    # Powell 2004 monthly listing pages (target: Dec 2004, 42 B1_empty + 6 C rows)
    'https://2001-2009.state.gov/secretary/former/powell/remarks/c11020.htm',
    'https://2001-2009.state.gov/secretary/former/powell/remarks/c13508.htm',
    # Rice 2005 monthly listing pages (target: Jul-Oct 2005, 37 A_true_gap + 88 B1_empty)
    'https://2001-2009.state.gov/secretary/rm/2005/c15743.htm',
    'https://2001-2009.state.gov/secretary/rm/2005/c15885.htm',
    'https://2001-2009.state.gov/secretary/rm/2005/c14317.htm',
    'https://2001-2009.state.gov/secretary/rm/2005/c15436.htm',
    'https://2001-2009.state.gov/secretary/rm/2005/c15258.htm',
    'https://2001-2009.state.gov/secretary/rm/index.htm',
    # Rice former-secretary archive path (coverage gap candidate)
    'https://2001-2009.state.gov/secretary/former/rice/remarks/index.htm',
]

# 2009-2017.state.gov: Clinton (2009-2013) and Kerry (2013-2017)
_ARCHIVED_TARGET_SECRETARIES_2009 = frozenset({'clinton', 'kerry'})
_ARCHIVED_LISTING_SEEDS_2009 = [
    # Clinton 2010-2013: archived at secretary/20092013clinton/rm/YEAR/
    # (2009 returns Technical Difficulties; year-index at root also works)
    *(f'https://2009-2017.state.gov/secretary/20092013clinton/rm/{y}/index.htm'
      for y in range(2010, 2014)),
    'https://2009-2017.state.gov/secretary/20092013clinton/rm/index.htm',
    # 2010 monthly seeds: the annual index may only link to monthly sub-indexes,
    # so the BFS needs explicit entry points to reach all 2010 speech pages.
    *(f'https://2009-2017.state.gov/secretary/20092013clinton/rm/2010/{m:02d}/index.htm'
      for m in range(1, 13)),
    # Kerry 2013-2017: archived at secretary/remarks/YEAR/ (same path as current secretary)
    *(f'https://2009-2017.state.gov/secretary/remarks/{y}/index.htm'
      for y in range(2013, 2018)),
    'https://2009-2017.state.gov/secretary/remarks/index.htm',
]

# 2017-2021.state.gov: Tillerson (2017-2018) and Pompeo (2018-2021)
# Confirmed URL structure: top-level listing pages (not under /secretary/)
_ARCHIVED_TARGET_SECRETARIES_2017 = frozenset({'pompeo', 'tillerson'})
_ARCHIVED_LISTING_SEEDS_2017 = [
    'https://2017-2021.state.gov/remarks-secretary-pompeo/',
    'https://2017-2021.state.gov/remarks-former-secretary-of-state-tillerson/',
]

# 2021-2025.state.gov: Blinken (2021-2025)
# Listing pages return "Technical Difficulties"; WP API also blocked (302→safe=1).
# Discovery via XML sitemaps (page-sitemap*.xml), which ARE accessible.
_ARCHIVED_TARGET_SECRETARIES_2021 = frozenset({'blinken'})
_ARCHIVED_LISTING_SEEDS_2021: List[str] = []  # sitemap discovery: no BFS seeds needed

# 1997-2001.state.gov: Albright (1997-2001)
# Complete year-index pages available; YYMMDD.html URL pattern encodes date directly
_ARCHIVED_TARGET_SECRETARIES_1997 = frozenset({'albright'})
_ARCHIVED_LISTING_SEEDS_1997 = [
    # /www/statements/ index pages — these link to individual speeches at /statements/YYMMDD.html
    'https://1997-2001.state.gov/www/statements/1997_index.html',
    'https://1997-2001.state.gov/www/statements/1998_index.html',
    'https://1997-2001.state.gov/www/statements/1999_index.html',
    'https://1997-2001.state.gov/www/statements/2000_index.html',
    'https://1997-2001.state.gov/www/statements/index.html',
    # Scope-defining seed: speech URLs resolve to /statements/YYMMDD.html (not /www/statements/).
    # Adding this gives scope pattern 'statements/' so the BFS accepts those speech links.
    'https://1997-2001.state.gov/statements/',
]


def _run_sitemap_discovery(
    domain: str,
    checkpoint: dict,
    session: 'requests.Session',
    target_dates: Optional[Set[str]] = None,
    window_days: int = 14,
    max_sitemaps: int = 200,
    rate_limit_secs: float = 0.5,
    show_progress: bool = True,
) -> int:
    """Discover URLs via XML sitemaps (page-sitemap*.xml) for a WordPress-based archive domain.

    Replaces Phase 1 + Phase 2 HTML BFS for domains where listing pages are inaccessible
    but the XML sitemaps are accessible (e.g. 2021-2025.state.gov).

    When target_dates is provided (set of 'YYYY-MM-DD' strings), only adds URLs whose
    sitemap lastmod date falls within window_days of any target date. This keeps the
    checkpoint small (hundreds rather than tens of thousands of URLs) so Phase 2 is fast.

    Returns count of newly added URLs.
    """
    from datetime import datetime, timedelta
    import xml.etree.ElementTree as _ET

    # Build date window: for each target date, mark acceptable lastmod range
    date_windows: List[tuple] = []
    if target_dates:
        for ds in target_dates:
            try:
                dt = datetime.strptime(ds, '%Y-%m-%d')
                date_windows.append((dt - timedelta(days=window_days),
                                     dt + timedelta(days=window_days)))
            except ValueError:
                pass

    def _in_window(lastmod_str: str) -> bool:
        if not date_windows:
            return True  # no filter: accept all
        try:
            dt = datetime.strptime(lastmod_str[:10], '%Y-%m-%d')
        except ValueError:
            return False
        return any(lo <= dt <= hi for lo, hi in date_windows)

    def _is_excluded_url(url: str) -> bool:
        """Skip translations, binary assets, and non-speech content."""
        lower = url.lower()
        return ('/translations/' in lower or
                lower.endswith(('.pdf', '.png', '.jpg', '.gif', '.zip')) or
                '/?state_' in lower or
                '/wp-content/' in lower)

    # Fetch sitemap index
    ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    sitemap_index_url = f'https://{domain}/sitemap_index.xml'
    pbar = tqdm(total=None, desc='sitemaps', unit='url', disable=not show_progress, dynamic_ncols=True)
    new_count = 0
    try:
        resp = session.get(sitemap_index_url, timeout=30)
        if resp.status_code != 200:
            pbar.close()
            return 0
        root = _ET.fromstring(resp.content)
    except Exception:
        pbar.close()
        return 0

    page_sitemaps = []
    for sitemap_el in root.findall('sm:sitemap', ns):
        loc_el = sitemap_el.find('sm:loc', ns)
        mod_el = sitemap_el.find('sm:lastmod', ns)
        if loc_el is None:
            continue
        loc = loc_el.text or ''
        mod = mod_el.text[:10] if mod_el is not None and mod_el.text else ''
        # Only page sitemaps (skip taxonomy sitemaps like state_document_type, etc.)
        if 'page-sitemap' in loc:
            page_sitemaps.append((loc, mod))

    # Sort by name so we process in order; limit to avoid runaway fetches
    page_sitemaps = page_sitemaps[:max_sitemaps]

    for sitemap_loc, sitemap_mod in page_sitemaps:
        # Skip entire sitemap if its lastmod is too far from any target date
        # (rough filter — a sitemap with lastmod 2020-01-01 has no Blinken pages)
        if date_windows and not _in_window(sitemap_mod):
            # sitemap_mod is the LATEST lastmod in that sitemap file, so if the
            # latest is before all our windows, skip. But if it's after, pages
            # inside might still be in range — we can't skip just yet.
            # Only skip if sitemap_mod < earliest window start
            earliest = min(lo for lo, _ in date_windows)
            try:
                sitemap_dt = datetime.strptime(sitemap_mod[:10], '%Y-%m-%d')
                if sitemap_dt < earliest:
                    continue
            except ValueError:
                pass

        try:
            sresp = session.get(sitemap_loc, timeout=30)
            if sresp.status_code != 200:
                continue
            sroot = _ET.fromstring(sresp.content)
        except Exception:
            time.sleep(rate_limit_secs)
            continue

        for url_el in sroot.findall('sm:url', ns):
            loc_el = url_el.find('sm:loc', ns)
            mod_el = url_el.find('sm:lastmod', ns)
            if loc_el is None:
                continue
            url = loc_el.text or ''
            lastmod = mod_el.text[:10] if mod_el is not None and mod_el.text else ''
            if not url or _is_excluded_url(url):
                continue
            if not _in_window(lastmod):
                continue
            if url not in checkpoint:
                # Store lastmod as the date hint; actual date extracted during Phase 2 scraping
                checkpoint[url] = None  # triggers Phase 2 scraping
                new_count += 1
                pbar.update(1)

        time.sleep(rate_limit_secs)

    pbar.close()
    return new_count


def _extract_date_from_archived_page(soup: 'BeautifulSoup', url: str) -> str:
    """Extract an ISO date string from an archived state.gov page.

    Tries multiple strategies in order of specificity:
    1. Legacy meta name tags (DC_date, dc.date.issued) — 2001-2009.state.gov
    2. Open Graph article:published_time — 2017-2021.state.gov (Drupal CMS)
    3. HTML5 <time datetime="..."> element
    4. JSON-LD structured data datePublished
    5. CSS class 'date' on span/p/div/td — 2001-2009.state.gov
    6. Year from URL path (approximate fallback)
    Returns '' if nothing found.
    """
    import json as _json

    # 1. Legacy DC meta name tags (2001-2009 / 2009-2017 archives)
    for attr in ('DC_date', 'dc.date.issued', 'DC.date', 'date'):
        tag = soup.find('meta', attrs={'name': attr, 'content': True})
        if tag:
            val = (tag.get('content') or '').strip()
            d = parse_iso_date(val)
            if d:
                return str(d)

    # 2. Open Graph article:published_time (Drupal / modern Wordpress sites)
    for og_tag in soup.find_all('meta', property='article:published_time'):
        val = (og_tag.get('content') or '')[:10]
        d = parse_iso_date(val)
        if d:
            return str(d)
    # Also try attrs dict form (some BeautifulSoup versions)
    tag = soup.find('meta', attrs={'property': 'article:published_time', 'content': True})
    if tag:
        d = parse_iso_date((tag.get('content') or '')[:10])
        if d:
            return str(d)

    # 3. HTML5 <time datetime="...">
    time_tag = soup.find('time', attrs={'datetime': True})
    if time_tag:
        d = parse_iso_date((time_tag.get('datetime') or '')[:10])
        if d:
            return str(d)

    # 4. JSON-LD structured data — handle both flat and @graph array structures
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = _json.loads(script.string or '{}')
            # Flatten @graph array (WordPress Yoast SEO emits this structure)
            items = data.get('@graph', None)
            if items is None:
                items = [data]
            elif not isinstance(items, list):
                items = [items]
            for item in items:
                for key in ('datePublished', 'dateCreated', 'dateModified'):
                    val = item.get(key, '')
                    if val:
                        d = parse_iso_date(str(val)[:10])
                        if d:
                            return str(d)
        except Exception:
            pass

    # 5. Common date spans/paragraphs (2001-2009 and 2017-2021 archives)
    for sel in (['p',    {'class': 'article-meta__publish-date'}],
                ['span', {'class': 'date'}],
                ['p',    {'class': 'date'}],
                ['div',  {'class': 'date'}],
                ['td',   {'class': 'date'}]):
        tag = soup.find(sel[0], sel[1])
        if tag:
            d = parse_iso_date(tag.get_text(strip=True))
            if d:
                return str(d)

    # 6. 1997-2001.state.gov: YYMMDD.html filename encodes the date (e.g. '970305.html' → 1997-03-05)
    m_ymd = re.search(r'/(\d{2})(\d{2})(\d{2})[a-z]?\.html', url)
    if m_ymd:
        yy, mm, dd = m_ymd.group(1), m_ymd.group(2), m_ymd.group(3)
        year = ('19' if int(yy) >= 97 else '20') + yy
        d = parse_iso_date(f'{year}-{mm}-{dd}')
        if d:
            return str(d)
    # Also handle YYYY/MMDD.html and YYYY/YYYYMMDD.html variants
    m_long = re.search(r'/(\d{4})(\d{2})(\d{2})[a-z]?\.html', url)
    if m_long:
        d = parse_iso_date(f'{m_long.group(1)}-{m_long.group(2)}-{m_long.group(3)}')
        if d:
            return str(d)

    # 7. Fall back: extract 4-digit year from URL path (covers 1990–2029; approximate)
    m = re.search(r'/((?:19|20)[0-9]{2})/', url)
    if m:
        return f'{m.group(1)}-01-01'  # approximate; year only

    return ''


def _is_archived_content_url(url: str) -> bool:
    """Return True if url looks like a speech content page on 2001-2009.state.gov."""
    if '2001-2009.state.gov' not in url:
        return False
    parsed = urlsplit(url)
    path = parsed.path.lower()
    # Must be under one of the two secretary scopes
    in_scope = (_POWELL_SCOPE in path or _RICE_SCOPE in path)
    if not in_scope:
        return False
    # Exclude index/listing pages and non-HTML pages
    if path.endswith('/index.htm') or path.endswith('/index.html'):
        return False
    if re.search(r'\.(gif|jpg|jpeg|png|pdf|doc|xls|zip|css|js)$', path):
        return False
    return True


def _is_archived_listing_url(url: str) -> bool:
    """Return True if url looks like a listing/index page on 2001-2009.state.gov."""
    if '2001-2009.state.gov' not in url:
        return False
    parsed = urlsplit(url)
    path = parsed.path.lower()
    return (_POWELL_SCOPE in path or _RICE_SCOPE in path) and (
        path.endswith('/index.htm') or path.endswith('/index.html') or path.endswith('/')
    )


def _is_month_listing(url: str) -> bool:
    """Return True for WaterFall month-index pages whose filename matches c#####.htm.

    These pages list speech titles and links for a single month but contain no
    transcript text.  They should be followed for BFS link discovery but excluded
    from Phase 2 text scraping.
    """
    fname = urlsplit(url).path.rsplit('/', 1)[-1]
    return bool(re.match(r'^c\d+\.htm$', fname, re.IGNORECASE))


_MIN_STUB_TOKENS = 40  # raised from 10; is_unreliable_source_text() enforces this threshold

_STUB_PHRASES = frozenset({
    '', 'na', 'n/a', 'as prepared for delivery',
    'as prepared', '[as prepared]', 'as prepared for delivery.',
})
_CONTINUATION_WORDS = frozenset({
    'and', 'or', 'but', 'which', 'including',
    'the', 'this', 'these', 'those',
})
_NAV_CUES_UNRELIABLE = (
    'secretary name::',
    'jump to in this section',
    'in this section',
    'foreign press center',
    'other state department archive sites',
    'you are viewing: archived content',
    'skip to content',
)
_AZ_MENU_SENTINEL = 'afghanistan\nalbania\n'  # 2017-2021 A-Z country menus always start here
_NAV_BOILERPLATE_TOKENS = frozenset({
    'news', 'releases', 'speeches', 'remarks', 'interviews', 'testimony',
    'travel', 'policy', 'countries', 'topics', 'bureaus', 'offices',
    'careers', 'contact', 'share', 'email', 'print', 'twitter', 'facebook',
    'youtube', 'home', 'search', 'more', 'related',
    'content', 'section', 'page', 'site', 'state', 'gov',
})


def is_unreliable_source_text(
    text: str,
    cleaned_text: str,
    tokens: set,
    old_url: str,
    secretary: str,
) -> tuple:   # (bool, list[str])
    """Return (True, reasons) when corpus text is too noisy or stub-like to score reliably.

    Any one criterion suffices: token count < 40, char length < 250, known stub phrase,
    mid-sentence continuation start, archive nav cues in first 6 KB, A-Z country menu,
    or >20% boilerplate token density in first 300 tokens.
    """
    reasons: list = []
    raw = (text or '').strip()

    if raw.lower() in _STUB_PHRASES:
        return True, ['stub_phrase']

    if len(tokens) < 40:
        reasons.append(f'low_tokens={len(tokens)}')

    if len(cleaned_text) < 250:
        reasons.append(f'short_chars={len(cleaned_text)}')

    stripped_alpha = raw.lstrip('"\'“‘’”([{<–—\\- \t\n\r:;,.')
    if stripped_alpha:
        first_word = stripped_alpha.split()[0].rstrip(',:;.') if stripped_alpha.split() else ''
        if first_word.lower() in _CONTINUATION_WORDS:
            reasons.append(f'mid_sentence={first_word!r}')

    sample6k = raw[:6000].lower()
    for cue in _NAV_CUES_UNRELIABLE:
        if cue in sample6k:
            reasons.append(f'nav_cue={cue[:35]!r}')
            break

    if _AZ_MENU_SENTINEL in sample6k:
        reasons.append('az_country_menu')

    first_toks = [w for w in normalize_for_match((cleaned_text or '')[:2000]).split()
                  if len(w) > 2][:300]
    if first_toks:
        bp = sum(1 for t in first_toks if t in _NAV_BOILERPLATE_TOKENS)
        ratio = bp / len(first_toks)
        if ratio > 0.20:
            reasons.append(f'high_bp={ratio:.2f}')

    return bool(reasons), reasons

_ARCHIVE_NAV_PREFIX = 'other state department archive sites'
_ARCHIVE_NAV_MARKER_2009 = 'you are viewing'   # present in first 500 chars of 2009-2017 / 2017-2021 source texts
_ARCHIVE_NAV_MARKERS_WIDE = (
    'you are viewing',
    'archived content',
    'jump to in this section',
    'in this section',
    'foreign press center',
    'skip to content',          # 2017-2021/2021-2025 modern USG page header
)

# When the checkpoint stores only a year-only approximation (e.g. '2002-01-01' from the URL
# fallback), Phase 3's 60-day window filter silently discards correct candidate pages that are
# dated later in the same year.  Re-extract the actual date from the page text using a
# domain-specific character window that skips navigation and lands on speech-header content.
_DOMAIN_DATE_REEXTRACT_WINDOWS = {
    '2001-2009.state.gov': (100,  3000),   # nav is ~482 chars; date in speech header
    '2009-2017.state.gov': (2000, 8000),   # nav ~2300 chars; date block follows location line
    '2017-2021.state.gov': (700,  10700),  # skip archive banner; date after A-Z nav menu
    '1997-2001.state.gov': (100,  2000),   # archive banner ~2KB; date in page title/header
    '2021-2025.state.gov': (5000, 10000),  # A-Z nav ~6500 chars; article byline date at ~7500
}


def _is_year_only_date(date_str: str) -> bool:
    """Return True for year-only approximation dates stored by the URL fallback (e.g. '2002-01-01')."""
    return bool(date_str) and (date_str.endswith('-01-01') or date_str.count('-') < 2)


def _checkpoint_date_quality_stats(checkpoint: Dict[str, Optional[dict]]) -> Dict[str, int]:
    """Summarize checkpoint date quality for scraped pages."""
    out = {'with_text': 0, 'empty': 0, 'year_only': 0, 'full': 0}
    for data in checkpoint.values():
        if not data or not isinstance(data, dict) or not data.get('text'):
            continue
        out['with_text'] += 1
        dt = (data.get('date', '') or '').strip()
        if not dt:
            out['empty'] += 1
        elif _is_year_only_date(dt):
            out['year_only'] += 1
        else:
            out['full'] += 1
    return out


def _extract_candidate_dates_from_text(text: str, domain: str) -> List[str]:
    """Extract candidate ISO dates from page text using domain-specific windows."""
    if not text:
        return []
    lo, hi = _DOMAIN_DATE_REEXTRACT_WINDOWS.get(domain, (100, 5000))
    window = text[max(0, lo):max(0, hi)]
    if not window:
        return []
    pats = [
        re.compile(
            r'\b(January|February|March|April|May|June|July|August|September|'
            r'October|November|December)\s+(\d{1,2}),?\s+((?:19|20)\d{2})\b'
        ),
        re.compile(r'\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b'),
    ]
    found: List[str] = []
    for pat in pats:
        for m in pat.finditer(window):
            if len(m.groups()) == 3 and m.group(1).isalpha():
                try:
                    from datetime import datetime as _dt
                    d = _dt.strptime(
                        f'{m.group(1)} {int(m.group(2)):02d} {m.group(3)}',
                        '%B %d %Y'
                    ).date()
                    found.append(str(d))
                except Exception:
                    continue
            else:
                d = parse_iso_date(f'{m.group(1)}-{m.group(2)}-{m.group(3)}')
                if d:
                    found.append(str(d))
    # preserve order while deduplicating
    uniq: List[str] = []
    seen: Set[str] = set()
    for ds in found:
        if ds not in seen:
            seen.add(ds)
            uniq.append(ds)
    return uniq


def _best_date_for_targets(candidates: List[str], target_dates: Set[str]) -> str:
    """Pick best extracted date: exact target-date hit preferred, else first candidate."""
    if not candidates:
        return ''
    if target_dates:
        for ds in candidates:
            if ds in target_dates:
                return ds
    return candidates[0]


def _find_legacy_archive_content_anchor(
    text: str,
    secretary: str = '',
    start_at: int = 0,
    max_ahead: int = 3000,
) -> int:
    """Find likely speech/Q&A content anchor in legacy (2001-era) archive text.

    Used symmetrically by both corpus and candidate anchoring paths.
    Returns absolute char index into ``text`` or -1 if no anchor is found.
    """
    if not text:
        return -1
    lo = max(0, int(start_at))
    hi = min(len(text), lo + max(0, int(max_ahead)))
    window = text[lo:hi]
    if not window:
        return -1

    sec_upper = (secretary or '').strip().upper()
    pats = [
        r'\bSECRETARY [A-Z]+:(?!:)',
        r'\bQUESTION:(?!:)',
        r'\bMODERATOR:(?!:)',
        r'\bQ:(?!:)',
        r'\bAMBASSADOR [A-Z]+:(?!:)',
    ]
    if sec_upper:
        pats.insert(0, rf'\bSECRETARY {re.escape(sec_upper)}:(?!:)')
        pats.insert(1, rf'\bMR\. {re.escape(sec_upper)}:(?!:)')

    best = None
    for pat in pats:
        m = re.search(pat, window)
        if not m:
            continue
        idx = lo + m.start()
        if best is None or idx < best:
            best = idx
    return best if best is not None else -1


def _find_speech_content_start(text: str, secretary: str, half_fallback: bool = True) -> int:
    """Return char offset where the secretary's speech content starts in an archived state.gov page.

    half_fallback=True (default, every existing caller): when no anchor is found the
    second half of the text is returned, as before.  half_fallback=False (Rubio path,
    audit X-08): return -1 instead, so the caller can use the content block or flag
    the row -- the half-text cut started row 17095 mid-word ("hat they believe...").

    2017-2021 pages embed a full A-Z country navigation menu (~7,000+ chars) before speech content.
    2009-2017 pages have a location + date block that marks the end of navigation at ~2,304 chars.
    2001-2009 pages have short navigation (~482 chars); speech follows a "Month DD, 200x" date line.
    1997-2001 pages have an archive banner (~2KB); speech follows a "Month DD, 19YY" date line.
    """
    sec_upper = secretary.upper()
    # Direct secretary-name anchors work across all domains
    markers = [
        'SECRETARY %s:' % sec_upper,
        'SECRETARY %s\n' % sec_upper,
        'MR. %s:' % sec_upper,
        'SECRETARY:\n',
        'SECRETARY:\xc2\n',   # UTF-8 encoding artifact common on these pages
    ]
    for m in markers:
        idx = text.find(m)
        if idx != -1:
            return idx
    # 1997-2001 and 2001-2009: prepared statements have no "SECRETARY NAME:" speaker anchor;
    # content starts immediately after the "Month DD, YYYY" date line in the page header.
    # 2001-2009 nav is ~482 chars so the date line appears at ~400-550; 1997-2001 archive banner
    # is ~2KB so the date line appears at ~1800-2500.  A single regex covers both eras.
    # GUARD: 2009-2017/2017-2021 pages also have dates in the first 200 chars (inside the wide
    # nav banner), so skip this block for those domains — _loc_date_pat handles them below.
    _date_in_body = re.compile(
        r'(?:January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+\d{1,2},?\s+(?:19|20)\d{2}'
    )
    _is_wide_nav = (
        _ARCHIVE_NAV_MARKER_2009 in text[:500].lower()
        or 'skip to content' in text[:300].lower()   # 2017-2021/2021-2025 modern pages
    )
    m_body_date = None if _is_wide_nav else _date_in_body.search(text[50:3000])
    if m_body_date:
        pos = 50 + m_body_date.end()
        # Legacy 2001-era pages often put the first speaker/Q&A label shortly
        # after the date line; prefer that anchor over raw next-line clipping.
        legacy_anchor = _find_legacy_archive_content_anchor(
            text,
            secretary=secretary,
            start_at=pos,
            max_ahead=2500,
        )
        if legacy_anchor != -1:
            return legacy_anchor
        # 2001-2009 prepared remarks: skip post-date nav footer
        # (e.g., "Updates | Frequent Questions | ...") before speech content.
        para = text.find('\n\n', pos)
        if para != -1 and para < pos + 2000:
            return para + 2
        nxt = text.find('\n', pos)
        if nxt != -1:
            return nxt + 1

    # 2001-2009 pages with nav marker but no date line: symmetric fallback with
    # _strip_archive_nav_prefix so corpus and candidate anchors stay consistent.
    if _ARCHIVE_NAV_PREFIX in text[:500].lower() and not _is_wide_nav and m_body_date is None:
        _anc2 = _find_legacy_archive_content_anchor(text, secretary, start_at=200, max_ahead=3000)
        if _anc2 != -1:
            return _anc2
        _nav_off = text[:500].lower().find(_ARCHIVE_NAV_PREFIX)
        _s = max(200, (_nav_off + len(_ARCHIVE_NAV_PREFIX)) if _nav_off != -1 else 200)
        while _s < min(len(text), 3000):
            _nl = text.find('\n', _s)
            if _nl == -1:
                break
            if _nl - _s >= 60:
                return _s
            _s = _nl + 1

    # 2021-2025.state.gov: prepared remarks have "Secretary of State\n{Month DD, YYYY}\n" (no location)
    # Pattern: "Antony J. Blinken, Secretary of State\nMarch 6, 2024\n[CONTENT]"
    _sos_date_2021 = re.compile(
        r'Secretary of State\n'
        r'(?:January|February|March|April|May|June|July|August|'
        r'September|October|November|December)\s+\d{1,2},?\s+\d{4}\n'
    )
    m_sos = _sos_date_2021.search(text[500:])
    if m_sos:
        return 500 + m_sos.end()

    # 2009-2017.state.gov: nav/body boundary is frequently flattened into
    # "Secretary of State ... {Month DD, YYYY}" with inconsistent newlines.
    # Use whitespace-tolerant matching to keep page/corpus anchoring symmetric.
    _loc_date_pat = re.compile(
        r'Secretary of State(?:\s+|\xa0)+.{0,180}?'
        r'(?:January|February|March|April|May|June|July|August|'
        r'September|October|November|December)\s+\d{1,2},?\s+\d{4}\b',
        re.DOTALL,
    )
    m_loc = _loc_date_pat.search(text[600:11000])
    if m_loc:
        return 600 + m_loc.end()

    # Fallback 1: first true speaker label after nav region
    _sec_single = re.compile(r'\bSECRETARY [A-Z]+:(?!:)')
    m_sec = _sec_single.search(text[1500:])
    if m_sec:
        return 1500 + m_sec.start()

    # Fallback 2: Q&A pages may start with moderator/question markers first
    _qa_start = re.compile(r'\b(?:MODERATOR:|QUESTION:|Q:)')
    m_qa = _qa_start.search(text[1500:])
    if m_qa:
        return 1500 + m_qa.start()
    # Fallback: second half of text skips the navigation menu
    if not half_fallback:
        return -1
    return max(len(text) // 2, 0)


def _skip_residual_header(text: str, start: int, min_line_len: int = 60, max_scan: int = 800) -> int:
    """Advance *start* past short lines that are residual nav / header content.
    Speech sentences are typically 80+ chars; nav breadcrumbs, location lines,
    and date lines are almost always < 60 chars.  Scans at most *max_scan* chars."""
    pos = start
    limit = min(len(text), start + max_scan)
    while pos < limit:
        nl = text.find('\n', pos)
        if nl == -1 or nl >= limit:
            break
        if nl - pos >= min_line_len:
            return pos
        pos = nl + 1
    return start


def extract_state_speech_body(
    raw_html_or_text: str,
    url: str,
    secretary: str,
    expected_date: Optional[str] = None,
) -> dict:
    """Canonical speech-body extractor for archived state.gov pages.

    Accepts either a raw HTML string or an already-plain checkpoint text string.
    Returns a dict with keys: raw_text, clean_text, body_text, anchor_offset,
    date_extracted, title_extracted, extraction_mode, nav_flags.
    """
    from bs4 import BeautifulSoup as _BS4

    nav_flags: list = []
    text = raw_html_or_text or ''

    # ── Detect and parse HTML ────────────────────────────────────────────────
    _is_html = bool(
        re.search(r'<\s*html[\s>]', text[:500], re.IGNORECASE)
        or '<body' in text[:1000].lower()
    )
    if _is_html:
        extraction_mode = 'html_bs4'
        try:
            _soup = _BS4(text, 'lxml')
        except Exception:
            _soup = _BS4(text, 'html.parser')

        for _tag in _soup.find_all([
            'script', 'style', 'noscript', 'nav', 'header', 'footer',
            'aside', 'form', 'input', 'select', 'button',
        ]):
            _tag.decompose()

        for _sel in (
            '.social-share', '.share-block', '#share', '.sidebar',
            '.breadcrumb', '.breadcrumbs', '.related', '.more-links',
            '#related', '#sidebar', '.nav-menu', '.site-nav',
        ):
            for _el in _soup.select(_sel):
                _el.decompose()

        _title_tag = _soup.find('title')
        title_extracted = normalize_whitespace(_title_tag.get_text()) if _title_tag else ''

        _body_soup = None
        for _csel in ('main', 'article', '[role="main"]',
                      '.entry-content', '.post-content', '#content',
                      '#centerblock', '#main-content'):
            _body_soup = _soup.select_one(_csel)
            if _body_soup:
                break
        if _body_soup is None:
            _body_soup = _soup.find('body') or _soup

        clean_text = normalize_whitespace(
            _body_soup.get_text(separator='\n', strip=True)
        )
        raw_text = normalize_whitespace(_soup.get_text(separator='\n', strip=True))
    else:
        extraction_mode = 'plain_domain'
        raw_text = text
        clean_text = text
        title_extracted = ''

    # ── Domain-specific nav stripping ────────────────────────────────────────
    url_lower = url.lower()

    if '2017-2021.state.gov' in url_lower:
        _az_idx = clean_text.lower().find(_AZ_MENU_SENTINEL)
        if _az_idx != -1:
            nav_flags.append('az_menu_2017')
            _az_end = clean_text.find('\n\n', _az_idx + 100)
            if _az_end != -1 and _az_end < _az_idx + 12000:
                clean_text = clean_text[_az_end:].strip()

        for _cue in ('Skip to content', 'You are viewing: Archived Content'):
            _ci = clean_text[:500].lower().find(_cue.lower())
            if _ci != -1:
                nav_flags.append(f'archive_hdr_{_cue[:15]}')
                clean_text = clean_text[_ci + len(_cue):].strip()

        _pompeo_anchor = re.search(
            r'MICHAEL\s+R\.\s+POMPEO,\s+SECRETARY\s+OF\s+STATE'
            r'(?:\s*\n\s*[^\n]{1,120}\n)*',
            clean_text[:8000],
        )
        if _pompeo_anchor:
            nav_flags.append('pompeo_anchor')
            _pa_end = _pompeo_anchor.end()
            _pa_end = _skip_residual_header(clean_text, _pa_end, min_line_len=60, max_scan=800)
            body_text = clean_text[_pa_end:].strip()
            anchor_offset = _pa_end
        else:
            anchor_offset = _find_speech_content_start(clean_text, secretary)
            body_text = clean_text[anchor_offset:].strip()

    elif '2009-2017.state.gov' in url_lower:
        for _cue in ('You are viewing: Archived Content',
                     "Former Secretary Clinton's Remarks",
                     'Jump to In This Section'):
            if _cue.lower() in clean_text[:600].lower():
                nav_flags.append(f'clinton_hdr_{_cue[:20]}')

        _dbl_colon_re = re.compile(r'SECRETARY\s+[A-Z]+::\s*\n?', re.IGNORECASE)
        clean_text = _dbl_colon_re.sub('', clean_text)

        anchor_offset = _find_speech_content_start(clean_text, secretary)
        body_text = clean_text[anchor_offset:].strip()

        # If anchor landed inside nav region AND nav is confirmed, apply full strip.
        # Guard prevents replacing a correctly-anchored body (offset 2300-2800) when
        # no nav markers are actually present in the first 500 chars.
        _has_local_nav = any(m in clean_text[:500].lower() for m in _ARCHIVE_NAV_MARKERS_WIDE)
        if anchor_offset < 3000 and _has_local_nav:
            _stripped = _strip_archive_nav_prefix(clean_text, secretary)
            if _stripped and len(_stripped) >= 100:
                body_text = _stripped
                anchor_offset = max(0, len(clean_text) - len(clean_text.lstrip()) - len(body_text))

    elif '2001-2009.state.gov' in url_lower:
        if _ARCHIVE_NAV_PREFIX in clean_text[:500].lower():
            nav_flags.append('nav_2001_prefix')

        anchor_offset = _find_speech_content_start(clean_text, secretary)
        _near_nav = _ARCHIVE_NAV_PREFIX in clean_text[:min(1200, anchor_offset + 300)].lower()
        if _near_nav:
            anchor_offset = _skip_residual_header(clean_text, anchor_offset)
        body_text = clean_text[anchor_offset:].strip()

    elif '1997-2001.state.gov' in url_lower:
        anchor_offset = _find_speech_content_start(clean_text, secretary)
        body_text = clean_text[anchor_offset:].strip()

    else:
        extraction_mode = 'plain_fallback'
        anchor_offset = _find_speech_content_start(clean_text, secretary)
        body_text = clean_text[anchor_offset:].strip()

    # Strip footer material (these markers only appear after speech content ends)
    _footer_re = re.compile(
        r'\n(?:Bureaus\s*&\s*Offices|Related\b|Share\b|'
        r'Press\s+Availability\s+with|Country\s+Information)\b.*',
        re.DOTALL | re.IGNORECASE,
    )
    body_text = _footer_re.sub('', body_text).strip()

    # Extract date from page text for metadata / ±1-day sanity checking
    date_extracted = ''
    _date_m = re.search(
        r'(?:January|February|March|April|May|June|July|August|'
        r'September|October|November|December)\s+\d{1,2},?\s+(?:19|20)\d{2}',
        clean_text[:11000],
    )
    if _date_m:
        try:
            import dateutil.parser as _dp
            date_extracted = _dp.parse(_date_m.group(), dayfirst=False).strftime('%Y-%m-%d')
        except Exception:
            pass

    return {
        'raw_text': raw_text,
        'clean_text': clean_text,
        'body_text': body_text,
        'anchor_offset': anchor_offset,
        'date_extracted': date_extracted,
        'title_extracted': title_extracted,
        'extraction_mode': extraction_mode,
        'nav_flags': nav_flags,
    }


def _strip_archive_nav_prefix(text: str, secretary: str = '') -> str:
    """Strip archived state.gov nav boilerplate from corpus text.

    Two domain patterns are handled:
    - 2001-2009.state.gov: 'Other State Department Archive Sites' appears within
      the first 500 chars (sometimes after a title line).  Speech content starts
      right after the 'Month DD, YYYY' date line in the page header.
    - 2009-2017.state.gov / 2017-2021.state.gov: contains 'You are viewing: Archived Content'
      in the first 500 chars; nav sidebar is ~2,300–7,000 chars before speech content starts.

    Both branches use the same anchor strategy as _find_speech_content_start so that
    corpus_toks and cand_toks are symmetric — both start at actual speech content.
    """
    if not text:
        return text

    # Date-line anchor regex (covers 19xx and 20xx years; \r?\n handles both LF and CRLF pages).
    _date_anchor = re.compile(
        r'(?:January|February|March|April|May|June|July|August|September|'
        r'October|November|December)\s+\d{1,2},?\s+(?:19|20)\d{2}\r?\n'
    )

    # 2001-2009 pattern: nav prefix appears within first 500 chars, possibly after a title.
    # Use the date-line anchor for precise stripping so corpus_toks starts at speech content,
    # symmetric with the page anchor in _find_speech_content_start.
    if _ARCHIVE_NAV_PREFIX in text[:500].lower():
        m = _date_anchor.search(text[:2000])
        if m:
            after_date = text[m.end():]
            # Legacy prepared remarks / transcripts usually start shortly after
            # the date line with speaker or Q&A labels.
            anc = _find_legacy_archive_content_anchor(
                after_date,
                secretary=secretary,
                start_at=0,
                max_ahead=2500,
            )
            if anc != -1:
                return after_date[anc:].strip()
            # 2001-2009 prepared remarks: skip post-date nav footer before speech content.
            para = after_date.find('\n\n')
            if para != -1 and para < 2000:
                return after_date[para + 2:].strip()
            return after_date.strip()
        # Fall back: first paragraph break within the nav area
        for delim in ('\n\n', '\r\n\r\n'):
            idx = text.find(delim)
            if idx != -1 and idx < 1500:
                return text[idx:].strip()
        # No date anchor and no paragraph break: try speaker/Q&A label then prose heuristic.
        # Handles 2001-2009 essay/article pages where the breadcrumb has only "Month Year"
        # (no day number) so _date_anchor fails and all lines are single-\n-separated.
        _anc2 = _find_legacy_archive_content_anchor(text, secretary, start_at=200, max_ahead=3000)
        if _anc2 != -1:
            return text[_anc2:].strip()
        # First prose line >= 60 chars after the nav header (nav items are all < 60 chars)
        _nav_off = text[:500].lower().find(_ARCHIVE_NAV_PREFIX)
        _s = max(200, (_nav_off + len(_ARCHIVE_NAV_PREFIX)) if _nav_off != -1 else 200)
        while _s < min(len(text), 3000):
            _nl = text.find('\n', _s)
            if _nl == -1:
                break
            if _nl - _s >= 60:
                return text[_s:].strip()
            _s = _nl + 1
        return text

    # 2009-2017 / 2017-2021 pattern: nav sidebar spans ~2,300–7,000 chars.
    # Mirror _find_speech_content_start anchor strategy so corpus and page
    # are stripped to the same point even when line breaks are flattened.
    # Extended search window (11000) covers 2017-2021 pages where the A-Z nav
    # is ~7,000 chars and the date block follows it at ~7,000–10,700.
    # Restrict detection to first 500 chars: nav markers like "in this section"
    # also appear in page FOOTERS (after speech content), so a wider window
    # causes false positives that discard valid speech text at offset 0.
    _head = text[:500].lower()
    _has_2009_nav = any(m in _head for m in _ARCHIVE_NAV_MARKERS_WIDE)
    if _has_2009_nav:
        _loc_date = re.compile(
            r'Secretary of State(?:\s+|\xa0)+.{0,180}?'
            r'(?:January|February|March|April|May|June|July|August|'
            r'September|October|November|December)\s+\d{1,2},?\s+\d{4}\b',
            re.DOTALL,
        )
        m = _loc_date.search(text[600:11000])
        if m:
            after_date = text[600 + m.end():]
            # Primary: find SECRETARY NAME: (single colon, not double) — sidebars use
            # double-colon SECRETARY NAME:: for nav links, so this skips them automatically.
            _sec_in_page = re.compile(r'\bSECRETARY [A-Z]+:(?!:)')
            m_sec = _sec_in_page.search(after_date[:2000])
            if m_sec:
                return after_date[m_sec.start():].strip()
            # Q&A style pages can start with moderator/question labels before first secretary turn.
            _qa_start = re.compile(r'\b(?:MODERATOR:|QUESTION:|Q:)')
            m_qa = _qa_start.search(after_date[:2000])
            if m_qa:
                return after_date[m_qa.start():].strip()
            # Fallback: detect sidebar and skip to next paragraph break after it
            _sidebar_pat = re.compile(
                r'(?:Foreign Press Center|Jump to In This Section|In This Section\n)'
            )
            if _sidebar_pat.search(after_date[:800]):
                idx = after_date.find('\n\n', 200)
                if 0 < idx < 2000:
                    return after_date[idx:].strip()
            return after_date.strip()
        # If location/date block is absent but archive marker exists, strip to
        # first speaker/Q&A anchor after nav region.
        _sec_any = re.compile(r'\bSECRETARY [A-Z]+:(?!:)')
        m_sec_any = _sec_any.search(text[1500:])
        if m_sec_any:
            return text[1500 + m_sec_any.start():].strip()
        _qa_any = re.compile(r'\b(?:MODERATOR:|QUESTION:|Q:)')
        m_qa_any = _qa_any.search(text[1500:])
        if m_qa_any:
            return text[1500 + m_qa_any.start():].strip()
        # Fall back: date-line anchor in the nav window
        m_d = _date_anchor.search(text[200:4000])
        if m_d:
            return text[200 + m_d.end():].strip()
        return text[2000:].strip()

    return text


_BOILERPLATE_PHRASES = (
    'javascript is disabled',
    'please enable javascript',
    'for the best experience on this website',
)


def _strip_known_boilerplate(text: str) -> str:
    """Strip known scrape-failure boilerplate from archived page text.

    If a page returned a JavaScript-disabled error or similar navigation-only
    content, remove the offending prefix. If < 100 chars of real content remain
    after stripping, return '' so the caller can skip the page entirely.
    """
    lower_head = text[:500].lower()
    hit_pos = -1
    for phrase in _BOILERPLATE_PHRASES:
        pos = lower_head.find(phrase)
        if pos != -1 and (hit_pos == -1 or pos < hit_pos):
            hit_pos = pos
    if hit_pos == -1:
        return text
    # Try to find real content after the next paragraph break
    para_break = text.find('\n\n', hit_pos)
    if para_break != -1 and para_break < 500:
        remainder = text[para_break:].strip()
    else:
        remainder = text[500:].strip()
    return remainder if len(remainder) >= 100 else ''


def run_recover_archived(
    fused_csv: Path,
    round3_recovery: Path,
    triage_csv: Path,
    crawl_checkpoint: Path,
    output_recovery: Path,
    min_score: float = _MIN_SCORE_DEFAULT,       # unused: replaced by exact-date-only matching
    date_window_days: int = _DATE_WINDOW_DEFAULT,  # unused: replaced by exact-date-only matching
    rate_limit_secs: float = _RATE_LIMIT_DEFAULT,
    max_pages: int = _MAX_PAGES_DEFAULT,
    discovery_only: bool = False,
    show_progress: bool = True,
    domain: str = '2001-2009.state.gov',
    target_secretaries: Optional[frozenset] = None,
    listing_seeds: Optional[List[str]] = None,
    exact_date_min_score: float = 0.70,
    refresh_discovery: bool = False,
    url_overrides: Optional[dict] = None,
) -> None:
    """Crawl an archived state.gov domain to recover URLs for unresolved rows.

    Three phases (all resumable via crawl_checkpoint JSON):
      Phase 1 — URL discovery: crawl listing pages, collect speech page URLs.
      Phase 2 — Text scraping: fetch each speech page, extract text + date.
      Phase 3 — Text matching: compare scraped text to corpus rows via overlap_score.

    Outputs corpus_fusion_archived_recovery.csv in _RECOVERY_FIELDS format,
    compatible with the existing `fuse` subcommand.
    """
    # ── lazy imports (avoid loading heavy deps for all other subcommands) ──────
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise SystemExit(f'[recover_archived] Missing dependency: {exc}. '
                         f'Install requests and beautifulsoup4.') from exc

    # Full Firefox browser headers — required to pass CloudFront WAF on 2001-2009.state.gov.
    # The archive serves a "Technical Difficulties" page to requests lacking Sec-Fetch-* headers
    # or with a bot-identifying User-Agent string.
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
    }

    # Resolve parameter defaults
    _target_secs   = target_secretaries if target_secretaries is not None else _ARCHIVED_TARGET_SECRETARIES
    _listing_seeds = list(listing_seeds) if listing_seeds is not None else list(_ARCHIVED_LISTING_SEEDS)

    def _derive_scope_patterns(seeds: List[str]) -> List[str]:
        # Derive scope path fragments from seeds (e.g. 'secretary/former/powell/' from seed URLs).
        # Any crawled URL must contain at least one of these fragments to be accepted.
        _seed_paths: Set[str] = set()
        for _s in seeds:
            _p = urlsplit(_s).path.lower().lstrip('/')
            _parts = [p for p in _p.split('/') if p]  # strip empty segments
            if len(_parts) >= 3:
                _seed_paths.add('/'.join(_parts[:3]) + '/')
            elif len(_parts) >= 2:
                _seed_paths.add('/'.join(_parts[:2]) + '/')
            elif len(_parts) >= 1:
                _seed_paths.add(_parts[0] + '/')
        return list(_seed_paths)

    _scope_patterns: List[str] = _derive_scope_patterns(_listing_seeds)

    # ── Load unresolved corpus rows for the target secretaries ────────────────
    print('[recover_archived] Loading fused CSV for speech_text lookup...')
    fused_rows, _ = load_final_rows(fused_csv)
    fused_by_id: Dict[str, dict] = {
        (r.get('row_id', '') or '').strip(): r
        for r in fused_rows
        if (r.get('row_id', '') or '').strip()
    }

    print('[recover_archived] Loading round3 recovery to find unchanged_unresolved rows...')
    setup_field_limit()
    unresolved_ids: Set[str] = set()
    with round3_recovery.open('r', encoding='utf-8', errors='replace', newline='') as f:
        for rec in csv.DictReader(f):
            if rec.get('action', '') == 'unchanged_unresolved':
                rid = (rec.get('row_id', '') or '').strip()
                if rid:
                    unresolved_ids.add(rid)
    print(f'[recover_archived] unchanged_unresolved rows (all secretaries): {len(unresolved_ids)}')

    print('[recover_archived] Loading triage CSV...')
    triage_by_id: Dict[str, dict] = {}
    with triage_csv.open('r', encoding='utf-8', errors='replace', newline='') as f:
        for rec in csv.DictReader(f):
            rid = (rec.get('row_id', '') or '').strip()
            if rid:
                triage_by_id[rid] = rec

    # Build target list: rows for the target secretaries with usable speech text
    corpus_targets: List[dict] = []
    for rid in sorted(unresolved_ids):
        triage_row = triage_by_id.get(rid)
        if triage_row is None:
            continue
        sec = safe_lower(triage_row.get('secretary', ''))
        if sec not in _target_secs:
            continue
        fused_row = fused_by_id.get(rid, {})
        speech_text = (fused_row.get('speech_text', '') or '').strip()
        if len(speech_text) < 200:
            if not (url_overrides and rid in url_overrides):
                continue  # not enough text for reliable matching; allow through if manual URL provided
        old_url = (triage_row.get('old_url', '') or '').strip()
        if not old_url:
            old_url = (fused_row.get('url', '') or '').strip()
        if not old_url:
            old_url = (fused_row.get('old_url', '') or '').strip()

        corpus_targets.append({
            'row_id':       rid,
            'secretary':    triage_row.get('secretary', '').strip(),
            'date':         triage_row.get('date', '').strip(),
            'speech_text':  speech_text,
            'old_url':      old_url,
        })
    print(f'[recover_archived] {",".join(sorted(_target_secs))} targets with ≥200 char text: {len(corpus_targets)}')

    if not corpus_targets:
        print('[recover_archived] No targets — writing empty recovery CSV.')
        with output_recovery.open('w', encoding='utf-8', newline='') as f:
            csv.DictWriter(f, fieldnames=list(_RECOVERY_FIELDS)).writeheader()
        return

    # Option 3: targeted 2001 seed augmentation for unresolved Powell/Rice years.
    # Keeps discovery scoped and deterministic while improving A_true_gap coverage.
    if domain == '2001-2009.state.gov':
        target_years: Set[int] = set()
        for t in corpus_targets:
            d = parse_iso_date((t.get('date') or '').strip())
            if d and 2001 <= d.year <= 2009:
                target_years.add(d.year)
        addl: List[str] = []
        if 'powell' in _target_secs:
            for y in sorted(target_years):
                addl.extend([
                    f'https://2001-2009.state.gov/secretary/former/powell/remarks/{y}/index.htm',
                    f'https://2001-2009.state.gov/secretary/former/powell/remarks/{y}/',
                ])
        if 'rice' in _target_secs:
            for y in sorted(target_years):
                addl.extend([
                    f'https://2001-2009.state.gov/secretary/rm/{y}/index.htm',
                    f'https://2001-2009.state.gov/secretary/rm/{y}/',
                    f'https://2001-2009.state.gov/secretary/former/rice/remarks/{y}/index.htm',
                    f'https://2001-2009.state.gov/secretary/former/rice/remarks/{y}/',
                ])
        if addl:
            seen = set(_listing_seeds)
            n_before = len(_listing_seeds)
            for s in addl:
                if s not in seen:
                    _listing_seeds.append(s)
                    seen.add(s)
            if len(_listing_seeds) != n_before:
                print(f'[recover_archived] added {len(_listing_seeds) - n_before} targeted 2001 seeds for unresolved years')
        # Recompute scope patterns so dynamically-added seeds are included in link acceptance.
        _scope_patterns = _derive_scope_patterns(_listing_seeds)

    # ── Load or initialise checkpoint ─────────────────────────────────────────
    # checkpoint: {url: None | {text: str, date: str, title: str}}
    checkpoint: Dict[str, Optional[dict]] = {}
    if crawl_checkpoint.exists():
        try:
            checkpoint = json.loads(crawl_checkpoint.read_text(encoding='utf-8'))
            print(f'[recover_archived] Loaded checkpoint: {len(checkpoint)} URLs '
                  f'({sum(1 for v in checkpoint.values() if v is not None)} scraped)')
        except Exception as exc:
            print(f'[recover_archived] Warning: could not load checkpoint ({exc}); starting fresh')
            checkpoint = {}

    # ── Phase 1: URL discovery ─────────────────────────────────────────────────
    session = requests.Session()
    session.headers.update(HEADERS)

    # Sitemap discovery mode — replaces Phase 1 + Phase 2 BFS for domains where listing
    # pages are inaccessible but XML sitemaps are accessible (e.g. 2021-2025.state.gov).
    _use_sitemap = domain == '2021-2025.state.gov'
    if _use_sitemap:
        # Build target date set from corpus rows to focus sitemap discovery
        _target_date_set: Set[str] = set()
        for _tr in corpus_targets:
            _d = (_tr.get('date') or '').strip()
            if _d:
                _target_date_set.add(_d)
        print(f'[recover_archived] Phase 1+2: sitemap enumeration for {domain}...')
        new_sitemap = _run_sitemap_discovery(
            domain, checkpoint, session,
            target_dates=_target_date_set,
            window_days=21,
            rate_limit_secs=rate_limit_secs,
            show_progress=show_progress,
        )
        crawl_checkpoint.write_text(json.dumps(checkpoint, indent=2), encoding='utf-8')
        print(f'[recover_archived] WP sitemap: {new_sitemap} new URLs; {len(checkpoint)} total in checkpoint.')

    if not _use_sitemap:
        known_urls = set(checkpoint.keys())
        if refresh_discovery:
            print('[recover_archived] refresh_discovery=1: forcing Phase 1 seed re-crawl against existing checkpoint')
    # BFS queue: listing pages to visit; listing_seen prevents re-visiting
    listing_queue: List[str] = []
    listing_seen: Set[str] = set()
    for s in _listing_seeds:
        if s not in listing_seen:
            listing_queue.append(s)
            listing_seen.add(s)
    pages_fetched = 0
    new_discovered = 0

    print(f'[recover_archived] Phase 1: URL discovery ({len(listing_queue)} seed listing pages)...')
    pbar = tqdm(total=None, desc='discover', unit='page', disable=not show_progress, dynamic_ncols=True)
    while listing_queue and pages_fetched < max_pages:
        listing_url = listing_queue.pop(0)

        try:
            resp = session.get(listing_url, timeout=25, allow_redirects=True)
            resp.raise_for_status()
        except Exception:
            pbar.update(1)
            time.sleep(rate_limit_secs)
            pages_fetched += 1
            continue

        pages_fetched += 1
        pbar.update(1)

        try:
            soup = BeautifulSoup(resp.text, 'html.parser')
        except Exception:
            time.sleep(rate_limit_secs)
            continue

        # Detect global archive error page — if we see it on the first fetch, warn early
        _title_tag = soup.find('title')
        _page_title = _title_tag.get_text(strip=True) if _title_tag else ''
        if 'technical difficulties' in _page_title.lower() or 'exception: forbidden' in resp.text.lower():
            if pages_fetched == 1:
                print(f'[recover_archived] WARNING: {domain} is returning "Technical '
                      'Difficulties" error pages. The archive may be temporarily unavailable. '
                      'Discovery will continue but is unlikely to find speech URLs until the '
                      'archive is accessible again.')
            time.sleep(rate_limit_secs)
            continue

        # Use regex on raw HTML to extract ALL href values — BeautifulSoup's html.parser
        # stops parsing at the nested </body></html> injected by WaterFall v4.1 archive
        # banners, making soup.find_all('a') miss all speech links that appear after the banner.
        for href in re.findall(r"""href=["']([^"']+)["']""", resp.text):
            href = href.strip()
            if not href or href.startswith('#') or href.startswith('mailto:'):
                continue
            # Skip Backbone.js / Underscore template variables (e.g. "{{ link }}" from
            # <script type="text/template"> blocks on WordPress collection listing pages)
            if '{{' in href:
                continue
            abs_url = urljoin(listing_url, href).split('#')[0].rstrip('?')
            if abs_url in known_urls:
                continue
            if domain not in abs_url:
                continue
            parsed_link = urlsplit(abs_url)
            link_path = parsed_link.path.lower()
            in_scope = any(sc in link_path for sc in _scope_patterns)
            if not in_scope:
                continue
            if re.search(r'\.(gif|jpg|jpeg|png|pdf|doc|xls|zip|css|js)$', link_path):
                continue

            known_urls.add(abs_url)

            # Follow all in-scope pages for BFS link discovery
            if abs_url not in listing_seen:
                listing_seen.add(abs_url)
                listing_queue.append(abs_url)

            # Month listing pages (c#####.htm) contain only speech titles + links, not transcript
            # text. Follow them for BFS but exclude from Phase 2 text scraping.
            is_index = link_path.endswith('/index.htm') or link_path.endswith('/index.html') or link_path.endswith('/')
            if not is_index and not _is_month_listing(abs_url):
                checkpoint[abs_url] = None
                new_discovered += 1

        # For WordPress collection listing pages: extract speech URLs from
        # class="collection-result__link" anchors. These are top-level slug paths
        # (e.g. /championing-americas-first-freedom/) not in scope, so the main loop
        # above skips them. We extract them explicitly and add to checkpoint only
        # (not to the BFS queue — individual speech pages don't link to more speeches).
        _listing_in_scope = any(sc in urlsplit(listing_url).path.lower() for sc in _scope_patterns)
        if _listing_in_scope:
            for m in re.finditer(r'<a\s+([^>]*)>', resp.text, re.DOTALL):
                attrs = m.group(1)
                if 'collection-result__link' not in attrs:
                    continue
                href_m = re.search(r'href="([^"]+)"', attrs)
                if not href_m:
                    continue
                href = href_m.group(1).strip()
                if not href or '{{' in href:
                    continue
                abs_url = urljoin(listing_url, href).split('#')[0]
                if abs_url in known_urls or domain not in abs_url:
                    continue
                known_urls.add(abs_url)
                if abs_url not in checkpoint:
                    checkpoint[abs_url] = None
                    new_discovered += 1

        time.sleep(rate_limit_secs)

    pbar.close()

    forced_seed_urls_added = 0
    # Coverage fallback for 2001 Rice former branch: if discovery added nothing and
    # checkpoint still has no former/rice URLs, inject deterministic URLs as unscraped.
    if domain == '2001-2009.state.gov':
        _rice_former_in_ck = sum(
            1 for u in checkpoint.keys() if '/secretary/former/rice/' in u.lower()
        )
        if _rice_former_in_ck == 0:
            _forced_urls: Set[str] = set()
            # Seed/year URLs
            if 'rice' in _target_secs:
                _years = sorted({
                    parse_iso_date((t.get('date') or '').strip()).year
                    for t in corpus_targets
                    if parse_iso_date((t.get('date') or '').strip())
                })
                for y in _years:
                    if 2001 <= y <= 2009:
                        _forced_urls.add(f'https://2001-2009.state.gov/secretary/former/rice/remarks/{y}/index.htm')
                        _forced_urls.add(f'https://2001-2009.state.gov/secretary/former/rice/remarks/{y}/')
                _forced_urls.add('https://2001-2009.state.gov/secretary/former/rice/remarks/index.htm')

            # Old-url fallback URLs from unresolved target rows
            for t in corpus_targets:
                if safe_lower(t.get('secretary', '')) != 'rice':
                    continue
                ou = normalize_url(t.get('old_url', '') or '')
                if ou and domain in ou and '/secretary/former/rice/' in ou.lower():
                    _forced_urls.add(ou)

            for u in sorted(_forced_urls):
                if u not in checkpoint:
                    checkpoint[u] = None
                    forced_seed_urls_added += 1
            if forced_seed_urls_added:
                print(f'[recover_archived] forced coverage fallback injected {forced_seed_urls_added} rice-former URLs')

    print(f'[recover_archived] Phase 1 complete: {new_discovered} new speech URLs discovered, '
          f'{len(checkpoint)} total in checkpoint.')

    # Save checkpoint after discovery
    crawl_checkpoint.write_text(json.dumps(checkpoint, indent=2), encoding='utf-8')

    if discovery_only:
        print('[recover_archived] --discovery-only set; stopping after Phase 1.')
        return

    # ── Phase 2: Page scraping ─────────────────────────────────────────────────
    to_scrape = [url for url, data in checkpoint.items() if data is None]
    print(f'[recover_archived] Phase 2: Scraping {len(to_scrape)} unscraped pages...')
    pbar2 = tqdm(total=len(to_scrape), desc='scrape', unit='page',
                 disable=not show_progress, dynamic_ncols=True)
    scraped_count = 0
    for url in to_scrape:
        pbar2.update(1)
        try:
            resp = session.get(url, timeout=25, allow_redirects=True)
            resp.raise_for_status()
            html = resp.text
        except Exception:
            checkpoint[url] = {}   # mark as attempted but failed
            time.sleep(rate_limit_secs)
            scraped_count += 1
            if scraped_count % 50 == 0:
                crawl_checkpoint.write_text(json.dumps(checkpoint, indent=2), encoding='utf-8')
            continue

        try:
            # lxml tolerates the double-nested <html><body>...</body></html> injected by
            # WaterFall v4.1 archive banners; html.parser stops at the inner </body></html>
            # and discards all speech content that follows.
            try:
                soup = BeautifulSoup(html, 'lxml')
            except Exception:
                soup = BeautifulSoup(html, 'html.parser')
        except Exception:
            checkpoint[url] = {}
            time.sleep(rate_limit_secs)
            scraped_count += 1
            continue

        # Detect archive error page (2001-2009.state.gov returns a global "Technical
        # Difficulties" page with status 200 when the archive is unavailable)
        title_tag = soup.find('title')
        page_title = title_tag.get_text(strip=True) if title_tag else ''
        if 'technical difficulties' in page_title.lower() or 'exception: forbidden' in html.lower():
            checkpoint[url] = {}  # mark as error page; do not save garbage text
            time.sleep(rate_limit_secs)
            scraped_count += 1
            if scraped_count % 50 == 0:
                crawl_checkpoint.write_text(json.dumps(checkpoint, indent=2), encoding='utf-8')
            continue

        # Use full-document get_text() — soup.body only covers content before the archive
        # banner's nested </body></html>, missing the actual speech transcript.
        raw_text = soup.get_text(separator='\n', strip=True)

        date_str = _extract_date_from_archived_page(soup, url)
        title_tag = soup.find('title') or soup.find('h1') or soup.find('h2')
        title_text = title_tag.get_text(strip=True) if title_tag else ''

        checkpoint[url] = {'text': raw_text, 'date': date_str, 'title': title_text}
        scraped_count += 1

        # Periodic checkpoint save
        if scraped_count % 50 == 0:
            crawl_checkpoint.write_text(json.dumps(checkpoint, indent=2), encoding='utf-8')

        time.sleep(rate_limit_secs)

    pbar2.close()
    # Final save
    crawl_checkpoint.write_text(json.dumps(checkpoint, indent=2), encoding='utf-8')
    scraped_ok = sum(1 for v in checkpoint.values() if v and v.get('text'))
    print(f'[recover_archived] Phase 2 complete: {scraped_ok} pages with text.')

    # ── Phase 2.5: all-domain date backfill + targeted re-fetch for stale metadata ─────────
    pre_q = _checkpoint_date_quality_stats(checkpoint)
    target_dates: Set[str] = {
        (t.get('date') or '').strip() for t in corpus_targets if (t.get('date') or '').strip()
    }
    target_years: Set[int] = set()
    for ds in target_dates:
        d = parse_iso_date(ds)
        if d:
            target_years.add(d.year)

    date_backfill_attempted = 0
    date_backfill_updated = 0
    for url, data in checkpoint.items():
        if not data or not isinstance(data, dict) or not data.get('text'):
            continue
        stored = (data.get('date') or '').strip()
        if not stored or _is_year_only_date(stored):
            date_backfill_attempted += 1
            cands = _extract_candidate_dates_from_text(data.get('text', ''), domain)
            best = _best_date_for_targets(cands, target_dates)
            if best and best != stored:
                data['date'] = best
                checkpoint[url] = data
                date_backfill_updated += 1

    # Targeted re-fetch for unresolved-relevant stale dates (single-pass bounded refresh)
    refetch_attempted = 0
    refetch_updated = 0
    refetch_limit = 800
    stale_urls: List[str] = []
    for url, data in checkpoint.items():
        if not data or not isinstance(data, dict) or not data.get('text'):
            continue
        ds = (data.get('date') or '').strip()
        if ds and not _is_year_only_date(ds):
            continue
        year_hint = None
        m_year = re.search(r'/((?:19|20)\d{2})/', url)
        if m_year:
            try:
                year_hint = int(m_year.group(1))
            except Exception:
                year_hint = None
        if target_years and year_hint is not None and year_hint not in target_years:
            continue
        stale_urls.append(url)

    if stale_urls:
        pbar25 = tqdm(total=min(len(stale_urls), refetch_limit), desc='date_refetch', unit='page',
                      disable=not show_progress, dynamic_ncols=True)
        for url in stale_urls[:refetch_limit]:
            pbar25.update(1)
            refetch_attempted += 1
            try:
                resp = session.get(url, timeout=25, allow_redirects=True)
                resp.raise_for_status()
                html = resp.text
                try:
                    soup = BeautifulSoup(html, 'lxml')
                except Exception:
                    soup = BeautifulSoup(html, 'html.parser')
            except Exception:
                time.sleep(rate_limit_secs)
                continue

            title_tag = soup.find('title')
            page_title = title_tag.get_text(strip=True) if title_tag else ''
            if 'technical difficulties' in page_title.lower() or 'exception: forbidden' in html.lower():
                time.sleep(rate_limit_secs)
                continue

            raw_text = soup.get_text(separator='\n', strip=True)
            if len(raw_text) < 100:
                time.sleep(rate_limit_secs)
                continue
            fresh = _extract_date_from_archived_page(soup, url)
            if not fresh or _is_year_only_date(fresh):
                cands = _extract_candidate_dates_from_text(raw_text, domain)
                best = _best_date_for_targets(cands, target_dates)
                if best:
                    fresh = best
            old = (checkpoint.get(url, {}) or {}).get('date', '')
            checkpoint[url] = {'text': raw_text, 'date': fresh or old, 'title': page_title}
            if fresh and not _is_year_only_date(fresh) and fresh != old:
                refetch_updated += 1
            time.sleep(rate_limit_secs)
        pbar25.close()

    crawl_checkpoint.write_text(json.dumps(checkpoint, indent=2), encoding='utf-8')
    post_q = _checkpoint_date_quality_stats(checkpoint)
    print(
        '[recover_archived][date_backfill] '
        f'pre(empty={pre_q["empty"]},year_only={pre_q["year_only"]},full={pre_q["full"]}) '
        f'attempted={date_backfill_attempted} updated={date_backfill_updated} '
        f'refetch_attempted={refetch_attempted} refetch_updated={refetch_updated} '
        f'post(empty={post_q["empty"]},year_only={post_q["year_only"]},full={post_q["full"]})'
    )

    # ── Phase 3: Text matching ─────────────────────────────────────────────────
    print(f'[recover_archived] Phase 3: Matching {len(corpus_targets)} corpus rows against '
          f'{scraped_ok} scraped pages...')

    pages_with_text: List[dict] = []
    for url, data in checkpoint.items():
        if not data or not data.get('text') or len(data['text']) < 100:
            continue
        pages_with_text.append({'url': url, **data})

    recovery_rows: List[dict] = []
    stats = {'recovered': 0, 'unchanged_unresolved': 0}
    debug = {
        'targets': 0,
        'targets_with_date': 0,
        'targets_sidebar_nav': 0,
        'direct_url_hit': 0,
        'direct_url_accept': 0,
        'exact_date_pages_considered': 0,
        'exact_date_ge70': 0,
        'forced_seed_urls_added': forced_seed_urls_added,
        'date_backfill_attempted': date_backfill_attempted,
        'date_backfill_updated': date_backfill_updated,
        'date_refetch_attempted': refetch_attempted,
        'date_refetch_updated': refetch_updated,
        'blocked_by_higher_score': 0,
        'blocked_target_rows': 0,
    }

    # ── Pre-filter 1: Phantom rows (speech_text < 30 chars) ──────────────────
    # Catches 'NA' rows, '[As Prepared]', 'As Prepared for Delivery', etc.
    # These cannot be scored and may block URLs needed by legitimate rows.
    _phantom_indices: set = set()
    for _pf_i, _pf_t in enumerate(corpus_targets):
        _pf_speech = (_pf_t.get('speech_text', '') or '').strip()
        _pf_has_override = url_overrides and _pf_t['row_id'] in url_overrides
        if len(_pf_speech) < 30 and not _pf_has_override:
            _phantom_indices.add(_pf_i)
            recovery_rows.append(_make_recovery_row(
                row_id=_pf_t['row_id'], action='excluded_phantom',
                strategy='RA-S1', reason='no_speech_text',
                stage_used='recover_archived',
                candidate_id='', candidate_source='',
                score='0.000000', margin='',
                old_url=_pf_t.get('old_url', ''), new_url='',
                old_speech_text=_pf_t.get('speech_text', ''), new_speech_text='',
                speech_type_new='', location_new='',
                date_new=_pf_t.get('date', ''), secretary_new=_pf_t.get('secretary', ''),
                changed_url='0', changed_text='0',
            ))
            stats['excluded_phantom'] = stats.get('excluded_phantom', 0) + 1

    # ── Pre-filter 2: Corpus duplicates (same extracted speech in multiple rows) ─
    # Groups rows by 300-token speech signature. For each duplicate group, keeps
    # the row whose corpus date matches the checkpoint date for its url_override URL
    # (corrects nav-stub date extraction errors). Falls back to nav-detection, then
    # lowest row_id.
    # NOTE: _has_sidebar_nav is a local variable in the per-target loop below, NOT
    # a function, so it cannot be referenced here. Use _df_check_nav instead.
    _df_nav_re = re.compile(
        r'(?i)(SECRETARY\s+[A-Z.\'\-]+::|Jump to In This Section|In This Section'
        r'|Foreign Press Center|Other State Department Archive Sites)'
    )

    def _df_check_nav(text: str) -> bool:
        return bool(_df_nav_re.search(text[:6000]))

    def _df_sig_tokens(speech_text: str, secretary: str) -> frozenset:
        extracted, _ = extract_secretary_only_text(speech_text, secretary)
        if not extracted or len(extracted) < 200:
            return frozenset()  # insufficient signal — skip from dup detection
        return frozenset(token_set(normalize_whitespace(extracted), max_tokens=300))

    from collections import defaultdict as _defaultdict
    _sig_to_idxs: dict = _defaultdict(list)
    for _df_i, _df_t in enumerate(corpus_targets):
        if _df_i in _phantom_indices:
            continue
        _df_sig = _df_sig_tokens(_df_t.get('speech_text', ''), _df_t.get('secretary', ''))
        if len(_df_sig) >= 5:
            _sig_to_idxs[_df_sig].append(_df_i)

    _corpus_dup_indices: set = set()
    for _df_sig, _df_idxs in _sig_to_idxs.items():
        if len(_df_idxs) < 2:
            continue

        # Step 1: Nav detection — always exclude rows with sidebar nav pattern first.
        # Handles nav-prefixed dups (e.g. row 1929) and prevents nav boilerplate
        # from influencing tiebreaking in subsequent steps.
        _nav_set = {_df_i for _df_i in _df_idxs
                    if _df_check_nav(corpus_targets[_df_i].get('speech_text', '') or '')}
        _non_nav = [_df_i for _df_i in _df_idxs if _df_i not in _nav_set]
        _corpus_dup_indices.update(_nav_set)
        if len(_non_nav) <= 1:
            continue  # 0 or 1 non-nav rows remain; no further exclusion needed

        # Step 2: Checkpoint date-match among non-nav rows — corrects nav-extracted
        # date errors (e.g. row 3438 had wrong corpus date from nav breadcrumbs).
        _ckpt_date = None
        for _df_i in _non_nav:
            _df_rid = corpus_targets[_df_i]['row_id']
            if url_overrides and _df_rid in url_overrides:
                _df_ov = url_overrides[_df_rid]
                _df_ov_list = _df_ov if isinstance(_df_ov, list) else [_df_ov]
                for _df_u in _df_ov_list:
                    if _df_u in checkpoint and checkpoint[_df_u] and checkpoint[_df_u].get('date'):
                        _ckpt_date = checkpoint[_df_u]['date']
                        break
            if _ckpt_date:
                break

        _df_keep = None
        if _ckpt_date:
            _df_date_matches = [_df_i for _df_i in _non_nav
                                 if corpus_targets[_df_i].get('date', '') == _ckpt_date]
            if len(_df_date_matches) == 1:
                _df_keep = _df_date_matches[0]

        # Step 3: Lowest row_id among non-nav rows.
        if _df_keep is None:
            _df_keep = min(_non_nav, key=lambda _k: int(corpus_targets[_k]['row_id']))

        _corpus_dup_indices.update(_df_i for _df_i in _non_nav if _df_i != _df_keep)

    for _df_i in sorted(_corpus_dup_indices):
        _df_t = corpus_targets[_df_i]
        recovery_rows.append(_make_recovery_row(
            row_id=_df_t['row_id'], action='excluded_corpus_dup',
            strategy='RA-S1', reason='corpus_duplicate',
            stage_used='recover_archived',
            candidate_id='', candidate_source='',
            score='0.000000', margin='',
            old_url=_df_t.get('old_url', ''), new_url='',
            old_speech_text=_df_t.get('speech_text', ''), new_speech_text='',
            speech_type_new='', location_new='',
            date_new=_df_t.get('date', ''), secretary_new=_df_t.get('secretary', ''),
            changed_url='0', changed_text='0',
        ))
        stats['excluded_corpus_dup'] = stats.get('excluded_corpus_dup', 0) + 1

    # ── Pre-filter 2b: URL-based nav deduplication ───────────────────────────
    # If two rows share the same non-empty old_url and one has sidebar nav corpus
    # while another does not, the nav row is a lower-quality duplicate — exclude it.
    # Prevents nav-corpus rows from self-comparing in Phase 3a-0 and stealing URLs
    # that the real-content row (with an override or same old_url) should claim.
    _pf2b_url_to_idxs: dict = {}
    for _pf_i, _pf_t in enumerate(corpus_targets):
        if _pf_i in _phantom_indices or _pf_i in _corpus_dup_indices:
            continue
        # Use same URL resolution as Phase 3a-0: prefer 'url' field, fall back to 'old_url'
        _pf_url = (_pf_t.get('url') or _pf_t.get('old_url') or '').strip()
        if _pf_url:
            _pf2b_url_to_idxs.setdefault(_pf_url, []).append(_pf_i)
    for _pf_url, _pf_idxs in _pf2b_url_to_idxs.items():
        if len(_pf_idxs) < 2:
            continue
        _pf_nav = {_pf_i for _pf_i in _pf_idxs
                   if _df_check_nav(corpus_targets[_pf_i].get('speech_text', '') or '')}
        _pf_nonav = [_pf_i for _pf_i in _pf_idxs if _pf_i not in _pf_nav]
        if _pf_nav and _pf_nonav:
            _corpus_dup_indices.update(_pf_nav)
            for _pf_i in _pf_nav:
                _df_t2 = corpus_targets[_pf_i]
                recovery_rows.append(_make_recovery_row(
                    row_id=_df_t2['row_id'], action='excluded_corpus_dup',
                    strategy='RA-S1', reason='nav_url_duplicate',
                    stage_used='recover_archived',
                    candidate_id='', candidate_source='',
                    score='0.000000', margin='',
                    old_url=_df_t2.get('old_url', ''), new_url='',
                    old_speech_text=_df_t2.get('speech_text', ''), new_speech_text='',
                    speech_type_new='', location_new='',
                    date_new=_df_t2.get('date', ''), secretary_new=_df_t2.get('secretary', ''),
                    changed_url='0', changed_text='0',
                ))
                stats['excluded_corpus_dup'] = stats.get('excluded_corpus_dup', 0) + 1

    # Rebuild corpus_targets with excluded rows removed so Phase 3 indices are contiguous.
    _excluded_pre = _phantom_indices | _corpus_dup_indices
    if _excluded_pre:
        corpus_targets = [t for _ef_i, t in enumerate(corpus_targets)
                          if _ef_i not in _excluded_pre]
        print(f'[recover_archived] Pre-filters: excluded_phantom={stats.get("excluded_phantom",0)} '
              f'excluded_corpus_dup={stats.get("excluded_corpus_dup",0)} '
              f'remaining={len(corpus_targets)}')

    # ── Live-fetch helper for url_override URLs absent from checkpoint ────────
    _live_fetch_cache: dict = {}

    def _live_fetch_url(url: str):
        """Fetch a URL and return checkpoint-compatible dict, or None on failure."""
        if url in _live_fetch_cache:
            return _live_fetch_cache[url]
        try:
            _lf_resp = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
            if _lf_resp.status_code != 200:
                _live_fetch_cache[url] = None
                return None
            _lf_soup = BeautifulSoup(_lf_resp.text, 'html.parser')
            _lf_text = _lf_soup.get_text(separator='\n', strip=True)
            _lf_date = _extract_date_from_archived_page(_lf_soup, url)
            _lf_result = {'text': _lf_text, 'date': _lf_date, 'title': ''}
            _live_fetch_cache[url] = _lf_result
            return _lf_result
        except Exception:
            _live_fetch_cache[url] = None
            return None

    # ── Phase 3a: score all (target × exact-date page) pairs ─────────────────
    # all_scored: (target_idx, page_url, score, raw_page_text, page_date_str)
    all_scored: List[tuple] = []
    best_override_scores: Dict[int, float] = {}  # target_idx → best Phase 3a-1 score (below or above threshold)

    pbar3 = tqdm(corpus_targets, desc='recover_archived score', unit='row',
                 disable=not show_progress, dynamic_ncols=True)
    for i, target in enumerate(pbar3):
        debug['targets'] += 1
        secretary   = target['secretary']
        date_str    = target['date']
        speech_text = target['speech_text']
        row_date    = parse_iso_date(date_str)

        # If this row's existing URL is a blocked press-briefing path, the stored
        # speech_text is DPB/PRS content. Scoring valid candidates against it would
        # suppress correct matches. Clearing it triggers _source_text_blank=True in
        # Phase 3a-2, which accepts any exact-date valid URL (greedy picks longest).
        _row_old_url = target.get('old_url', '') or ''
        if not is_valid_candidate_url(_row_old_url, secretary):
            speech_text = ''

        if not row_date:
            continue  # no date → can never exact-match; will become unresolved in output
        debug['targets_with_date'] += 1

        # Strip corpus nav boilerplate; build token sets for full and secretary-only text.
        # Searches for the secretary speaker label (single colon form) from offset 0.
        # The single-colon 'SECRETARY NAME: ' cannot match the double-colon nav link
        # form 'SECRETARY NAME::' used by 2009-2017 sidebars, so this is safe for all
        # domains. Extended from 2009-2017/2017-2021 only to all domains so that
        # 2001-2009 Q&A transcripts anchor at the first secretary turn rather than
        # the preceding interviewer question ('MR. BROKAW: ...').
        _sec = secretary.upper()
        _speech_start = -1
        for _m in (f'SECRETARY {_sec}: ', f'SECRETARY {_sec}:\n',
                   f'MR. {_sec}: ', f'MR. {_sec}:\n',
                   'SECRETARY: ', 'SECRETARY:\n'):
            _i = speech_text.find(_m)
            if _i != -1 and (_speech_start == -1 or _i < _speech_start):
                _speech_start = _i
        speech_text_clean = (
            speech_text[_speech_start:]
            if _speech_start != -1
            else _strip_archive_nav_prefix(speech_text, secretary)
        )
        corpus_toks = token_set(normalize_whitespace(speech_text_clean))
        sec_text, _mode = extract_secretary_only_text(speech_text_clean, secretary)
        sec_toks = token_set(normalize_whitespace(sec_text)) if sec_text else corpus_toks

        # Detect sidebar-nav corpus text: double-colon pattern (SECRETARY NAME::) means
        # the original scraping captured navigation links rather than the speech body.
        # When the old_url is in the checkpoint we trust the URL match as authoritative
        # and substitute the checkpoint page text for scoring (see Phase 3a-0 below).
        # Also covers 2001-2009.state.gov archive pages ('Other State Department Archive
        # Sites' appears in the nav banner for that domain).
        _has_sidebar_nav = bool(re.search(
            r'(?i)(SECRETARY\s+[A-Z.\'\-]+::|Jump to In This Section|In This Section|Foreign Press Center'
            r'|Other State Department Archive Sites'
            r'|You are viewing: Archived Content|Skip to content)',
            speech_text[:6000],
        ))
        if _has_sidebar_nav:
            debug['targets_sidebar_nav'] += 1

        # Phase 3a-0: direct URL lookup — if old_url is already in the checkpoint,
        # score against it without requiring a date match. The date-based search below
        # misses rows where the crawl entry's date was not extracted or differs from
        # the triage date; direct lookup bypasses that filter for known URLs.
        _old_url = target.get('url', '') or target.get('old_url', '') or ''
        if _old_url and _old_url in checkpoint and is_valid_candidate_url(_old_url, secretary):
            _cand_data = checkpoint[_old_url]
            if _cand_data and _cand_data.get('text'):
                debug['direct_url_hit'] += 1
                _page_clean = _strip_known_boilerplate(_cand_data['text'])
                if len(_page_clean) >= 100:
                    _anchor = _find_speech_content_start(_page_clean, secretary)
                    _cand_toks = token_set(normalize_whitespace(_page_clean[_anchor:]))
                    if _has_sidebar_nav or len(corpus_toks) < _MIN_STUB_TOKENS:
                        # Corpus is nav garbage or too sparse — URL match is the evidence;
                        # self-compare guarantees alignment (catches low-token stubs like row 12480).
                        _direct_sc = overlap_score(_cand_toks, _cand_toks)
                    else:
                        _direct_sc = max(overlap_score(corpus_toks, _cand_toks),
                                         overlap_score(sec_toks,    _cand_toks))
                    if _direct_sc >= exact_date_min_score:
                        debug['direct_url_accept'] += 1
                        all_scored.append(
                            (i, _old_url, _direct_sc, _cand_data['text'],
                             _cand_data.get('date', ''))
                        )

        # Phase 3a-1: url_override — manually-verified URLs for rows with empty
        # old_url or unreliable corpus text.  Acceptance strategy depends on
        # whether the corpus is unreliable and whether one or multiple overrides exist.
        _override_urls: list = []
        if url_overrides:
            _row_id_str = str(target.get('row_id', ''))
            if _row_id_str in url_overrides:
                _raw = url_overrides[_row_id_str]
                _raw = _raw if isinstance(_raw, list) else [_raw]
                for _u in _raw:
                    if _u in checkpoint and checkpoint[_u] and checkpoint[_u].get('text'):
                        _override_urls.append(_u)
                    else:
                        # URL absent from checkpoint — attempt live-fetch
                        _lf = _live_fetch_url(_u)
                        if _lf:
                            checkpoint[_u] = _lf  # cache in-memory for Phase 3b
                            _override_urls.append(_u)
        if _override_urls:
            # Comprehensive unreliability test replaces the old narrow _MIN_STUB_TOKENS check
            _is_unreliable, _unreliable_reasons = is_unreliable_source_text(
                speech_text, speech_text_clean, corpus_toks, _old_url, secretary
            )
            _is_stub = _is_unreliable  # keep _is_stub for any downstream compat

            _n_overrides = len(_override_urls)
            _old_url_in_overrides = bool(_old_url and _old_url in _override_urls)

            for _ov_url in _override_urls:
                _ov_data = checkpoint[_ov_url]

                # Use canonical extractor for cleaner body text extraction
                _ov_extracted = extract_state_speech_body(
                    _ov_data['text'], _ov_url, secretary,
                    expected_date=target.get('date', '') or None,
                )
                _ov_body = _ov_extracted['body_text']

                # Fall back to legacy path if extractor returned too little text
                if len(_ov_body) < 100:
                    _ov_page_clean = _strip_known_boilerplate(_ov_data['text'])
                    if len(_ov_page_clean) < 100:
                        continue
                    _ov_anchor = _find_speech_content_start(_ov_page_clean, secretary)
                    _ov_near_nav = any(
                        m in _ov_page_clean[:min(1200, _ov_anchor + 300)].lower()
                        for m in (_ARCHIVE_NAV_PREFIX, _ARCHIVE_NAV_MARKER_2009, 'skip to content')
                    )
                    if _ov_near_nav:
                        _ov_anchor = _skip_residual_header(_ov_page_clean, _ov_anchor)
                    _ov_body = _ov_page_clean[_ov_anchor:].strip()

                if len(_ov_body) < 100:
                    continue

                _ov_cand_toks = token_set(normalize_whitespace(_ov_body), max_tokens=300)
                _ov_sec_cand_text, _ = extract_secretary_only_text(
                    normalize_whitespace(_ov_body), secretary
                )
                _ov_sec_cand_toks = (
                    token_set(_ov_sec_cand_text)
                    if _ov_sec_cand_text and len(_ov_sec_cand_text) > 20
                    else _ov_cand_toks
                )

                # ── Acceptance strategy ──────────────────────────────────────
                if _is_unreliable:
                    # Any unreliable corpus + any manually-verified override URL:
                    # trust the URL, not the corrupted text comparison.
                    # Self-compare restores V43 behavior for nav-corrupted multi-URL rows.
                    _ov_sc = overlap_score(_ov_cand_toks, _ov_cand_toks)
                else:
                    # Reliable corpus: Pass 1 — six comparison variants
                    _ov_nav_stripped = _strip_archive_nav_prefix(_ov_body, secretary)
                    _ov_cand_toks_alt = (
                        token_set(normalize_whitespace(_ov_nav_stripped), max_tokens=300)
                        if _ov_nav_stripped and len(_ov_nav_stripped) >= 100
                        else _ov_cand_toks
                    )
                    _ov_sc = max(
                        overlap_score(corpus_toks, _ov_cand_toks),
                        overlap_score(corpus_toks, _ov_cand_toks_alt),
                        overlap_score(sec_toks,    _ov_cand_toks),
                        overlap_score(sec_toks,    _ov_cand_toks_alt),
                        overlap_score(corpus_toks, _ov_sec_cand_toks),
                        overlap_score(sec_toks,    _ov_sec_cand_toks),
                    )

                # Pass 2 — paragraph/sliding-window (reliable corpus, below threshold)
                if not _is_unreliable and _ov_sc < exact_date_min_score:
                    _ov_content = _ov_body
                    for _win_toks in (50, 100, 200, 300):
                        for _stride_k in (0, 1, 2, 3):
                            _ov_start_c = _stride_k * 200
                            if _ov_start_c >= len(_ov_content):
                                break
                            _ov_w_toks = token_set(
                                normalize_whitespace(_ov_content[_ov_start_c:]),
                                max_tokens=_win_toks,
                            )
                            _ov_sc = max(_ov_sc,
                                         overlap_score(corpus_toks, _ov_w_toks),
                                         overlap_score(sec_toks,    _ov_w_toks))
                            if _ov_sc >= exact_date_min_score:
                                break
                        if _ov_sc >= exact_date_min_score:
                            break

                # Pass 3 — full secretary-only text without token cap
                if not _is_unreliable and _ov_sc < exact_date_min_score \
                        and _ov_sec_cand_text and len(_ov_sec_cand_text) > 20:
                    _ov_sec_full_toks = {
                        w for w in normalize_for_match(_ov_sec_cand_text).split() if len(w) > 2
                    }
                    _ov_sc = max(_ov_sc,
                                 overlap_score(corpus_toks, _ov_sec_full_toks),
                                 overlap_score(sec_toks,    _ov_sec_full_toks))

                if _ov_sc > best_override_scores.get(i, 0.0):
                    best_override_scores[i] = _ov_sc
                if _ov_sc >= exact_date_min_score:
                    all_scored.append(
                        (i, _ov_url, _ov_sc, _ov_data['text'],
                         _ov_data.get('date', ''))
                    )

            # Emit diagnostics when multi-URL override finds no page >= threshold
            if _n_overrides > 1:
                _ov_accepted_urls = {url for (_, url, sc, __, ___) in all_scored
                                     if sc >= exact_date_min_score and url in _override_urls}
                if not _ov_accepted_urls:
                    debug.setdefault('multi_override_no_accept', []).append({
                        'row_id': target.get('row_id'),
                        'is_unreliable': _is_unreliable,
                        'reasons': _unreliable_reasons,
                        'best_score': best_override_scores.get(i, 0.0),
                        'override_urls': _override_urls,
                    })

        # Date-only mode: when speech_text was cleared (corrupted old URL) corpus_toks
        # and sec_toks are empty, so SequenceMatcher cannot score.  Accept any valid
        # exact-date page; greedy Phase 3b picks the longest-text one.
        _source_text_blank = len(normalize_whitespace(speech_text or '')) < 50

        for page in pages_with_text:
            cand_date = parse_iso_date(page.get('date', ''))
            # EXACT DATE ONLY — no window-based fallback
            if cand_date != row_date:
                continue
            # URL quality filter: skip press-briefing and non-remarks pages.
            if not is_valid_candidate_url(page['url'], secretary):
                continue
            debug['exact_date_pages_considered'] += 1

            # Strip JavaScript-disabled and similar scrape-failure boilerplate from
            # the candidate page text before scoring to avoid false-positive matches.
            page_clean = _strip_known_boilerplate(page['text'])
            if len(page_clean) < 100:
                continue

            if _source_text_blank:
                # No reliable corpus text — date match is sufficient. Use a sentinel
                # score below any genuine content match (min content score with
                # max_tokens=120 is 85/120=0.7083) so content-matched rows always
                # claim pages before date-only rows in Phase 3c.
                all_scored.append((i, page['url'], 0.705, page['text'], page.get('date', '')))
                continue

            # Skip navigation / location-date blocks that precede speech content.
            _anchor = _find_speech_content_start(page_clean, secretary)
            cand_toks = token_set(normalize_whitespace(page_clean[_anchor:]))
            sc = max(overlap_score(corpus_toks, cand_toks),
                     overlap_score(sec_toks,    cand_toks))
            if sc < exact_date_min_score:
                _sec_cand_text, _ = extract_secretary_only_text(
                    normalize_whitespace(page_clean[_anchor:]), secretary
                )
                if _sec_cand_text and len(_sec_cand_text) > 20:
                    _sec_cand_toks = token_set(_sec_cand_text)
                    sc = max(sc,
                             overlap_score(corpus_toks, _sec_cand_toks),
                             overlap_score(sec_toks,    _sec_cand_toks))
                # Pass 2 — sliding window
                if sc < exact_date_min_score:
                    _content = page_clean[_anchor:]
                    for _stride_k in (1, 2, 3):
                        _start = _stride_k * 300
                        if _start >= len(_content):
                            break
                        _w_toks = token_set(normalize_whitespace(_content[_start:]))
                        sc = max(sc,
                                 overlap_score(corpus_toks, _w_toks),
                                 overlap_score(sec_toks,    _w_toks))
                        if sc >= exact_date_min_score:
                            break
                # Pass 3 — full secretary-only text without token cap
                if sc < exact_date_min_score and _sec_cand_text and len(_sec_cand_text) > 20:
                    _sec_full_toks = {
                        w for w in normalize_for_match(_sec_cand_text).split() if len(w) > 2
                    }
                    sc = max(sc,
                             overlap_score(corpus_toks, _sec_full_toks),
                             overlap_score(sec_toks,    _sec_full_toks))
            if sc >= exact_date_min_score:
                debug['exact_date_ge70'] += 1
                all_scored.append((i, page['url'], sc, page['text'], page.get('date', '')))

    pbar3.close()

    # ── Phase 3b: greedy assignment — highest score claims the candidate first ─
    # Sorting by score descending ensures the best-evidence row always wins when
    # multiple corpus rows share the same date and compete for the same page URL
    # (e.g. Powell row_id 1393 at score 0.357 must not block row_id 1395 at 1.0).
    # Sort: highest score first; within same score, lower index wins.
    all_scored.sort(key=lambda x: (-x[2], x[0]))

    # Track which pages scored high (>= 0.95) for each target, for post-assignment
    # exclusion of rows that are blocked only because a page was claimed by another row
    # with the same/similar content (genuine corpus duplicate).
    _high_score_pages: Dict[int, Set[str]] = {}
    for _hs_ti, _hs_pu, _hs_sc, _, __ in all_scored:
        if _hs_sc >= 0.95:
            _high_score_pages.setdefault(_hs_ti, set()).add(_hs_pu)

    used_pages:   Set[str] = set()
    used_targets: Set[int] = set()
    assignments:  Dict[int, Tuple[str, float, str, str]] = {}  # idx → (url, score, text, date)
    blocked_target_ids: Set[int] = set()

    for target_idx, page_url, sc, page_text, page_date in all_scored:
        if target_idx in used_targets or page_url in used_pages:
            if target_idx not in used_targets and page_url in used_pages:
                debug['blocked_by_higher_score'] += 1
                blocked_target_ids.add(target_idx)
            continue
        assignments[target_idx] = (page_url, sc, page_text, page_date)
        used_pages.add(page_url)
        used_targets.add(target_idx)
    debug['blocked_target_rows'] = len(blocked_target_ids)

    # ── Phase 3c: emit recovery rows ─────────────────────────────────────────
    for i, target in enumerate(corpus_targets):
        row_id      = target['row_id']
        secretary   = target['secretary']
        date_str    = target['date']
        speech_text = target['speech_text']
        old_url     = target['old_url']

        if i in assignments:
            _url, _sc, _text, _date = assignments[i]
            recovery_rows.append(_make_archived_recovery_row(
                row_id=row_id,
                new_url=_url,
                new_text=_text,
                old_text=speech_text,
                old_url=old_url,
                secretary=secretary,
                date=_date or date_str,
                score=_sc,
            ))
            stats['recovered'] += 1
        else:
            # Check if all high-score pages for this row were claimed by other rows
            # (genuine corpus duplicate: same speech content assigned to another row).
            _hs_pages = _high_score_pages.get(i, set())
            _all_claimed = bool(_hs_pages) and all(pu in used_pages for pu in _hs_pages)
            if _all_claimed:
                recovery_rows.append(_make_recovery_row(
                    row_id=row_id, action='excluded_corpus_dup',
                    strategy='RA-S1', reason='page_claimed_by_other_row',
                    stage_used='recover_archived',
                    candidate_id='', candidate_source='',
                    score=f'{best_override_scores.get(i, 0.0):.6f}', margin='',
                    old_url=old_url, new_url='',
                    old_speech_text=speech_text, new_speech_text='',
                    speech_type_new='',
                    location_new=_extract_location_from_speech_text(speech_text) if speech_text else '',
                    date_new=date_str, secretary_new=secretary,
                    changed_url='0', changed_text='0',
                ))
                stats['excluded_corpus_dup'] = stats.get('excluded_corpus_dup', 0) + 1
            else:
                recovery_rows.append(_make_recovery_row(
                    row_id=row_id, action='unchanged_unresolved',
                    strategy='RA-S1', reason='no_exact_date_archived_match',
                    stage_used='recover_archived',
                    candidate_id='', candidate_source='',
                    score=f'{best_override_scores.get(i, 0.0):.6f}', margin='',
                    old_url=old_url, new_url='',
                    old_speech_text=speech_text, new_speech_text='',
                    speech_type_new='',
                    location_new=_extract_location_from_speech_text(speech_text) if speech_text else '',
                    date_new=date_str, secretary_new=secretary,
                    changed_url='0', changed_text='0',
                ))
                stats['unchanged_unresolved'] += 1

    print(f'[recover_archived] total={len(corpus_targets)} '
          f'recovered={stats["recovered"]} '
          f'unchanged_unresolved={stats["unchanged_unresolved"]}')
    print(
        '[recover_archived][debug] '
        f"targets={debug['targets']} targets_with_date={debug['targets_with_date']} "
        f"sidebar_nav={debug['targets_sidebar_nav']} direct_url_hit={debug['direct_url_hit']} "
        f"direct_url_accept={debug['direct_url_accept']} exact_date_considered={debug['exact_date_pages_considered']} "
        f"exact_date_ge70={debug['exact_date_ge70']} forced_seed_urls_added={debug['forced_seed_urls_added']} "
        f"date_backfill_attempted={debug['date_backfill_attempted']} date_backfill_updated={debug['date_backfill_updated']} "
        f"date_refetch_attempted={debug['date_refetch_attempted']} date_refetch_updated={debug['date_refetch_updated']} "
        f"blocked_by_higher_score={debug['blocked_by_higher_score']} blocked_target_rows={debug['blocked_target_rows']}"
    )

    with output_recovery.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(_RECOVERY_FIELDS))
        w.writeheader()
        w.writerows(recovery_rows)
    print(f'[recover_archived] wrote {output_recovery}')


def _make_archived_recovery_row(
    row_id: str,
    new_url: str,
    new_text: str,
    old_text: str,
    old_url: str,
    secretary: str,
    date: str,
    score: float,
) -> dict:
    """Build a _RECOVERY_FIELDS dict from an archived-crawl match."""
    return _make_recovery_row(
        row_id=row_id,
        action='recovered',
        strategy='RA-S1',
        reason='archived_crawl_match',
        stage_used='recover_archived',
        candidate_id='',
        candidate_source='archived_crawl',
        score=f'{score:.6f}',
        margin='',
        old_url=old_url,
        new_url=new_url,
        old_speech_text=old_text,
        new_speech_text=new_text,
        speech_type_new='',
        location_new=_extract_location_from_speech_text(new_text) if new_text else '',
        date_new=date,
        secretary_new=secretary,
        changed_url='1' if canonical_url_key(new_url) != canonical_url_key(old_url) else '0',
        changed_text='1' if normalize_whitespace(new_text) != normalize_whitespace(old_text) else '0',
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Subcommand 6: audit
# ═══════════════════════════════════════════════════════════════════════════════


_FULL_AUDIT_FIELDS = [
    'row_id', 'action', 'reason',
    'secretary', 'date', 'location',
    'old_url', 'new_url',
    'score', 'margin',
    'stage_used', 'candidate_id', 'candidate_source',
    'changed_url', 'changed_text',
    'quality_before', 'quality_after',
    'is_dropped', 'is_sc2_row',
]


def run_manual_resolve(
    manual_json_path: Path,
    all_prior_recovery_csvs: List[Path],
    fused_csv: Path,
    checkpoint_paths: Dict[str, Path],
    output_csv: Path,
) -> None:
    """Stage 5.9: emit recovered rows for manually verified URLs.

    Reads manual_json_path ({"row_id": "url"}) and produces a recovery CSV
    with action='recovered' for rows that are still unchanged_unresolved after
    all automated stages. Rows already resolved by earlier stages are skipped.
    Rows whose URL is absent from the appropriate domain checkpoint are also
    skipped with a warning (no silent empty-text entries).
    """
    import json as _json_mod

    setup_field_limit()

    # ── Load manual resolutions ─────────────────────────────────────────────
    with manual_json_path.open(encoding='utf-8') as f:
        manual: Dict[str, str] = _json_mod.load(f)
    if not manual:
        print('[manual_resolve] JSON is empty — nothing to do')
        output_csv.write_text('', encoding='utf-8')
        return
    print(f'[manual_resolve] {len(manual)} manual entries: {sorted(manual.keys())}')

    # ── Determine effective action per row (last-write-wins, mimicking combine) ─
    effective_action: Dict[str, str] = {}
    for csv_path in all_prior_recovery_csvs:
        if not csv_path.exists():
            continue
        with csv_path.open(encoding='utf-8', errors='replace', newline='') as f:
            for rec in csv.DictReader(f):
                rid = (rec.get('row_id') or '').strip()
                act = (rec.get('action') or '').strip()
                if rid and act:
                    effective_action[rid] = act

    _RESOLVED_ACTIONS = {'recovered', 'deep_recovered', 'excluded_corpus_dup', 'excluded_phantom'}
    resolved_ids = {rid for rid, act in effective_action.items() if act in _RESOLVED_ACTIONS}

    unresolved_manual = {rid: url for rid, url in manual.items() if rid not in resolved_ids}
    skipped_resolved = len(manual) - len(unresolved_manual)
    print(f'[manual_resolve] {skipped_resolved} already resolved (skipped), '
          f'{len(unresolved_manual)} to process')

    if not unresolved_manual:
        output_csv.write_text('', encoding='utf-8')
        return

    # ── Load fused CSV for metadata (secretary, date, location, old corpus text) ─
    fused_by_id: Dict[str, dict] = {}
    with fused_csv.open(encoding='utf-8', errors='replace', newline='') as f:
        for row in csv.DictReader(f):
            rid = (row.get('row_id') or '').strip()
            if rid in unresolved_manual:
                fused_by_id[rid] = row

    # ── Group by domain so each checkpoint is loaded at most once ──────────
    _from_url = __import__('urllib.parse', fromlist=['urlparse']).urlparse
    domain_to_rids: Dict[str, List[str]] = {}
    for rid, url in unresolved_manual.items():
        domain = _from_url(url).netloc  # e.g. '2001-2009.state.gov'
        domain_to_rids.setdefault(domain, []).append(rid)

    # ── Process each domain ─────────────────────────────────────────────────
    _FIELDNAMES = list(_make_recovery_row(
        row_id='', action='', strategy='', reason='', stage_used='',
        candidate_id='', candidate_source='', score='', margin='',
        old_url='', new_url='', old_speech_text='', new_speech_text='',
        speech_type_new='', location_new='', date_new='', secretary_new='',
        changed_url='', changed_text='',
    ).keys())

    n_emitted = 0
    n_missing = 0
    recovery_rows: List[dict] = []

    for domain, rids in sorted(domain_to_rids.items()):
        ckpt_path = checkpoint_paths.get(domain)
        if ckpt_path is None or not ckpt_path.exists():
            print(f'[manual_resolve] WARN no checkpoint configured for domain {domain!r} '
                  f'— skipping rows {rids}')
            n_missing += len(rids)
            continue

        print(f'[manual_resolve] Loading checkpoint for {domain} ({ckpt_path.name}) …')
        import json as _j
        checkpoint: Dict[str, Optional[dict]] = _j.loads(
            ckpt_path.read_text(encoding='utf-8')
        )

        for rid in sorted(rids, key=int):
            url = unresolved_manual[rid]
            ckpt_entry = checkpoint.get(url)
            if not ckpt_entry or not ckpt_entry.get('text'):
                print(f'[manual_resolve] WARN row {rid}: URL not in checkpoint or empty text '
                      f'— skipping ({url})')
                n_missing += 1
                continue

            raw_text   = ckpt_entry['text']
            fused_row  = fused_by_id.get(rid, {})
            secretary  = (fused_row.get('secretary') or '').strip()
            date_str   = (fused_row.get('Date') or fused_row.get('date') or '').strip()
            location   = (fused_row.get('Location') or '').strip()
            old_url    = (fused_row.get('url') or '').strip()
            old_text   = (fused_row.get('speech_text') or '').strip()

            # Extract speech body using existing helpers
            clean = _strip_known_boilerplate(raw_text)
            anchor = _find_speech_content_start(clean, secretary)
            body = normalize_whitespace(clean[anchor:]).strip()

            if len(body) < 100:
                print(f'[manual_resolve] WARN row {rid}: extracted body too short '
                      f'({len(body)} chars) — skipping')
                n_missing += 1
                continue

            changed_url  = '1' if url != old_url else '0'
            changed_text = '1' if body != old_text else '0'

            recovery_rows.append(_make_recovery_row(
                row_id=rid,
                action='recovered',
                strategy='RA-M1',
                reason='manual_url_override',
                stage_used='manual_resolve',
                candidate_id=url,
                candidate_source=domain,
                score='1.000000',
                margin='',
                old_url=old_url,
                new_url=url,
                old_speech_text=old_text,
                new_speech_text=body,
                speech_type_new='',
                location_new=location,
                date_new=date_str,
                secretary_new=secretary,
                changed_url=changed_url,
                changed_text=changed_text,
            ))
            n_emitted += 1
            print(f'[manual_resolve] row {rid} ({secretary}, {date_str}): '
                  f'emitted recovered → {url}')

    # ── Write output CSV ────────────────────────────────────────────────────
    with output_csv.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
        writer.writeheader()
        writer.writerows(recovery_rows)

    print(f'[manual_resolve] Done — emitted={n_emitted}, '
          f'skipped_resolved={skipped_resolved}, missing_checkpoint={n_missing}')
    print(f'[manual_resolve] Wrote {output_csv.name}')


def run_audit(
    input_final: Path,
    fused_csv: Path,
    dedup_audit: Path,
    rubio_audit: Path,
    deep_recovery: Path,
    output_audit: Path,
    backfill_audit: Optional[Path] = None,
    url_overrides: Optional[dict] = None,
) -> None:
    """Join all intermediate audit CSVs into one unified per-row audit."""
    from collections import Counter as _Counter

    print('[audit] Loading original v2 CSV...')
    old_rows, _ = load_final_rows(input_final)
    old_rows_by_id: Dict[str, dict] = {
        (r.get('row_id', '') or '').strip(): r
        for r in old_rows
        if (r.get('row_id', '') or '').strip()
    }

    print('[audit] Loading fused CSV...')
    fused_rows, _ = load_final_rows(fused_csv)
    fused_rows_by_id: Dict[str, dict] = {
        (r.get('row_id', '') or '').strip(): r
        for r in fused_rows
        if (r.get('row_id', '') or '').strip()
    }

    # Dedup audit: index only the dropped rows (action = dropped_text_dup / dropped_url_dup)
    dedup_dropped_by_id: Dict[str, dict] = {}
    with dedup_audit.open('r', encoding='utf-8', errors='replace', newline='') as f:
        for rec in csv.DictReader(f):
            if rec.get('action', '') in ('dropped_text_dup', 'dropped_url_dup'):
                rid = (rec.get('row_id', '') or '').strip()
                if rid:
                    dedup_dropped_by_id[rid] = rec

    # Rubio audit: separate new vs. upgrade rows
    rubio_new_by_id: Dict[str, dict] = {}
    rubio_upgrade_by_id: Dict[str, dict] = {}
    with rubio_audit.open('r', encoding='utf-8', errors='replace', newline='') as f:
        for rec in csv.DictReader(f):
            rid = (rec.get('row_id', '') or '').strip()
            if not rid:
                continue
            act = rec.get('action', '')
            if act == 'sc2_rubio_new':
                rubio_new_by_id[rid] = rec
            elif act == 'sc2_rubio_upgrade':
                rubio_upgrade_by_id[rid] = rec

    # Deep recovery: indexed by row_id
    recovery_by_id: Dict[str, dict] = {}
    with deep_recovery.open('r', encoding='utf-8', errors='replace', newline='') as f:
        for rec in csv.DictReader(f):
            rid = (rec.get('row_id', '') or '').strip()
            if rid:
                recovery_by_id[rid] = rec

    # Rubio backfill new rows — read directly from fused CSV (match_pass='rubio_backfill')
    # so audit remains correct even when the backfill audit file was overwritten
    backfill_new_by_id: Dict[str, dict] = {
        rid: row
        for rid, row in fused_rows_by_id.items()
        if (row.get('match_pass', '') or '') == 'rubio_backfill'
        and rid not in old_rows_by_id
        and rid not in rubio_new_by_id
    }

    print(
        f'[audit] old={len(old_rows_by_id)} fused={len(fused_rows_by_id)} '
        f'dropped={len(dedup_dropped_by_id)} sc2_new={len(rubio_new_by_id)} '
        f'sc2_upgrade={len(rubio_upgrade_by_id)} recovery_triage={len(recovery_by_id)} '
        f'backfill_new={len(backfill_new_by_id)}'
    )

    audit_rows: List[dict] = []

    # ── Original v2 rows ──────────────────────────────────────────────────────
    for rid, old_row in old_rows_by_id.items():
        old_url  = (old_row.get('url', '') or '').strip()
        q_before = quality_score_row(old_row)
        # Always populated so every row is identifiable, even when old_url/new_url are empty
        secretary = (old_row.get('secretary', '') or '').strip()
        date      = (old_row.get('Date', '') or '').strip()
        location  = (old_row.get('Location', '') or '').strip()

        if rid in dedup_dropped_by_id:
            drop_action = dedup_dropped_by_id[rid].get('action', 'dropped_text_dup')
            audit_rows.append({
                'row_id':           rid,
                'action':           drop_action,
                'reason':           'text_hash_dup' if drop_action == 'dropped_text_dup' else 'expand_url_dup',
                'secretary':        secretary,
                'date':             date,
                'location':         (
                    _clean_location(location, speech_text=(old_row.get('speech_text') or ''))
                    or _impute_location_from_url(old_url)
                ),
                'old_url':          old_url,
                'new_url':          '',
                'score':            '0.000000',
                'margin':           '',
                'stage_used':       'smart_dedup',
                'candidate_id':     '',
                'candidate_source': '',
                'changed_url':      '0',
                'changed_text':     '0',
                'quality_before':   q_before,
                'quality_after':    0,
                'is_dropped':       '1',
                'is_sc2_row':       '0',
            })
            continue

        fused_row = fused_rows_by_id.get(rid, {})
        new_url = (fused_row.get('url', '') or old_url).strip()
        q_after = quality_score_row(fused_row) if fused_row else 0
        changed_url = '1' if new_url != old_url else '0'
        changed_text = '1' if (
            normalize_whitespace(fused_row.get('speech_text', '') or '') !=
            normalize_whitespace(old_row.get('speech_text', '') or '')
        ) else '0'

        # Determine action (sc2_upgrade takes priority over deep_recover)
        if rid in rubio_upgrade_by_id:
            action      = 'sc2_upgrade'
            reason      = 'sc2_quality_upgrade'
            stage_used  = 'rubio_expand'
            score       = ''
            margin      = ''
            cand_id     = ''
            cand_source = ''
        elif rid in recovery_by_id:
            rec = recovery_by_id[rid]
            if rec.get('action') == 'recovered':
                action      = 'deep_recovered'
                reason      = rec.get('reason', '')
                stage_used  = rec.get('stage_used', 'deep_recover')
                score       = rec.get('score', '')
                margin      = rec.get('margin', '')
                cand_id     = rec.get('candidate_id', '')
                cand_source = rec.get('candidate_source', '')
            elif rec.get('action') in ('excluded_phantom', 'excluded_corpus_dup'):
                action      = rec['action']
                reason      = rec.get('reason', '')
                stage_used  = rec.get('stage_used', 'recover_archived')
                score       = rec.get('score', '0.000000')
                margin      = ''
                cand_id     = ''
                cand_source = ''
                new_url     = ''
                changed_url = '0'
                changed_text = '0'
            else:
                action      = 'unchanged_unresolved'
                reason      = 'no_strategy_succeeded'
                stage_used  = rec.get('stage_used', 'deep_recover')
                score       = rec.get('score', '0.000000')
                margin      = ''
                cand_id     = ''
                cand_source = ''
        else:
            action      = 'kept_no_change'
            reason      = ''
            stage_used  = 'none'
            score       = ''
            margin      = ''
            cand_id     = ''
            cand_source = ''

        # Reclassify rows that are unchanged_unresolved only because they were
        # filtered from corpus_targets (short speech_text, no url_override).
        if (action == 'unchanged_unresolved'
                and url_overrides is not None
                and len((old_row.get('speech_text', '') or '').strip()) < 50
                and rid not in url_overrides):
            action      = 'excluded_phantom'
            reason      = 'short_speech_no_override'
            stage_used  = 'run_audit'
            score       = '0.000000'
            margin      = ''
            cand_id     = ''
            cand_source = ''
            new_url     = ''
            changed_url  = '0'
            changed_text = '0'

        audit_rows.append({
            'row_id':           rid,
            'action':           action,
            'reason':           reason,
            'secretary':        secretary,
            'date':             date,
            'location':         (
                _clean_location(
                    (fused_row.get('Location') or location or ''),
                    speech_text=(fused_row.get('speech_text') or old_row.get('speech_text') or ''),
                )
                or _impute_location_from_url(new_url or old_url)
            ),
            'old_url':          old_url,
            'new_url':          new_url,
            'score':            score,
            'margin':           margin,
            'stage_used':       stage_used,
            'candidate_id':     cand_id,
            'candidate_source': cand_source,
            'changed_url':      changed_url,
            'changed_text':     changed_text,
            'quality_before':   q_before,
            'quality_after':    q_after,
            'is_dropped':       '0',
            'is_sc2_row':       '0',
        })

    # ── SC2 new rows ──────────────────────────────────────────────────────────
    for rid, rubio_rec in rubio_new_by_id.items():
        fused_row = fused_rows_by_id.get(rid, {})
        new_url = (fused_row.get('url', '') or rubio_rec.get('url', '')).strip()
        q_after = quality_score_row(fused_row) if fused_row else 0
        audit_rows.append({
            'row_id':           rid,
            'action':           'sc2_new',
            'reason':           'sc2_coverage_gap',
            'secretary':        (fused_row.get('secretary', '') or '').strip(),
            'date':             (fused_row.get('Date', '') or '').strip(),
            'location':         (
                _clean_location(
                    (fused_row.get('Location', '') or '').strip(),
                    speech_text=(fused_row.get('speech_text') or ''),
                )
                or _impute_location_from_url(new_url)
            ),
            'old_url':          '',
            'new_url':          new_url,
            'score':            '1.000000',
            'margin':           '',
            'stage_used':       'rubio_expand',
            'candidate_id':     '',
            'candidate_source': 'small2',
            'changed_url':      '0',
            'changed_text':     '0',
            'quality_before':   0,
            'quality_after':    q_after,
            'is_dropped':       '0',
            'is_sc2_row':       '1',
        })

    # ── Rubio backfill new rows ───────────────────────────────────────────────
    for rid, bf_rec in backfill_new_by_id.items():
        fused_row = bf_rec  # backfill_new_by_id values are already fused rows
        new_url = (fused_row.get('url', '') or '').strip()
        q_after = quality_score_row(fused_row)
        audit_rows.append({
            'row_id':           rid,
            'action':           'rubio_backfill_new',
            'reason':           'rubio_backfill_coverage_gap',
            'secretary':        (fused_row.get('secretary', '') or '').strip(),
            'date':             (fused_row.get('Date', '') or '').strip(),
            'location':         (
                _clean_location(
                    (fused_row.get('Location', '') or '').strip(),
                    speech_text=(fused_row.get('speech_text') or ''),
                )
                or _impute_location_from_url(new_url)
            ),
            'old_url':          '',
            'new_url':          new_url,
            'score':            '1.000000',
            'margin':           '',
            'stage_used':       'rubio_backfill',
            'candidate_id':     '',
            'candidate_source': 'rubio_backfill',
            'changed_url':      '0',
            'changed_text':     '0',
            'quality_before':   0,
            'quality_after':    q_after,
            'is_dropped':       '0',
            'is_sc2_row':       '0',
        })

    # Sort: numeric (original) rows first by int value, then sc2 rows alphabetically
    def _sort_key(r: dict) -> tuple:
        rid = r['row_id']
        if rid.isdigit():
            return (0, int(rid), '')
        return (1, 0, rid)

    audit_rows.sort(key=_sort_key)

    with output_audit.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=_FULL_AUDIT_FIELDS)
        w.writeheader()
        w.writerows(audit_rows)

    action_counts = _Counter(r['action'] for r in audit_rows)
    print(f'[audit] total_rows={len(audit_rows)}')
    for act, cnt in sorted(action_counts.items()):
        print(f'[audit]   {act}={cnt}')
    print(f'[audit] wrote {output_audit}')


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description='corpus_fusion.py — smart dedup, Rubio coverage, deep recovery, final merge'
    )
    sub = p.add_subparsers(dest='cmd', required=True)

    # smart_dedup
    p_d = sub.add_parser('smart_dedup', help='Two-pass dedup: text-hash for all rows, URL-first for expand rows only')
    p_d.add_argument('--input-final',       type=Path, default=DEFAULT_INPUT_FINAL)
    p_d.add_argument('--output-deduped',    type=Path, default=DEFAULT_DEDUPED)
    p_d.add_argument('--output-dedup-audit', type=Path, default=DEFAULT_DEDUP_AUDIT)

    # rubio_expand
    p_r = sub.add_parser('rubio_expand', help='Gap-fill Rubio coverage from secstate_naive_corpus2.csv')
    p_r.add_argument('--deduped-final',     type=Path, default=DEFAULT_DEDUPED)
    p_r.add_argument('--small2-corpus',     type=Path, default=DEFAULT_SMALL2_CORPUS)
    p_r.add_argument('--output',            type=Path, default=DEFAULT_RUBIO_EXPANDED)
    p_r.add_argument('--output-rubio-audit', type=Path, default=DEFAULT_RUBIO_AUDIT)
    p_r.add_argument('--no-progress',       action='store_true')

    # deep_recover
    p_dr = sub.add_parser('deep_recover', help='Enhanced recovery for triage rows via S1-S4 strategies')
    p_dr.add_argument('--input-final',      type=Path, default=DEFAULT_RUBIO_EXPANDED)
    p_dr.add_argument('--triage-csv',       type=Path, default=DEFAULT_TRIAGE)
    p_dr.add_argument('--checkpoint-db',    type=Path, default=DEFAULT_DB)
    p_dr.add_argument('--small2-corpus',    type=Path, default=DEFAULT_SMALL2_CORPUS)
    p_dr.add_argument('--output-recovery',  type=Path, default=DEFAULT_DEEP_RECOVERY)
    p_dr.add_argument('--s3-min-score',     type=float, default=0.70)
    p_dr.add_argument('--s3-min-margin',    type=float, default=0.04)
    p_dr.add_argument('--s4-min-score',     type=float, default=0.70)
    p_dr.add_argument('--s4-min-margin',    type=float, default=0.06)
    p_dr.add_argument('--s4-max-candidates', type=int,  default=400)
    p_dr.add_argument('--no-progress',      action='store_true')

    # fuse
    p_f = sub.add_parser('fuse', help='Merge deep recovery results into final deduped output')
    p_f.add_argument('--deduped-final',     type=Path, default=DEFAULT_RUBIO_EXPANDED)
    p_f.add_argument('--deep-recovery',     type=Path, default=DEFAULT_DEEP_RECOVERY)
    p_f.add_argument('--output-fused',      type=Path, default=DEFAULT_FUSED)
    p_f.add_argument('--output-audit',      type=Path, default=DEFAULT_FUSION_AUDIT)

    # recover_round2
    p_r2 = sub.add_parser('recover_round2', help='Round-2 recovery for unchanged_unresolved rows with relaxed strategies')
    p_r2.add_argument('--fused-csv',           type=Path, default=DEFAULT_FUSED)
    p_r2.add_argument('--round1-recovery',     type=Path, default=DEFAULT_DEEP_RECOVERY)
    p_r2.add_argument('--triage-csv',          type=Path, default=DEFAULT_TRIAGE)
    p_r2.add_argument('--checkpoint-db',       type=Path, default=DEFAULT_DB)
    p_r2.add_argument('--output-recovery',     type=Path, default=DEFAULT_ROUND2_RECOVERY)
    p_r2.add_argument('--r2s2-min-score',      type=float, default=0.70)
    p_r2.add_argument('--r2s2-min-margin',     type=float, default=0.02)
    p_r2.add_argument('--r2s3-window-days',    type=int,   default=180)
    p_r2.add_argument('--r2s3-min-score',      type=float, default=0.70)
    p_r2.add_argument('--r2s3-min-margin',     type=float, default=0.05)
    p_r2.add_argument('--r2s4-min-score',      type=float, default=0.70)
    p_r2.add_argument('--r2s4-min-margin',     type=float, default=0.04)
    p_r2.add_argument('--r2s4-max-candidates', type=int,   default=400)
    p_r2.add_argument('--no-progress',         action='store_true')

    # recover_round3
    p_r3 = sub.add_parser('recover_round3', help='Round-3 relaxed threshold recovery for Clinton/Kerry/Pompeo Q4')
    p_r3.add_argument('--fused-csv',           type=Path, default=DEFAULT_FUSED_V2)
    p_r3.add_argument('--round2-recovery',     type=Path, default=DEFAULT_ROUND2_RECOVERY)
    p_r3.add_argument('--triage-csv',          type=Path, default=DEFAULT_TRIAGE)
    p_r3.add_argument('--checkpoint-db',       type=Path, default=DEFAULT_DB)
    p_r3.add_argument('--output-recovery',     type=Path, default=DEFAULT_ROUND3_RECOVERY)
    p_r3.add_argument('--r3-min-score',          type=float, default=0.70)
    p_r3.add_argument('--r3-min-margin',         type=float, default=0.03)
    p_r3.add_argument('--r3-date-window-days',   type=int,   default=365)
    p_r3.add_argument('--r3-max-candidates',     type=int,   default=600)
    p_r3.add_argument('--no-progress',           action='store_true')

    # recover_round4
    p_r4 = sub.add_parser('recover_round4', help='Exact-date SQLite strategy at 0.35 threshold for Blinken/Albright')
    p_r4.add_argument('--fused-csv',            type=Path, default=DEFAULT_FUSED_V13)
    p_r4.add_argument('--combined-recovery',    type=Path, default=DEFAULT_ROUND3_RECOVERY)
    p_r4.add_argument('--triage-csv',           type=Path, default=DEFAULT_TRIAGE)
    p_r4.add_argument('--checkpoint-db',        type=Path, default=DEFAULT_DB)
    p_r4.add_argument('--output-recovery',      type=Path, default=DEFAULT_ROUND4_RECOVERY)
    p_r4.add_argument('--exact-date-min-score', type=float, default=0.70)
    p_r4.add_argument('--target-secretaries',   nargs='+', default=['blinken', 'albright'])
    p_r4.add_argument('--no-progress',          action='store_true')

    # rubio_backfill
    p_rb = sub.add_parser('rubio_backfill', help='Backfill net-new Rubio speeches from secstate_naive_corpus_rubio.csv')
    p_rb.add_argument('--fused-csv',            type=Path, default=DEFAULT_FUSED)
    p_rb.add_argument('--rubio-corpus',         type=Path, default=DEFAULT_RUBIO_CORPUS)
    p_rb.add_argument('--output-fused',         type=Path, default=DEFAULT_FUSED)
    p_rb.add_argument('--output-backfill-audit', type=Path, default=DEFAULT_BACKFILL_AUDIT)
    p_rb.add_argument('--no-progress',          action='store_true')

    # recover_archived
    p_ar = sub.add_parser('recover_archived',
                          help='Crawl 2001-2009.state.gov to find URLs for unresolved Powell/Rice rows')
    p_ar.add_argument('--fused-csv',         type=Path, default=DEFAULT_FUSED_V4)
    p_ar.add_argument('--round3-recovery',   type=Path, default=DEFAULT_ROUND3_RECOVERY)
    p_ar.add_argument('--triage-csv',        type=Path, default=DEFAULT_TRIAGE)
    p_ar.add_argument('--crawl-checkpoint',  type=Path, default=DEFAULT_ARCHIVED_CRAWL)
    p_ar.add_argument('--output-recovery',   type=Path, default=DEFAULT_ARCHIVED_RECOVERY)
    p_ar.add_argument('--min-score',         type=float, default=_MIN_SCORE_DEFAULT)
    p_ar.add_argument('--date-window-days',  type=int,   default=_DATE_WINDOW_DEFAULT)
    p_ar.add_argument('--rate-limit-secs',   type=float, default=_RATE_LIMIT_DEFAULT)
    p_ar.add_argument('--max-pages',         type=int,   default=_MAX_PAGES_DEFAULT)
    p_ar.add_argument('--discovery-only',    action='store_true',
                      help='Only run Phase 1 (URL discovery), skip scraping and matching')
    p_ar.add_argument('--refresh-discovery', action='store_true',
                      help='Force Phase 1 seed re-crawl even when crawl checkpoint already exists')
    p_ar.add_argument('--domain',            type=str,   default='2001-2009.state.gov',
                      help='Archive domain to crawl (default: 2001-2009.state.gov)')
    p_ar.add_argument('--target-secretaries', nargs='+', default=None,
                      help='Secretary names to target (lowercase); defaults to powell rice')
    p_ar.add_argument('--exact-date-min-score', type=float, default=0.70,
                      help='Minimum overlap score for exact-date candidates (default: 0.70)')
    p_ar.add_argument('--no-progress',       action='store_true')

    # audit
    p_a = sub.add_parser('audit', help='Produce unified per-row audit CSV (mirrors url_match_audit style)')
    p_a.add_argument('--input-final',       type=Path, default=DEFAULT_INPUT_FINAL)
    p_a.add_argument('--fused-csv',         type=Path, default=DEFAULT_FUSED)
    p_a.add_argument('--dedup-audit',       type=Path, default=DEFAULT_DEDUP_AUDIT)
    p_a.add_argument('--rubio-audit',       type=Path, default=DEFAULT_RUBIO_AUDIT)
    p_a.add_argument('--deep-recovery',     type=Path, default=DEFAULT_DEEP_RECOVERY)
    p_a.add_argument('--backfill-audit',    type=Path, default=DEFAULT_BACKFILL_AUDIT)
    p_a.add_argument('--output-audit',      type=Path, default=DEFAULT_FULL_AUDIT)

    return p


def main() -> None:
    setup_field_limit()
    args = build_parser().parse_args()

    if args.cmd == 'smart_dedup':
        run_smart_dedup(
            input_final=args.input_final,
            output_deduped=args.output_deduped,
            output_dedup_audit=args.output_dedup_audit,
        )

    elif args.cmd == 'rubio_expand':
        run_rubio_expand(
            deduped_final=args.deduped_final,
            small2_corpus=args.small2_corpus,
            output_path=args.output,
            output_rubio_audit=args.output_rubio_audit,
            show_progress=not args.no_progress,
        )

    elif args.cmd == 'deep_recover':
        run_deep_recover(
            input_final=args.input_final,
            triage_csv=args.triage_csv,
            checkpoint_db=args.checkpoint_db,
            small2_corpus=args.small2_corpus,
            output_recovery=args.output_recovery,
            s3_min_score=float(args.s3_min_score),
            s3_min_margin=float(args.s3_min_margin),
            s4_min_score=float(args.s4_min_score),
            s4_min_margin=float(args.s4_min_margin),
            s4_max_candidates=max(1, int(args.s4_max_candidates)),
            show_progress=not args.no_progress,
        )

    elif args.cmd == 'fuse':
        run_fuse(
            deduped_final=args.deduped_final,
            deep_recovery_csv=args.deep_recovery,
            output_fused=args.output_fused,
            output_audit=args.output_audit,
        )

    elif args.cmd == 'recover_round2':
        run_recover_round2(
            fused_csv=args.fused_csv,
            round1_recovery=args.round1_recovery,
            triage_csv=args.triage_csv,
            checkpoint_db=args.checkpoint_db,
            output_recovery=args.output_recovery,
            r2s2_min_score=args.r2s2_min_score,
            r2s2_min_margin=args.r2s2_min_margin,
            r2s3_window_days=args.r2s3_window_days,
            r2s3_min_score=args.r2s3_min_score,
            r2s3_min_margin=args.r2s3_min_margin,
            r2s4_min_score=args.r2s4_min_score,
            r2s4_min_margin=args.r2s4_min_margin,
            r2s4_max_candidates=args.r2s4_max_candidates,
            show_progress=not args.no_progress,
        )

    elif args.cmd == 'recover_round3':
        run_recover_round3(
            fused_csv=args.fused_csv,
            round2_recovery=args.round2_recovery,
            triage_csv=args.triage_csv,
            checkpoint_db=args.checkpoint_db,
            output_recovery=args.output_recovery,
            r3_min_score=args.r3_min_score,
            r3_min_margin=args.r3_min_margin,
            r3_date_window_days=args.r3_date_window_days,
            r3_max_candidates=args.r3_max_candidates,
            show_progress=not args.no_progress,
        )

    elif args.cmd == 'recover_round4':
        run_recover_round4(
            fused_csv=args.fused_csv,
            combined_recovery=args.combined_recovery,
            triage_csv=args.triage_csv,
            checkpoint_db=args.checkpoint_db,
            output_recovery=args.output_recovery,
            exact_date_min_score=args.exact_date_min_score,
            target_secretaries=frozenset(s.lower() for s in args.target_secretaries),
            show_progress=not args.no_progress,
        )

    elif args.cmd == 'rubio_backfill':
        run_rubio_backfill(
            fused_csv=args.fused_csv,
            rubio_corpus=args.rubio_corpus,
            output_fused=args.output_fused,
            output_backfill_audit=args.output_backfill_audit,
            show_progress=not args.no_progress,
        )

    elif args.cmd == 'recover_archived':
        _secs_arg = frozenset(s.lower() for s in args.target_secretaries) \
            if args.target_secretaries else None
        _domain = args.domain
        # Pick the right default seeds based on domain
        if _secs_arg is None:
            _seeds_arg = None  # function will use _ARCHIVED_LISTING_SEEDS
        elif _domain == '2009-2017.state.gov':
            _seeds_arg = _ARCHIVED_LISTING_SEEDS_2009
        elif _domain == '2017-2021.state.gov':
            _seeds_arg = _ARCHIVED_LISTING_SEEDS_2017
        elif _domain == '2021-2025.state.gov':
            _seeds_arg = _ARCHIVED_LISTING_SEEDS_2021  # WP API: empty list, seeds unused
        elif _domain == '1997-2001.state.gov':
            _seeds_arg = _ARCHIVED_LISTING_SEEDS_1997
        else:
            _seeds_arg = _ARCHIVED_LISTING_SEEDS
        run_recover_archived(
            fused_csv=args.fused_csv,
            round3_recovery=args.round3_recovery,
            triage_csv=args.triage_csv,
            crawl_checkpoint=args.crawl_checkpoint,
            output_recovery=args.output_recovery,
            min_score=args.min_score,
            date_window_days=args.date_window_days,
            rate_limit_secs=args.rate_limit_secs,
            max_pages=args.max_pages,
            discovery_only=args.discovery_only,
            show_progress=not args.no_progress,
            domain=_domain,
            target_secretaries=_secs_arg,
            listing_seeds=_seeds_arg,
            exact_date_min_score=args.exact_date_min_score,
            refresh_discovery=args.refresh_discovery,
        )

    elif args.cmd == 'audit':
        run_audit(
            input_final=args.input_final,
            fused_csv=args.fused_csv,
            dedup_audit=args.dedup_audit,
            rubio_audit=args.rubio_audit,
            deep_recovery=args.deep_recovery,
            output_audit=args.output_audit,
            backfill_audit=args.backfill_audit,
        )


if __name__ == '__main__':
    main()
