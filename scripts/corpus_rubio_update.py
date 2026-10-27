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
    0  — new rows found, merged, and verifier passed (or 0 new rows found).
         Still 0 when some new rows were HELD (see below): the rows that pass
         are released, and the hold is signalled by --held-flag instead.
    1  — verifier failed on a corpus-global check
    2  — unexpected error

Never drop a speech (2026-10 rubio coverage audit, M-01/M-02/M-03/M-06):
  * Location -- a row whose Location cannot be confidently normalized is
    merged with corpus_location.provisional_location()'s value, never
    dropped. 'Location Method' / 'Location Provisional' are recorded per row
    in data/rubio_merge_provenance.csv (sidecar only -- they are NOT in the
    published schema, see _PROVENANCE_COLUMNS).
  * Date -- an empty Date falls back to the input's 'listing_date', then the
    URL's /YYYY/MM/ month. A row with none of the three is held, not merged.
  * Verifier -- the verifier is run on the NEW rows alone first. A new row
    that fails a check on its own is held in --held-out (default
    data/rubio_held.csv: url, check, detail, first_held, attempts) and the
    rest are merged. Only the full-corpus run that follows is a hard gate,
    so a corpus-global failure still blocks the release.
  * Held rows are retried by feeding the held CSV's `url` column back to
    rubio_backfill.py as supplemental URLs on the next run. A held URL that
    is merged (or found to already be in the corpus) is dropped from the
    file; one that is not seen at all this run is kept as is.
  * When anything is held, --held-flag (default data/rubio_held.flag) is
    written with the count, and `held=N` is appended to $GITHUB_OUTPUT, so
    the workflow can open an issue. The flag is removed when nothing is held.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import date, datetime, timezone
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
    import corpus_title  # type: ignore[import]
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
        provisional_location,
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
    # canonical pipeline in corpus_finalize_v4_30.py. rubio_path=True adds the
    # X-09 "(Via translation)" strip, exactly as rubio_reextract.clean_sos_text
    # does; this function only ever sees new Rubio rows, so no other
    # secretary's text is affected.
    row['SOS Text'] = strip_stage_directions(row['SOS Text'], rubio_path=True)

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


# No Rubio-only Speech Type additions: the controlled vocabulary is unchanged
# (decided 2026-10-21). Kept as an alias so the mapping has one call site.
_RUBIO_SPEECH_TYPE_TITLECASE = SPEECH_TYPE_TITLECASE


def _map_backfill_row(rec: dict[str, str], next_rid: int) -> dict[str, str]:
    """Map a rubio_backfill row to the 10-column corpus schema."""
    raw_st = (rec.get('speech_type', '') or 'remarks').strip().lower()
    st = _RUBIO_SPEECH_TYPE_TITLECASE.get(raw_st)
    if st is None:
        st = 'Remarks'
        print(f'  NOTE: unmapped speech_type {raw_st!r} -> Remarks ({rec.get("url", "")})')
    return {
        'Row Id':       str(next_rid),
        'Secretary':    'Rubio',
        'Date':         rec.get('Date', ''),
        # The headline as rubio_backfill read it, normalised the way every other
        # Title is (finalize's ftfy pass: quotes, NBSP, mojibake) so Check AJ
        # compares like with like.
        'Title':        corpus_title.fix_text(corpus_title.clean_title(rec.get('Title', '') or '')),
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
    # Since 2026-10 these rows are MERGED with a provisional Location rather
    # than dropped; the three columns below say with what.
    'location_provisional_value', 'location_method', 'disposition',
]


def _write_quarantine(path: Path, records: list[dict]) -> None:
    """Sidecar of rows whose Location could not be confidently normalized.

    Same convention as the enrichment audit sidecars this script already
    writes. Deliberately carries no speech text: it is meant to stay small
    and reviewable. Since 2026-10 the rows listed here are merged with a
    provisional Location (disposition 'merged_provisional') -- this file is
    the triage list for "teach the gazetteer / venue alias table", not a list
    of lost speeches.
    """
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=_QUARANTINE_COLUMNS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(records)


