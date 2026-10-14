#!/usr/bin/env python3
"""Ongoing Rubio corpus update — for GitHub Actions weekly automation.

Takes a full SOS corpus CSV (v4.23+ schema, 10 base columns, optionally
already carrying corpus_enrich_v1.py's enrichment columns) and a fresh
rubio_backfill output CSV (17 columns), deduplicates, cleans new rows
through the same pipeline as corpus_finalize_v4_23.py, appends them, runs
corpus_enrich_v1.py's enrichment in --incremental mode (only the newly
merged rows lack a Row Hash, so already-enriched existing rows are left
untouched -- their enrichment columns pass through unchanged), and calls
corpus_verify_research_grade.py to gate the output.

Usage:
    python corpus_rubio_update.py \\
        --corpus  data/corpus.csv \\
        --backfill data/rubio_new.csv \\
        --output  data/corpus_updated.csv

Exit codes:
    0  — new rows found, merged, and verifier passed (or 0 new rows found)
    1  — verifier failed
    2  — unexpected error
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
import subprocess
import sys
from pathlib import Path

# ── Import cleaning pipeline from finalize script ────────────────────────────
# Both scripts live in the same directory (Project2/ locally; scripts/ in repo).
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
try:
    from corpus_finalize_v4_30 import (  # type: ignore[import]
        normalize_encoding,
        strip_footer,
        strip_leading_headers,
        strip_boilerplate,
        strip_stage_directions,
        SPEECH_TYPE_TITLECASE,
        _RECOVERY_NAV_GUARD_RE,
    )
    from corpus_enrich_v1 import (  # type: ignore[import]
        enrich_row,
        enrich_rows_ner,
        get_nlp,
        write_manifest,
        backfill_canonical_urls,
        write_people_audit,
        write_unresolved_gpe_audit,
        OUTPUT_COLUMNS,
    )
except ImportError as _e:
    sys.exit(f"ERROR: cannot import cleaning/enrichment pipeline — {_e}")

try:
    from corpus_keys import (  # type: ignore[import]
        canonical_url_key as _canonical_url_key,
        canonicalize_live_url,
        slug_date_key,
        url_slug,
    )
    from corpus_location import (  # type: ignore[import]
        build_gazetteer,
        normalize_location,
        assert_schema_safe,
    )
except ImportError as _e:
    sys.exit(f"ERROR: cannot import corpus_keys — {_e}")

csv.field_size_limit(10 ** 8)

# OUTPUT_COLUMNS comes from corpus_enrich_v1.py (10 base columns + its
# enrichment columns) rather than being hardcoded here, so this script never
# silently drops enrichment columns already present on rows loaded from
# args.corpus when it writes the merged+enriched output back out.

def _clean_new_row(row: dict[str, str]) -> dict[str, str]:
    """Apply encoding normalization + boilerplate stripping to a new Rubio row."""
    title = row.get('Title', '') or ''
    for field in ('SOS Text', 'Natural Text'):
        text = normalize_encoding(row.get(field, '') or '')
        text = strip_boilerplate(strip_footer(strip_leading_headers(text)), title)
        row[field] = text

    # Stage direction stripping (audience reactions, (inaudible) markers, language/
    # transcript annotations, ceremony directions) — SOS Text only, matching the
    # canonical pipeline in corpus_finalize_v4_30.py.
    row['SOS Text'] = strip_stage_directions(row['SOS Text'])

    # Natural Text recovery: if nat_wc < sos_wc × 0.5 and nat_wc < 50,
    # repopulate from Raw Text (same logic as corpus_finalize_v4_23.py).
    nat_wc = len(row['Natural Text'].split()) if row['Natural Text'] else 0
    sos_wc = len(row['SOS Text'].split()) if row['SOS Text'] else 0
    if nat_wc < 50 and sos_wc > 0 and nat_wc < sos_wc * 0.5:
        raw = normalize_encoding(row.get('Raw Text', '') or '').strip()
        if raw and len(raw.split()) > nat_wc * 2:
            recovered = strip_boilerplate(
                strip_footer(strip_leading_headers(raw)), title
            )
            if len(recovered.split()) > nat_wc and not _RECOVERY_NAV_GUARD_RE.search(recovered[:400]):
                row['Natural Text'] = recovered

    return row


def _map_backfill_row(rec: dict[str, str], next_rid: int) -> dict[str, str]:
    """Map a 17-column rubio_backfill row to the 10-column corpus schema."""
    st = SPEECH_TYPE_TITLECASE.get(
        (rec.get('speech_type', '') or 'remarks').strip().lower(),
        'Remarks',
    )
    return {
        'Row Id':       str(next_rid),
        'Secretary':    'Rubio',
        'Date':         rec.get('Date', ''),
        'Title':        rec.get('Title', ''),
        'Speech Type':  st,
        # No default here on purpose. An absent Location used to become
        # 'Washington, D.C.', inventing a dateline for a speech that might
        # have been given anywhere. It is now normalized (and quarantined if
        # unresolvable) by the caller.
        'Location':     (rec.get('Location') or '').strip(),
        'URL':          rec.get('url', ''),
        'SOS Text':     rec.get('speech_text', ''),
        'Natural Text': rec.get('speaker_text', ''),
        'Raw Text':     rec.get('raw_text', ''),
    }


_QUARANTINE_COLUMNS = [
    'url', 'canonical_url', 'slug', 'date', 'title', 'speech_type',
    'location_raw', 'normalize_method', 'sos_word_count', 'natural_word_count',
]


def _write_quarantine(path: Path, records: list[dict]) -> None:
    """Sidecar of rows dropped for an unresolvable Location.

    Same convention as the enrichment audit sidecars this script already
    writes. Deliberately carries no speech text: it is meant to stay small
    and reviewable. The re-add path is "teach the gazetteer, then re-run with
    --full-rescan", not "paste the text back in".
    """
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=_QUARANTINE_COLUMNS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(records)


def main() -> None:
    ap = argparse.ArgumentParser(description='Weekly Rubio corpus update for GitHub Actions')
    ap.add_argument('--corpus',   required=True, help='Existing full-corpus CSV (10-column v4.23+ schema)')
    ap.add_argument('--backfill', required=True, help='rubio_backfill output CSV (17-column schema)')
    ap.add_argument('--output',   required=True, help='Merged output CSV path')
    ap.add_argument('--max-gap-days', type=int, default=90,
                    help='Max allowed date gap for verifier (default: 90 to accommodate Rubio recess periods)')
    ap.add_argument('--skip-ner', action='store_true',
                    help='Skip Phase 3-4 (People/Countries Mentioned) NER pass -- '
                         'leaves those columns unset on new rows. Not recommended for '
                         'normal weekly runs; useful for a fast dry run.')
    ap.add_argument('--spacy-model', default='en_core_web_lg')
    args = ap.parse_args()

    # ── Load existing corpus ─────────────────────────────────────────────────
    print(f'Loading corpus: {args.corpus}')
    with open(args.corpus, newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    print(f'  {len(rows):,} existing rows')

    # Three dedup keys, not one. A URL key alone is not rename-proof: in
    # September 2026 state.gov renamed /releases/office-of-the-spokesperson/
    # to /releases/office-of-the-spokesman/, and 565 already-collected Rubio
    # speeches came back looking brand new. They were one unrelated Location
    # failure away from being published as duplicates.
    #   url      — the historical key, kept so behaviour never regresses
    #   canon    — the same key after applying the known path renames
    #   slugdate — (last path segment, Date); survives ANY path rename, and is
    #              collision-free across all 17,617 corpus rows
    existing_urls: set[str] = set()
    existing_canon: set[str] = set()
    existing_slugdt: set[str] = set()
    for r in rows:
        url = r.get('URL', '')
        existing_urls.add(_canonical_url_key(url))
        existing_canon.add(_canonical_url_key(canonicalize_live_url(url)))
        k = slug_date_key(url, r.get('Date', ''))
        if k:
            existing_slugdt.add(k)
    max_rid = max((int(r.get('Row Id', 0)) for r in rows), default=-1)

    # Location gazetteer, derived from the corpus we are merging into -- so
    # new rows inherit the project's own curated vocabulary rather than a
    # hand-written table that would drift away from it.
    # Fill Canonical URL on any row that predates the column (idempotent).
    n_canon = backfill_canonical_urls(rows)
    if n_canon:
        print(f'  Canonical URL: filled {n_canon:,} row(s) from URL + rename map')

    gaz = build_gazetteer(rows)
    print(f'  Location gazetteer: {len(gaz.city):,} cities, '
          f'{len(gaz.rhs_vocab):,} region spellings')

    # ── Load and deduplicate backfill ────────────────────────────────────────
    print(f'Loading backfill: {args.backfill}')
    new_rows: list[dict[str, str]] = []
    quarantined: list[dict] = []
    loc_methods: Counter = Counter()
    seen_urls: set[str] = set()
    seen_slugdt: set[str] = set()
    n_candidates = 0
    rej = {'url': 0, 'canonical-url': 0, 'slug+date': 0, 'within-backfill': 0}

    with open(args.backfill, newline='', encoding='utf-8') as f:
        for rec in csv.DictReader(f):
            raw_url = rec.get('url', '')
            ckey = _canonical_url_key(raw_url)
            if not ckey:
                continue
            n_candidates += 1
            canon = _canonical_url_key(canonicalize_live_url(raw_url))
            sdkey = slug_date_key(raw_url, rec.get('Date', ''))

            if ckey in existing_urls:
                rej['url'] += 1
                continue
            if canon in existing_canon:
                rej['canonical-url'] += 1
                continue
            if sdkey and sdkey in existing_slugdt:
                rej['slug+date'] += 1
                continue
            if ckey in seen_urls or (sdkey and sdkey in seen_slugdt):
                rej['within-backfill'] += 1
                continue

            seen_urls.add(ckey)
            if sdkey:
                seen_slugdt.add(sdkey)

            # Location must be strict "City, Region" (verifier Checks L/O/P).
            # Unresolvable means quarantine, never a fabricated default --
            # an "En Route to Doha" speech is not a Washington speech.
            raw_loc = (rec.get('Location') or '').strip()
            norm_loc, method = normalize_location(raw_loc, gaz)
            if not norm_loc or not assert_schema_safe(norm_loc):
                quarantined.append({
                    'url': raw_url,
                    'canonical_url': canonicalize_live_url(raw_url),
                    'slug': url_slug(raw_url),
                    'date': rec.get('Date', ''),
                    'title': rec.get('Title', ''),
                    'speech_type': rec.get('speech_type', ''),
                    'location_raw': raw_loc,
                    'normalize_method': method,
                    'sos_word_count': len((rec.get('speech_text') or '').split()),
                    'natural_word_count': len((rec.get('speaker_text') or '').split()),
                })
                continue

            max_rid += 1
            mapped = _map_backfill_row(rec, max_rid)
            mapped['Location'] = norm_loc
            mapped['Canonical URL'] = canonicalize_live_url(raw_url)
            loc_methods[method] += 1
            cleaned = _clean_new_row(mapped)
            new_rows.append(cleaned)

    print(f'  {n_candidates:,} candidate rows, {len(new_rows):,} new after dedup '
          f'(rejected: ' + ', '.join(f'{k} {v}' for k, v in rej.items()) + ')')
    if rej['slug+date'] or rej['canonical-url']:
        print(f'  NOTE: {rej["slug+date"] + rej["canonical-url"]:,} row(s) were caught ONLY by a '
              f'rename-proof key, not by the raw URL — state.gov has renamed a path segment. '
              f'Consider adding it to PATH_RENAMES in scripts/corpus_keys.py.')
    print(f'  {len(new_rows):,} new Rubio rows (after deduplication)')
    if loc_methods:
        print('  Location normalization: '
              + ', '.join(f'{k} {v}' for k, v in loc_methods.most_common()))

    # ── Quarantine sidecar ───────────────────────────────────────────────────
    # Written BEFORE the early exit below: if every new row is quarantined we
    # still must leave evidence, otherwise choosing quarantine over a
    # fabricated default would just be a silent drop.
    if quarantined:
        qpath = Path(args.output).parent / 'rubio_unresolved_location.csv'
        _write_quarantine(qpath, quarantined)
        print('')
        print(f'  !! QUARANTINED {len(quarantined):,} row(s) with unresolvable Location.')
        print(f'     Not merged, not released. Triage: {qpath}')
        for q in quarantined[:10]:
            print(f'       [{q["normalize_method"]}] {q["location_raw"]!r} — {q["url"]}')
        if len(quarantined) > 10:
            print(f'       ... and {len(quarantined) - 10} more')
        print('')

    if not new_rows:
        print('No new rows — corpus unchanged. Skipping output write and verification.')
        sys.exit(0)

    # ── Merge and renumber ───────────────────────────────────────────────────
    merged = rows + new_rows
    for i, r in enumerate(merged):
        r['Row Id'] = str(i)

    # ── Enrich ────────────────────────────────────────────────────────────────
    # --incremental semantics: existing rows already carry a Row Hash (they
    # came from an already-enriched args.corpus) and are left untouched;
    # only the freshly merged Rubio rows (no Row Hash yet) get word counts +
    # a Row Hash computed. If args.corpus predates enrichment entirely (no
    # rows have a Row Hash), this naturally enriches the whole corpus once.
    enriched_count = sum(enrich_row(r, incremental=True) for r in merged)
    print(f'Enriched {enriched_count:,} row(s) Phase 1-2 (incremental).')

    # Phase 3-4 NER pass: same --incremental semantics (key-presence check
    # inside enrich_rows_ner, since People/Countries Mentioned can be
    # legitimately empty). A weekly run only has a handful of new rows, so
    # n_process defaults to 1 here -- not worth the multiprocess spin-up
    # cost for a handful of documents (see corpus_enrich_v1.py's own
    # --n-process guidance for the full-corpus case).
    if not args.skip_ner:
        nlp = get_nlp(args.spacy_model)
        audit_log: list = []
        unresolved_log: list = []
        ner_count = enrich_rows_ner(
            merged, nlp, incremental=True,
            audit_log=audit_log, unresolved_log=unresolved_log,
        )
        print(f'Enriched {ner_count:,} row(s) Phase 3-4 NER (incremental).')
        people_audit_path = Path(args.output).parent / 'rubio_update_people_mention_audit.csv'
        unresolved_audit_path = Path(args.output).parent / 'rubio_update_unresolved_gpe_audit.csv'
        write_people_audit(people_audit_path, audit_log)
        write_unresolved_gpe_audit(unresolved_audit_path, unresolved_log)

    # ── Write output ─────────────────────────────────────────────────────────
    print(f'Writing output: {args.output}')
    with open(args.output, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(merged)
    print(f'  {len(merged):,} total rows written')

    manifest_path = Path(args.output + '.manifest.json')
    write_manifest(manifest_path, Path(args.output), merged)
    print(f'  Wrote manifest: {manifest_path}')

    # ── Verify ───────────────────────────────────────────────────────────────
    verifier = _HERE / 'corpus_verify_research_grade.py'
    print(f'Running verifier: {verifier}')
    result = subprocess.run(
        [sys.executable, str(verifier),
         '--input', args.output,
         '--max-gap-days', str(args.max_gap_days),
         '--baseline', args.corpus],
        capture_output=False,
    )
    if result.returncode != 0:
        print('ERROR: verifier FAILED — output not suitable for release.')
        sys.exit(1)

    print(f'\nMerged {len(new_rows):,} new Rubio rows. Total: {len(merged):,} rows. Verifier: PASSED.')


if __name__ == '__main__':
    main()