# Per-row provenance of every merged new row. 'Location Method' and
# 'Location Provisional' live HERE, not in the published CSV: the verifier's
# schema check fails on any column outside EXPECTED_COLUMNS +
# KNOWN_ENRICH_COLUMNS, and corpus_enrich_v1.OUTPUT_COLUMNS (which the
# writer below uses with extrasaction='ignore') does not list them. Promoting
# them to the schema means adding them to both of those lists -- never to
# corpus_enrich_v1._PHASE_1_2_COLUMNS, whose truthiness test would make every
# existing row look un-enriched and recompute it.
# 'Modality' (spoken | written | video | joint, plan step 3) and 'Untranscribed
# Rubio Segments' (count of "(In Spanish.)"-type placeholders in his own text,
# audit X-10) live here for the same reason: a new published column would add
# an empty field to every other secretary's row, which must stay bit-identical.
_PROVENANCE_COLUMNS = [
    'Row Id', 'URL', 'Date', 'Date Method', 'Location', 'Location Raw',
    'Location Method', 'Location Provisional', 'Speech Type',
    'Modality', 'Untranscribed Rubio Segments',
]


def _write_csv(path: Path, columns: list[str], records: list[dict]) -> None:
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(records)


# ── Date fallback (plan step 6d, M-06) ───────────────────────────────────────
# _parse_date in rubio_backfill.py returns '' when neither JSON-LD, the URL nor
# the block has a date. An empty Date used to reach the verifier ("Empty Date",
# "Bad date format") and block the whole week. Fallback order after the
# scraper's own: listing date (the archive listing shows one per item) ->
# the URL's /YYYY/MM/ month, as YYYY-MM-01. The day of a URL-month date is a
# placeholder, so the row's Date Method in the provenance sidecar says so.
_URL_YM_RE = re.compile(r'/((?:19|20)\d{2})/(\d{2})/')
_LISTING_DATE_FORMATS = ('%B %d, %Y', '%b %d, %Y', '%b. %d, %Y', '%m/%d/%Y')


def _iso_or_empty(value: str) -> str:
    value = (value or '').strip()
    if not value:
        return ''
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        pass
    # "Sept. 4, 2026" -- AP style, which strptime's %b does not know.
    cleaned = re.sub(r'^Sept\b\.?', 'Sep', value, flags=re.IGNORECASE)
    for fmt in _LISTING_DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return ''


def _resolve_date(rec: dict[str, str]) -> tuple[str, str]:
    """(ISO date, method) for a backfill record; ('', 'undated') if none."""
    d = _iso_or_empty(rec.get('Date', ''))
    if d:
        return d, 'scraped'
    d = _iso_or_empty(rec.get('listing_date', ''))
    if d:
        return d, 'listing_date'
    m = _URL_YM_RE.search(rec.get('url', '') or '')
    if m and 1 <= int(m.group(2)) <= 12:
        return f'{m.group(1)}-{m.group(2)}-01', 'url_month'
    return '', 'undated'


# ── Held rows (plan step 6b, M-03) ───────────────────────────────────────────
_HELD_COLUMNS = ['url', 'check', 'detail', 'first_held', 'attempts']


def _read_held(path: Path) -> dict[str, dict]:
    """Previous run's held list, keyed by canonical URL key."""
    if not path.is_file():
        return {}
    with open(path, newline='', encoding='utf-8') as f:
        return {_canonical_url_key(r.get('url', '')): r
                for r in csv.DictReader(f) if r.get('url')}


def _update_held(previous: dict[str, dict], held_now: list[dict],
                 seen_keys: set[str], today: str) -> list[dict]:
    """Carry first_held / attempts forward.

    held_now     -- rows held THIS run (url, check, detail)
    seen_keys    -- canonical keys of every backfill candidate this run; a
                    previously held URL that was seen and is not held again
                    was merged or deduplicated, so it leaves the list. One
                    that was not seen (its fetch failed, or it was not fed
                    back) stays as it was, to be retried next time.
    """
    out: dict[str, dict] = {}
    for h in held_now:
        key = _canonical_url_key(h['url'])
        prev = previous.get(key, {})
        try:
            attempts = int(prev.get('attempts') or 0) + 1
        except ValueError:
            attempts = 1
        out[key] = {
            'url': h['url'],
            'check': h['check'],
            'detail': h['detail'],
            'first_held': prev.get('first_held') or today,
            'attempts': str(attempts),
        }
    for key, prev in previous.items():
        if key not in out and key not in seen_keys:
            out[key] = {c: prev.get(c, '') for c in _HELD_COLUMNS}
    return list(out.values())


_FAIL_LINE_RE = re.compile(r'^\s*FAIL\s+(.*)$', re.MULTILINE)


def _run_verifier(verifier: Path, csv_path: Path, extra: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, str(verifier), '--input', str(csv_path)] + extra,
        capture_output=True, text=True,
    )
    return result.returncode, (result.stdout or '') + (result.stderr or '')


def _check_name(fail_msg: str) -> str:
    """'Check T: Speakers Present empty: ...' -> 'Check T'; otherwise the
    message up to its first ':' (the unlettered checks, e.g. 'Empty Date')."""
    m = re.match(r'(Check [A-Z]{1,2})\b', fail_msg)
    return m.group(1) if m else fail_msg.split(':', 1)[0].strip()[:60]


def gate_new_rows(new_rows: list[dict], verifier: Path,
                  runner=_run_verifier) -> dict[int, list[str]]:
    """Run the verifier's checks on the NEW rows alone; return
    {index into new_rows: [FAIL messages]} for rows that fail on their own.

    The verifier has no importable per-row API (every check is inline in its
    main()), so this runs it as a subprocess on a CSV holding only new rows,
    Row Ids renumbered 0..k-1 so the sequence check holds. With no
    --baseline (Check Q skipped), a gap limit nothing can exceed, and a
    manifest path that does not exist (Check S's manifest sub-check only
    warns), what can still fail on such a file is exactly the row-local
    checks. One batch run in the common case; only when it fails is each
    row run alone to find the culprits. A batch failure that no single row
    reproduces is cross-row (e.g. two new rows with the same content): those
    are left for the full-corpus run, which stays a hard gate.
    """
    if not new_rows:
        return {}
    cols = list(OUTPUT_COLUMNS)
    with tempfile.TemporaryDirectory(prefix='rubio_gate_') as td:
        tdp = Path(td)
        # --rubio-coverage adds the row-local AD-AG checks (turn recall, other
        # speakers' words, byline, mid-word start); --audit-checks adds AK-AL (the
        # 2026-10-27 audit's detectors: text a repair would still change, People
        # Mentioned values that are not people). With no --baseline every
        # probe row counts as new, so a FAIL holds the row.
        extra = ['--max-gap-days', str(10 ** 7),
                 '--manifest-path', str(tdp / 'no_manifest.json'),
                 '--rubio-coverage', '--titles', '--audit-checks']

        def _probe(rows: list[dict], name: str) -> tuple[int, str]:
            path = tdp / name
            probe = [dict(r, **{'Row Id': str(i)}) for i, r in enumerate(rows)]
            _write_csv(path, cols, probe)
            return runner(verifier, path, extra)

        rc, _ = _probe(new_rows, 'batch.csv')
        if rc == 0:
            return {}
        failures: dict[int, list[str]] = {}
        for i, row in enumerate(new_rows):
            rc, out = _probe([row], f'row_{i}.csv')
            if rc != 0:
                msgs = _FAIL_LINE_RE.findall(out) or [f'verifier exit {rc}']
                failures[i] = [m.strip() for m in msgs]
        return failures


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
    ap.add_argument('--held-out', default=None,
                    help='Held-rows CSV (url, check, detail, first_held, attempts). '
                         'Read for first_held/attempts if it exists, then rewritten. '
                         'Feed its url column back to rubio_backfill.py next run. '
                         'Default: <output dir>/rubio_held.csv')
    ap.add_argument('--held-flag', default=None,
                    help='Written (with the held count) when any row is held; removed '
                         'otherwise. Default: <output dir>/rubio_held.flag')
    args = ap.parse_args()
    out_dir = Path(args.output).parent
    held_path = Path(args.held_out) if args.held_out else out_dir / 'rubio_held.csv'
    flag_path = Path(args.held_flag) if args.held_flag else out_dir / 'rubio_held.flag'
    today = datetime.now(timezone.utc).date().isoformat()
    previous_held = _read_held(held_path)

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
    held_now: list[dict] = []
    seen_keys: set[str] = set()
    provenance: dict[int, dict] = {}   # id(row dict) -> sidecar record
    date_methods: Counter = Counter()
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
            seen_keys.add(ckey)
            canon = _canonical_url_key(canonicalize_live_url(raw_url))
            # Date fallback BEFORE the slug+date key, so a row the scraper
            # left undated still dedups against its own earlier copy.
            iso_date, date_method = _resolve_date(rec)
            rec = dict(rec, Date=iso_date)
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

            # No date from the page, the listing or the URL: hold the row
            # (retried next run) rather than let it fail the whole release.
            if not iso_date:
                held_now.append({'url': raw_url, 'check': 'undated',
                                 'detail': 'no Date from page, listing_date or URL month'})
                continue
            date_methods[date_method] += 1

            # Location must be strict "City, Region" (verifier Checks L/O/P).
            # Unresolvable no longer means dropped: the row is merged with a
            # provisional value (still never a fabricated Washington -- an
            # "En Route to Doha" speech is not a Washington speech) and
            # flagged Location Provisional = 1 in the provenance sidecar.
            raw_loc = (rec.get('Location') or '').strip()
            norm_loc, method = normalize_location(raw_loc, gaz)
            provisional = 0
            if not norm_loc or not assert_schema_safe(norm_loc):
                strict_method = method
                norm_loc, method = provisional_location(raw_loc, gaz)
                provisional = 1
                quarantined.append({
                    'url': raw_url,
                    'canonical_url': canonicalize_live_url(raw_url),
                    'slug': url_slug(raw_url),
                    'date': rec.get('Date', ''),
                    'title': rec.get('Title', ''),
                    'speech_type': rec.get('speech_type', ''),
                    'location_raw': raw_loc,
                    'normalize_method': strict_method,
                    'sos_word_count': len((rec.get('speech_text') or '').split()),
                    'natural_word_count': len((rec.get('speaker_text') or '').split()),
                    'location_provisional_value': norm_loc,
                    'location_method': method,
                    'disposition': 'merged_provisional',
                })

            max_rid += 1
            mapped = _map_backfill_row(rec, max_rid)
            mapped['Location'] = norm_loc
            mapped['Canonical URL'] = canonicalize_live_url(raw_url)
            loc_methods[method] += 1
            cleaned = _clean_new_row(mapped)
            new_rows.append(cleaned)
            provenance[id(cleaned)] = {
                'URL': raw_url, 'Date': iso_date, 'Date Method': date_method,
                'Location': norm_loc, 'Location Raw': raw_loc,
                'Location Method': method, 'Location Provisional': str(provisional),
                'Speech Type': cleaned.get('Speech Type', ''),
                'Modality': (rec.get('modality') or '').strip(),
                'Untranscribed Rubio Segments': (rec.get('untranscribed_rubio_segments') or '').strip(),
            }

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
    if date_methods:
        print('  Date source: '
              + ', '.join(f'{k} {v}' for k, v in date_methods.most_common()))

    # ── Per-row verifier gate ────────────────────────────────────────────────
    # Row-local verifier checks run on the new rows alone, after they are
    # fully enriched (the checks cover the enrichment columns too). A row that
    # fails is held and retried next run instead of blocking every other
    # row's release for weeks (09-07 -> 09-28).
    verifier = _HERE / 'corpus_verify_research_grade.py'
    if new_rows:
        _enrich_new_rows(new_rows, args)
        row_failures = gate_new_rows(new_rows, verifier)
        if row_failures:
            for i in sorted(row_failures):
                msgs = row_failures[i]
                held_now.append({
                    'url': new_rows[i].get('URL', ''),
                    'check': '; '.join(dict.fromkeys(_check_name(m) for m in msgs)),
                    'detail': ' | '.join(msgs)[:2000],
                })
            new_rows = [r for i, r in enumerate(new_rows) if i not in row_failures]

    # ── Provisional-Location sidecar ─────────────────────────────────────────
    # Same file name as when these rows were quarantined, so the workflow's
    # artifact upload keeps working. Written AFTER the gate: a provisional row
    # the gate held was not merged, so its disposition says 'held', and the
    # release notes (which count Location Provisional = 1 in the provenance
    # sidecar) never claim it.
    held_urls = {h['url'] for h in held_now}
    for q in quarantined:
        if q['url'] in held_urls:
            q['disposition'] = 'held'
    if quarantined:
        qpath = out_dir / 'rubio_unresolved_location.csv'
        _write_quarantine(qpath, quarantined)
        n_merged_prov = sum(q['disposition'] == 'merged_provisional' for q in quarantined)
        print('')
        print(f'  !! PROVISIONAL Location on {len(quarantined):,} row(s) -- {n_merged_prov:,} merged, '
              f'flagged Location Provisional = 1; {len(quarantined) - n_merged_prov:,} held. Triage: {qpath}')
        for q in quarantined[:10]:
            print(f'       [{q["normalize_method"]} -> {q["location_method"]}] '
                  f'{q["location_raw"]!r} -> {q["location_provisional_value"]!r} '
                  f'({q["disposition"]}) — {q["url"]}')
        if len(quarantined) > 10:
            print(f'       ... and {len(quarantined) - 10} more')
        print('')

    # Written BEFORE the early exit below: if every new row is held we still
    # must leave the retry list behind, or holding would be a silent drop.
    held_rows = _update_held(previous_held, held_now, seen_keys, today)
    _write_csv(held_path, _HELD_COLUMNS, held_rows)
    if held_now:
        print('')
        print(f'  !! HELD {len(held_now):,} new row(s) that fail a row-local check '
              f'({len(held_rows):,} in the retry list). Not merged this run: {held_path}')
        for h in held_now[:10]:
            print(f'       [{h["check"]}] {h["url"]}')
        if len(held_now) > 10:
            print(f'       ... and {len(held_now) - 10} more')
        print('')
        flag_path.write_text(f'{len(held_now)}\n', encoding='utf-8')
    elif flag_path.exists():
        flag_path.unlink()
    gh_out = os.environ.get('GITHUB_OUTPUT')
    if gh_out:
        with open(gh_out, 'a', encoding='utf-8') as f:
            f.write(f'held={len(held_now)}\n')

    if not new_rows:
        print('No new rows — corpus unchanged. Skipping output write and verification.')
        sys.exit(0)

    # ── Merge and renumber ───────────────────────────────────────────────────
    merged = rows + new_rows
    for i, r in enumerate(merged):
        r['Row Id'] = str(i)

    # ── Enrich ────────────────────────────────────────────────────────────────
    # The new rows were enriched before the gate; this pass covers the rare
    # args.corpus that predates enrichment (no rows have a Row Hash), which
    # it enriches once. Existing enriched rows are skipped (--incremental),
    # and Row Hash excludes Row Id, so the renumbering above is harmless.
    enriched_count = sum(enrich_row(r, incremental=True) for r in merged)
    if enriched_count:
        print(f'Enriched {enriched_count:,} existing row(s) Phase 1-2 (incremental).')
    if not args.skip_ner:
        ner_count = enrich_rows_ner(merged, get_nlp(args.spacy_model), incremental=True)
        if ner_count:
            print(f'Enriched {ner_count:,} existing row(s) Phase 3-4 NER (incremental).')

    # ── Provenance sidecar ───────────────────────────────────────────────────
    prov_records = [dict(provenance[id(r)], **{'Row Id': r['Row Id']}) for r in new_rows]
    prov_path = out_dir / 'rubio_merge_provenance.csv'
    _write_csv(prov_path, _PROVENANCE_COLUMNS, prov_records)
    n_prov = sum(p['Location Provisional'] == '1' for p in prov_records)
    print(f'  Wrote provenance sidecar: {prov_path} ({n_prov:,} provisional Location)')

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
    # Full corpus, against the pre-merge baseline. Every new row already
    # passed the row-local checks above, so a FAIL here is corpus-global
    # (Check Q, Row Id sequence, duplicates against existing rows, date
    # gaps) and stays a hard failure.
    print(f'Running verifier: {verifier}')
    result = subprocess.run(
        [sys.executable, str(verifier),
         '--input', args.output,
         '--max-gap-days', str(args.max_gap_days),
         '--baseline', args.corpus,
         '--rubio-coverage', '--titles', '--audit-checks'],
        capture_output=False,
    )
    if result.returncode != 0:
        print('ERROR: verifier FAILED — output not suitable for release.')
        sys.exit(1)

    print(f'\nMerged {len(new_rows):,} new Rubio rows. Total: {len(merged):,} rows. Verifier: PASSED.'
          + (f' Held: {len(held_now):,}.' if held_now else ''))


def _enrich_new_rows(new_rows: list[dict], args) -> None:
    """Phase 1-2 + (unless --skip-ner) Phase 3-4 on the new rows only.

    --incremental semantics: only the freshly scraped rows lack a Row Hash /
    NER columns, so this is the work the merged-corpus pass used to do,
    moved ahead of the per-row gate so the gate sees enriched rows. A weekly
    run only has a handful of new rows, so n_process stays 1 -- not worth the
    multiprocess spin-up (see corpus_enrich_v1.py's --n-process guidance).
    """
    enriched_count = sum(enrich_row(r, incremental=True) for r in new_rows)
    print(f'Enriched {enriched_count:,} row(s) Phase 1-2 (incremental).')
    if args.skip_ner:
        return
    nlp = get_nlp(args.spacy_model)
    audit_log: list = []
    unresolved_log: list = []
    ner_count = enrich_rows_ner(
        new_rows, nlp, incremental=True,
        audit_log=audit_log, unresolved_log=unresolved_log,
    )
    print(f'Enriched {ner_count:,} row(s) Phase 3-4 NER (incremental).')
    out_dir = Path(args.output).parent
    write_people_audit(out_dir / 'rubio_update_people_mention_audit.csv', audit_log)
    write_unresolved_gpe_audit(out_dir / 'rubio_update_unresolved_gpe_audit.csv', unresolved_log)


if __name__ == '__main__':
    main()
