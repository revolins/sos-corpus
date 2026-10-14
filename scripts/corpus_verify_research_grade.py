#!/usr/bin/env python3
"""Strict research-grade verifier for v4.14+ SoS speech corpus CSVs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

# Check W needs the canonical country-name set to check "Countries
# Mentioned" entries are drawn from a known vocabulary. That table is built
# dynamically from pycountry at import time in corpus_enrich_v1.py (~250
# entries) -- importing it here (unlike the small, static Row Hash formula
# duplicated for Check S) avoids maintaining a second drifting copy of a
# large generated table. Falls back to skipping that one sub-check (format
# checks still run) if the import fails for any reason (e.g. verify is run
# from an environment without pycountry/corpus_enrich_v1 on the path).
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from corpus_enrich_v1 import _ISO3_BY_CANONICAL_NAME  # type: ignore[import]
except BaseException:
    # BaseException, not Exception: corpus_enrich_v1.py itself calls
    # sys.exit() (raising SystemExit, a BaseException NOT an Exception) if
    # IT can't import corpus_finalize_v4_30 (e.g. ftfy missing in this
    # environment) -- an `except Exception` here would miss that and crash
    # the verifier entirely over an optional cross-check. This import must
    # never be allowed to take verify.py down with it.
    _ISO3_BY_CANONICAL_NAME = None

csv.field_size_limit(10 ** 8)

EXPECTED_COLUMNS = [
    'Row Id', 'Secretary', 'Date', 'Title', 'Speech Type',
    'Location', 'URL', 'SOS Text', 'Natural Text', 'Raw Text',
]

# Columns corpus_enrich_v1.py (and later versions) may add. The schema check
# below accepts a CSV with none, some, or all of these present -- a
# finalize-only CSV and an enriched CSV both pass -- but any column outside
# EXPECTED_COLUMNS + KNOWN_ENRICH_COLUMNS is flagged as unexpected/misspelled.
KNOWN_ENRICH_COLUMNS = [
    'SOS Word Count', 'Natural Word Count', 'Row Hash', 'Speakers Present',
    'People Mentioned (Natural Text)', 'People Mentioned (SOS Text)',
    'Countries Mentioned', 'Country ISO Codes Mentioned',
    # Optional/additive on purpose: it must NOT go in EXPECTED_COLUMNS, or a
    # finalize-stage CSV would fail the schema check AND the "Empty {col} = 0"
    # loop (which iterates EXPECTED_COLUMNS) would hard-fail on the very first
    # run, before anything has populated the column.
    'Canonical URL',
]

# Known no-ISO-code entries -- Check W does not treat these as a count
# mismatch. See corpus_enrich_v1.py's _TIER_C_COUNTRIES comment: Kosovo has
# no official ISO 3166-1 alpha-3 code.
_NO_ISO_CODE_COUNTRIES = {'Kosovo', 'Soviet Union', 'Yugoslavia', 'Czechoslovakia'}

# Mirrors corpus_enrich_v1.py's _ANONYMOUS_SENTINELS (duplicated, not
# imported, matching Check S's Row Hash formula precedent -- this check
# must not depend on importing the enrichment script for a static list).
_SENTINEL_WORDS = {
    'QUESTION', 'Q', 'MODERATOR', 'HOST', 'REPORTER', 'NARRATOR',
    'PARTICIPANT', 'STAFF', 'AUDIENCE', 'INTERPRETER',
}

# Row Hash formula, duplicated from corpus_enrich_v1.py's row_hash() by
# design -- the verifier must not depend on importing the enrichment script.
# Keep the two in sync if the hash formula ever changes.
_ROW_HASH_FIELDS = [
    'Secretary', 'Date', 'Title', 'Speech Type', 'Location',
    'URL', 'SOS Text', 'Natural Text', 'Raw Text',
]
_ROW_HASH_DELIM = '\x1f'
_ROW_HASH_RE = re.compile(r'[0-9a-f]{64}')

# Check Z's content hash: the Row Hash fields MINUS the URL. Row Hash itself
# includes URL and must keep including it (it is published, and changing the
# formula would invalidate every released hash and manifest fingerprint), but
# that is exactly why Check S could not see the September 2026 incident: when
# state.gov renamed a path segment, 609 already-collected speeches came back
# with new URLs, new Row Hashes, and therefore no collision to report.
# Recomputing a URL-free hash here costs one sha256 pass and needs no schema
# change. Verified 0 collisions across all 17,617 rows of v2026.09.03.
_CONTENT_HASH_FIELDS = [f for f in _ROW_HASH_FIELDS if f != 'URL']


def _row_hash(row: dict[str, str]) -> str:
    payload = _ROW_HASH_DELIM.join(row.get(f, '') or '' for f in _ROW_HASH_FIELDS)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _content_hash(row: dict[str, str]) -> str:
    """Row Hash without the URL -- see _CONTENT_HASH_FIELDS."""
    payload = _ROW_HASH_DELIM.join(row.get(f, '') or '' for f in _CONTENT_HASH_FIELDS)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def url_slug(url: str) -> str:
    """Last path segment of a canonicalized URL; '' for a bare host.

    Mirrors corpus_keys.url_slug, duplicated here for the same reason
    _row_hash is: the verifier must stay importable on its own.
    """
    key = canonical_url(url)
    if not key:
        return ''
    key = key.split('#', 1)[0].split('?', 1)[0].rstrip('/')
    segments = [seg for seg in key.split('/') if seg]
    return segments[-1] if len(segments) > 1 else ''


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()

SECRETARIES = {
    'Christopher', 'Albright', 'Powell', 'Rice', 'Clinton', 'Kerry',
    'Tillerson', 'Pompeo', 'Blinken', 'Rubio',
}

SPEECH_TYPES = {
    'Remarks', 'Speech', 'Interview', 'Testimony', 'Press Statement',
    'Statement', 'Press Availability', 'Joint Press Availability',
    'Press Briefing', 'Op-Ed', 'Town Hall', 'Remarks to the Press',
}

BAD_TEXT_PATTERNS = {
    'skip/share nav': re.compile(r'Skip\s+to\s+(?:Main\s+)?Content|\bShare\s+(?:Press|Remarks|Interview|Speech|Statement|Address|Testimony|Townhall|Op-Ed|Joint)', re.I),
    'archive release header': re.compile(r'As\s+released\s+by\s+the\s+Office\s+of\s+the\s+Spokesman', re.I),
    'archive notice/header': re.compile(r'You\s+are\s+viewing\s+ARCHIVED\s+CONTENT|2017-2021\s+ARCHIVED\s+CONTENT|Other\s+State\s+Department\s+Archive\s+Sites', re.I),
    'footer tags/backtop': re.compile(r'\nTags\n|\bBack\s+to\s+Top\b|\bTags\s+(?:Secretary|Office\s+of|Anti-Semitism|Iran|Israel|North\s+Korea)\b', re.I),
}

SPEAKER_LABEL_LINE = re.compile(r'(?m)^\s*[A-Z][A-Z .\'\-]{2,70}:\s*')
URL_YM_RE = re.compile(r'/((?:19|20)\d{2})/(\d{2})/')
MONTH_DATE_RE = re.compile(
    r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+((?:19|20)\d{2})\b',
    re.I,
)
MONTH_NUM = {m.lower(): i for i, m in enumerate(
    ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'], 1
)}


def canonical_url(url: str) -> str:
    u = (url or '').strip().lower()
    u = re.sub(r'^https?://(?:www\.)?', '', u)
    return u.rstrip('/')


def page_has_url_month_date(row: dict[str, str]) -> bool:
    m = URL_YM_RE.search(row.get('URL', '') or '')
    if not m:
        return False
    y, mo = int(m.group(1)), int(m.group(2))
    if row.get('Date', '').startswith(f'{y:04d}-{mo:02d}'):
        return False
    raw = row.get('Raw Text', '') or ''
    for dm in MONTH_DATE_RE.finditer(raw[:12000]):
        if int(dm.group(2)) == y and MONTH_NUM[dm.group(1).lower()] == mo:
            return True
    return False


def main() -> None:
    default_input = Path(__file__).resolve().parent / 'secretaryofstate_speeches_final.fused.v44.patched.enriched.postprocessed.v4.14.csv'
    ap = argparse.ArgumentParser(description='Research-grade corpus verifier')
    ap.add_argument('--input', default=str(default_input))
    ap.add_argument('--max-gap-days', type=int, default=45)
    ap.add_argument('--baseline', default=None,
                     help='Path to the pre-enrichment (finalize-stage) CSV. If given, '
                          'Check Q diffs the 10 base columns row-for-row against it to '
                          'guard against the enrichment script mutating already-QA\'d '
                          'text. Skipped (not failed) when omitted.')
    ap.add_argument('--manifest-path', default=None,
                     help='Path to the enrichment manifest sidecar for Check S. '
                          'Defaults to <input>.manifest.json.')
    ap.add_argument('--allow-changed-rows', default=None,
                     help='Audit CSV from a deliberate text patch (columns Row Id, field). '
                          'Check Q then accepts a baseline difference in exactly those '
                          '(Row Id, field) pairs -- and only in SOS Text / Natural Text -- '
                          'and still fails on any other difference.')
    args = ap.parse_args()

    with open(args.input, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        rows = list(reader)

    failures: list[str] = []

    def ok(msg: str) -> None:
        print(f'  ok    {msg}')

    def fail(msg: str) -> None:
        failures.append(msg)
        print(f'  FAIL  {msg}')

    print(f'Verifying {args.input} …\n')

    ok(f'Row count = {len(rows):,}')
    _known_cols = set(EXPECTED_COLUMNS) | set(KNOWN_ENRICH_COLUMNS)
    _missing_cols = [c for c in EXPECTED_COLUMNS if c not in cols]
    _unexpected_cols = [c for c in cols if c not in _known_cols]
    if not _missing_cols and not _unexpected_cols:
        ok(f'Column names match research schema ({len(cols)} columns)')
    else:
        if _missing_cols:
            fail(f'Missing expected column(s): {_missing_cols}')
        if _unexpected_cols:
            fail(f'Unexpected/unknown column(s): {_unexpected_cols}')

    ids = [r.get('Row Id', '') for r in rows]
    if ids == [str(i) for i in range(len(rows))]:
        ok(f'Row Id sequential 0…{len(rows)-1}')
    else:
        fail('Row Id sequence mismatch')

    for col in EXPECTED_COLUMNS:
        bad = [r.get('Row Id', '?') for r in rows if not (r.get(col, '') or '').strip()]
        if bad:
            fail(f'Empty {col}: {len(bad)} rows, sample {bad[:8]}')
        else:
            ok(f'Empty {col} = 0')

    sec_bad = [(r['Row Id'], r['Secretary']) for r in rows if r.get('Secretary') not in SECRETARIES]
    if sec_bad:
        fail(f'Non-canonical Secretary values: {sec_bad[:10]}')
    else:
        ok('Secretary values canonical')

    type_bad = [(r['Row Id'], r['Speech Type']) for r in rows if r.get('Speech Type') not in SPEECH_TYPES]
    if type_bad:
        fail(f'Non-controlled Speech Type values: {len(type_bad)} sample {type_bad[:10]}')
    else:
        ok('Speech Type controlled taxonomy')

    dups = [u for u, c in Counter(canonical_url(r.get('URL', '')) for r in rows).items() if u and c > 1]
    if dups:
        fail(f'Duplicate canonical URLs: {dups[:8]}')
    else:
        ok('Duplicate canonical URLs = 0')

    date_bad = [r['Row Id'] for r in rows if page_has_url_month_date(r)]
    if date_bad:
        fail(f'Rows still have recoverable URL/page date mismatch: {date_bad[:10]}')
    else:
        ok('No recoverable URL/page date mismatches')

    for field in ['SOS Text', 'Natural Text']:
        for name, pat in BAD_TEXT_PATTERNS.items():
            hits = [r['Row Id'] for r in rows if pat.search(r.get(field, '') or '')]
            if hits:
                fail(f'{field} contains {name}: {len(hits)} sample {hits[:10]}')
            else:
                ok(f'{field}: {name} = 0')

    speech_label_hits = [r['Row Id'] for r in rows if SPEAKER_LABEL_LINE.search(r.get('SOS Text', '') or '')]
    if speech_label_hits:
        fail(f'SOS Text contains speaker-label lines: {len(speech_label_hits)} sample {speech_label_hits[:10]}')
    else:
        ok('SOS Text speaker-label lines = 0')

    # Check A — SOS Text with repeated identical lines AND very few unique words
    # Catches metadata-header-only rows (e.g. "Secretary Powell\nSecretary Powell\nDetroit\nDetroit")
    # while ignoring legitimate speeches with repeated short phrases like "(Laughter.)"
    def _has_repeated_header_lines(text: str) -> bool:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) < 2:
            return False
        unique_words = len(set(text.lower().split()))
        return unique_words < 20 and len(lines) != len(set(lines))

    repeated_header = [r['Row Id'] for r in rows if _has_repeated_header_lines(r.get('SOS Text', '') or '')]
    if repeated_header:
        fail(f'SOS Text has repeated header lines: {len(repeated_header)} sample {repeated_header[:5]}')
    else:
        ok('SOS Text repeated header lines = 0')

    # Check B — "About state.gov" footer in SOS or Natural Text (not caught by footer tags/backtop regex)
    _ABOUT_STATE_RE = re.compile(r'about\s+state\.gov\s*\|', re.IGNORECASE)
    for col in ('SOS Text', 'Natural Text'):
        about_hits = [r['Row Id'] for r in rows if _ABOUT_STATE_RE.search(r.get(col, '') or '')]
        if about_hits:
            fail(f'{col} contains "About state.gov" footer: {len(about_hits)} sample {about_hits[:5]}')
        else:
            ok(f'{col} "About state.gov" footer = 0')

    # Check C — legacy "Updates | Frequent Questions | Contact Us" nav fragment
    _UPDATES_NAV_VERIF = re.compile(
        r'Updates\s*\|\s*Frequent\s+Questions\s*\|\s*Contact\s+Us',
        re.IGNORECASE,
    )
    for field in ['SOS Text', 'Natural Text']:
        hits = [r['Row Id'] for r in rows if _UPDATES_NAV_VERIF.search(r.get(field, '') or '')]
        if hits:
            fail(f'{field} contains legacy nav fragment: {len(hits)} sample {hits[:5]}')
        else:
            ok(f'{field}: legacy nav fragment = 0')

    # Check D — inline "Released on [Month DD, YYYY]" press-release date stamp
    _RELEASED_ON_VERIF = re.compile(
        r'Released\s+on\s+(?:January|February|March|April|May|June|July|'
        r'August|September|October|November|December)\s+\d{1,2},\s+\d{4}',
        re.IGNORECASE,
    )
    for field in ['SOS Text', 'Natural Text']:
        hits = [r['Row Id'] for r in rows if _RELEASED_ON_VERIF.search(r.get(field, '') or '')]
        if hits:
            fail(f'{field} contains "Released on" date tag: {len(hits)} sample {hits[:5]}')
        else:
            ok(f'{field}: "Released on" date tag = 0')

    # Check E — boundary doc reference codes YYYY/NNN in first/last 200 chars
    _DOC_CODE_VERIF = re.compile(r'\b\d{4}/\d{3,4}\b')
    for field in ['SOS Text', 'Natural Text']:
        hits = []
        for r in rows:
            text = r.get(field, '') or ''
            m = _DOC_CODE_VERIF.search(text[:200])
            if m:
                hits.append(r['Row Id'])
                continue
            if len(text) > 200:
                m2 = _DOC_CODE_VERIF.search(text[-200:])
                if m2:
                    hits.append(r['Row Id'])
        if hits:
            fail(f'{field} has doc reference code at text boundary: {len(hits)} sample {hits[:5]}')
        else:
            ok(f'{field}: doc reference codes at boundary = 0')

    # Check F — audience reaction annotations in SOS Text
    _AUDIENCE_VERIF = re.compile(
        r'\(\s*(?:laughter|applause|cheers?|cheering|clapping)\s*\.?\s*\)',
        re.IGNORECASE,
    )
    hits = [r['Row Id'] for r in rows if _AUDIENCE_VERIF.search(r.get('SOS Text', '') or '')]
    if hits:
        fail(f'SOS Text contains audience reaction annotations: {len(hits)} sample {hits[:5]}')
    else:
        ok('SOS Text: audience reaction annotations = 0')

    # Check G — (inaudible) markers in SOS Text
    _INAUDIBLE_VERIF = re.compile(r'\(\s*inaudible[^)]{0,30}\)', re.IGNORECASE)
    hits = [r['Row Id'] for r in rows if _INAUDIBLE_VERIF.search(r.get('SOS Text', '') or '')]
    if hits:
        fail(f'SOS Text contains (inaudible) markers: {len(hits)} sample {hits[:5]}')
    else:
        ok('SOS Text: (inaudible) markers = 0')

    # Check H — mojibake artifacts in SOS Text
    # Check I — mojibake artifacts in Natural Text
    _MOJIBAKE_VERIF = re.compile(r'Ã[^\s]')
    for field in ['SOS Text', 'Natural Text']:
        hits = [r['Row Id'] for r in rows if _MOJIBAKE_VERIF.search(r.get(field, '') or '')]
        if hits:
            fail(f'{field} contains mojibake artifacts: {len(hits)} sample {hits[:5]}')
        else:
            ok(f'{field}: mojibake artifacts = 0')

    # Check J — SOS Text < 20 words (soft audit, warn only)
    _wc = lambda t: len(t.split()) if t else 0
    short_sos = [(r['Row Id'], _wc(r.get('SOS Text', ''))) for r in rows
                 if _wc(r.get('SOS Text', '')) < 20]
    if short_sos:
        print(f'  warn  SOS Text < 20 words: {len(short_sos)} rows — {short_sos[:10]}')
    else:
        ok('SOS Text: no rows < 20 words')

    # Check K removed 2026-09. It declared Location == 'Op-Ed' legal when the
    # Speech Type matched, but 'Op-Ed' contains no comma, so Check L below
    # rejected the very rows Check K permitted -- the two could never both
    # pass. README.md documented 'Op-Ed'/'Recorded'/'On Travel' as legal
    # Location values on the same mistaken basis; it has been corrected. The
    # corpus long ago converged on strict 'City, Region' (0 of 17,617 rows use
    # a non-geographic Location), and corpus_location.normalize_location now
    # depends on that invariant holding.

    # Check L — every Location must contain a comma (strict City, Country format)
    no_comma = [r['Row Id'] for r in rows if ',' not in (r.get('Location', '') or '')]
    if no_comma:
        fail(f'Location missing comma (not City, Country format): {len(no_comma)} rows, sample {no_comma[:10]}')
    else:
        ok('Location: all values contain comma (City, Country format)')

    # Check M — Raw Text byline attributes speech to a non-Secretary official
    _WRONG_PERSON_RE = re.compile(
        r'Share\s+(?:Remarks|Statement|Interview|Press\s+Briefing)\s+'
        r'[A-Za-z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Za-z][a-z]+'
        r'\s+(?:Assistant|Deputy|Under|Acting)\s+Secretary',
        re.IGNORECASE,
    )
    _SEC_NAMES_VERIF = {
        'warren', 'christopher', 'madeleine', 'albright', 'colin', 'powell',
        'condoleezza', 'rice', 'hillary', 'rodham', 'clinton', 'john', 'kerry',
        'rex', 'tillerson', 'mike', 'michael', 'pompeo', 'antony', 'tony', 'blinken',
        'marco', 'rubio',
    }
    wrong_person_rows = []
    for r in rows:
        raw_head = (r.get('Raw Text', '') or '')[:600]
        m = _WRONG_PERSON_RE.search(raw_head)
        if m:
            name_words = set(m.group(0).lower().split()[2:5])
            if not (name_words & _SEC_NAMES_VERIF):
                wrong_person_rows.append(r['Row Id'])
    if wrong_person_rows:
        fail(f'Raw Text byline attributes speech to non-Secretary official: {wrong_person_rows}')
    else:
        ok('Raw Text byline: no non-Secretary officials detected')

    # Check N — DRC must include "the" (Democratic Republic of the Congo)
    _DRC_ABBREV_RE = re.compile(r'Democratic\s+Republic\s+of\s+Congo(?!\s*,\s*the|\s+the\b)', re.I)
    drc_hits = [r['Row Id'] for r in rows if _DRC_ABBREV_RE.search(r.get('Location', '') or '')]
    if drc_hits:
        fail(f'Location uses "Democratic Republic of Congo" (missing "the"): {len(drc_hits)} rows, sample {drc_hits[:5]}')
    else:
        ok('Location: DRC named correctly with "the"')

    # Check O — no "En Route" in Location
    _EN_ROUTE_RE = re.compile(r'\bEn Route\b', re.I)
    en_route_locs = [r['Row Id'] for r in rows if _EN_ROUTE_RE.search(r.get('Location', '') or '')]
    if en_route_locs:
        fail(f'Location contains "En Route": {len(en_route_locs)} rows, sample {en_route_locs[:5]}')
    else:
        ok('Location: no "En Route" values')

    # Check P — no three-part locations (2+ commas); DRC has exactly 1 comma so no carve-out needed
    multi_comma_locs = [r['Row Id'] for r in rows
                        if (r.get('Location', '') or '').count(',') >= 2]
    if multi_comma_locs:
        fail(f'Location has 2+ commas (three-part format): {len(multi_comma_locs)} rows, sample {multi_comma_locs[:5]}')
    else:
        ok('Location: no three-part (venue, city, country) format')

    # Check Q — regression guard: if --baseline given, the 10 base columns
    # must be byte-identical, per Row Id, to any row that exists in BOTH
    # baseline and current. Catches an enrichment-script bug that silently
    # mutates already-QA'd text -- the single biggest regression risk since
    # enrichment is meant to be strictly additive. A row dropped between
    # baseline and current is also a hard failure (data loss). A row present
    # in current but absent from baseline is NOT a failure -- this is the
    # normal case when --baseline is the pre-merge corpus and current is a
    # post-merge+enrich output with freshly added rows (e.g. the weekly
    # Rubio update), which have nothing in baseline to diff against.
    # Skipped (not failed) entirely when --baseline is not provided.
    if args.baseline:
        with open(args.baseline, newline='', encoding='utf-8') as f:
            baseline_rows = {r.get('Row Id'): r for r in csv.DictReader(f)}
        current_ids = {r.get('Row Id') for r in rows}
        allowed: set[tuple[str, str]] = set()
        if args.allow_changed_rows:
            with open(args.allow_changed_rows, newline='', encoding='utf-8') as f:
                for a in csv.DictReader(f):
                    if a.get('field') in ('SOS Text', 'Natural Text'):
                        allowed.add((str(a['Row Id']), a['field']))
        q_mismatches = []
        q_allowed = set()
        for r in rows:
            rid = r.get('Row Id')
            base_r = baseline_rows.get(rid)
            if base_r is None:
                continue
            for col in EXPECTED_COLUMNS:
                if r.get(col, '') != base_r.get(col, ''):
                    if (rid, col) in allowed:
                        q_allowed.add((rid, col))
                        continue
                    q_mismatches.append((rid, col))
                    break
        if allowed:
            unused = allowed - q_allowed
            ok(f'Check Q: {len(q_allowed)} audited patch change(s) accepted via --allow-changed-rows'
               + (f'; {len(unused)} listed change(s) not present' if unused else ''))
        q_dropped = [rid for rid in baseline_rows if rid not in current_ids]
        q_added = len(current_ids) - (len(baseline_rows) - len(q_dropped))
        if q_mismatches or q_dropped:
            if q_mismatches:
                fail(f'Check Q: base columns diverge from --baseline: {len(q_mismatches)} rows, sample {q_mismatches[:5]}')
            if q_dropped:
                fail(f'Check Q: rows in --baseline missing from current output: {len(q_dropped)} sample {q_dropped[:5]}')
        else:
            shared = len(baseline_rows)
            ok(f'Check Q: base columns byte-identical to --baseline for {shared} shared rows'
               + (f'; {q_added} new row(s) not in baseline' if q_added else ''))
    else:
        print('  skip  Check Q: no --baseline given, regression guard not run')

    # Check R — SOS Word Count / Natural Word Count present, non-negative
    # integers, and recomputation-consistent for every row (deterministic,
    # so no sampling). Skipped when the columns are absent (not yet enriched).
    if 'SOS Word Count' in cols and 'Natural Word Count' in cols:
        wc_bad = []
        for r in rows:
            for text_col, wc_col in (('SOS Text', 'SOS Word Count'), ('Natural Text', 'Natural Word Count')):
                raw = r.get(wc_col, '')
                try:
                    n = int(raw)
                except (TypeError, ValueError):
                    wc_bad.append((r['Row Id'], wc_col, raw))
                    continue
                if n < 0 or n != len((r.get(text_col, '') or '').split()):
                    wc_bad.append((r['Row Id'], wc_col, raw))
        if wc_bad:
            fail(f'Check R: word-count mismatch/malformed: {len(wc_bad)} rows, sample {wc_bad[:5]}')
        else:
            ok(f'Check R: SOS/Natural Word Count recomputation-consistent ({len(rows)} rows)')
        # Soft check only -- COPY_SOS_TO_NAT-style patch rows can legitimately
        # make the two equal, so an inversion isn't automatically a bug.
        inverted = [r['Row Id'] for r in rows
                    if int(r.get('Natural Word Count', 0) or 0) < int(r.get('SOS Word Count', 0) or 0)]
        if inverted:
            print(f'  warn  Check R: Natural Word Count < SOS Word Count: {len(inverted)} rows — {inverted[:10]}')
    else:
        print('  skip  Check R: word-count columns not present (not enriched)')

    # Check S — Row Hash recomputation-consistent, plus manifest sidecar
    # cross-check. Skipped when Row Hash is absent (not yet enriched).
    if 'Row Hash' in cols:
        hash_bad = []
        for r in rows:
            h = r.get('Row Hash', '') or ''
            if not _ROW_HASH_RE.fullmatch(h):
                hash_bad.append((r['Row Id'], 'malformed', h))
            elif h != _row_hash(r):
                hash_bad.append((r['Row Id'], 'mismatch', h))
        if hash_bad:
            fail(f'Check S: Row Hash malformed/mismatch: {len(hash_bad)} rows, sample {hash_bad[:5]}')
        else:
            ok(f'Check S: Row Hash recomputation-consistent ({len(rows)} rows)')

        # Row Hash UNIQUENESS -- distinct from recomputation-consistency
        # above. Row Hash covers the 9 content fields (Secretary...Raw
        # Text), excluding Row Id/URL, so a collision between two different
        # Row Ids means byte-identical content under a (possibly)
        # different URL -- a duplicate the existing canonical-URL dedup
        # check wouldn't catch. Added as a mechanical corpus-integrity
        # check (found 0 collisions on the full corpus at time of writing).
        hash_counts = Counter(r.get('Row Hash', '') for r in rows)
        hash_dupes = {h: c for h, c in hash_counts.items() if c > 1}
        if hash_dupes:
            sample_rids = [r['Row Id'] for r in rows if r.get('Row Hash', '') in hash_dupes][:10]
            fail(f'Check S: Row Hash collisions (byte-identical content across different Row Ids): {len(hash_dupes)} distinct hash(es), sample rows {sample_rids}')
        else:
            ok('Check S: Row Hash values are all unique (no duplicate-content rows)')

        manifest_path = Path(args.manifest_path) if args.manifest_path else Path(args.input + '.manifest.json')
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError) as e:
                fail(f'Check S: manifest at {manifest_path} unreadable: {e}')
            else:
                manifest_bad = []
                if manifest.get('row_count') != len(rows):
                    manifest_bad.append(f"row_count {manifest.get('row_count')} != {len(rows)}")
                if manifest.get('csv_sha256') != _sha256_file(Path(args.input)):
                    manifest_bad.append('csv_sha256 does not match input file')
                if manifest_bad:
                    fail(f'Check S: manifest mismatch: {manifest_bad}')
                else:
                    ok(f'Check S: manifest at {manifest_path} matches input')
        else:
            print(f'  warn  Check S: no manifest found at {manifest_path} (expected sidecar of an enriched CSV)')
    else:
        print('  skip  Check S: Row Hash column not present (not enriched)')

    # Check Z — duplicate speeches that survive a URL rename.
    #
    # This is the check that was missing on 2026-09-21. state.gov renamed
    # /releases/office-of-the-spokesperson/ to /releases/office-of-the-spokesman/
    # between two weekly runs. The scraper's URL-key dedup went blind, the
    # merge queued 609 already-present speeches for append, and NOTHING
    # flagged it: the duplicate-canonical-URL check above compares URLs (which
    # differed), and Check S's Row Hash includes URL (so the hashes differed
    # too). The release was stopped only by an unrelated Location failure.
    #
    # Two keys, because neither alone is sufficient:
    #   Z1 (slug, Date)  — the reliable one. A rename changes the path but not
    #                      the trailing slug or the speech date.
    #   Z2 content hash  — catches a re-scrape under a genuinely new slug, but
    #                      MISSES one whose Raw Text drifted between fetches
    #                      (a related-links rail, a tags block, a typo fix).
    # Both measured at 0 collisions over all 17,617 rows of v2026.09.03, so
    # both are hard failures with no allowlist and nothing grandfathered in.
    z_slug_date: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        slug = url_slug(r.get('URL', ''))
        day = (r.get('Date', '') or '').strip()
        if slug and day:
            z_slug_date[f'{slug}{_ROW_HASH_DELIM}{day}'].append(r['Row Id'])
    z1_dupes = {k: v for k, v in z_slug_date.items() if len(v) > 1}
    if z1_dupes:
        sample = [(k.split(_ROW_HASH_DELIM)[0], v) for k, v in list(z1_dupes.items())[:5]]
        fail(f'Check Z: duplicate (URL slug, Date): {len(z1_dupes)} group(s) — '
             f'a state.gov path rename may have defeated URL dedup; sample {sample}')
    else:
        ok(f'Check Z: (URL slug, Date) unique across {len(rows)} rows')

    z_content: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        z_content[_content_hash(r)].append(r['Row Id'])
    z2_dupes = {k: v for k, v in z_content.items() if len(v) > 1}
    if z2_dupes:
        fail(f'Check Z: byte-identical content under different URLs: '
             f'{len(z2_dupes)} group(s), sample rows {[v for v in list(z2_dupes.values())[:5]]}')
    else:
        ok('Check Z: no duplicate content across differing URLs')

    # Check Z3 — Canonical URL, when the column is present.
    #
    # 'URL' is frozen at the originally-scraped value (it is hashed into Row
    # Hash); 'Canonical URL' tracks where the page lives now. Its uniqueness
    # is where a FUTURE, unmapped state.gov rename becomes visible: two rows,
    # one on the old path and one on the new, would collapse onto the same
    # live location.
    if 'Canonical URL' in cols:
        cu_missing = [r['Row Id'] for r in rows
                      if not (r.get('Canonical URL') or '').strip().lower().startswith('http')]
        if cu_missing:
            # Warn, not fail: this column is additive, and turning a schema
            # addition into a release outage on its first run is not worth it.
            # Promote to fail() once a release has shipped with it populated.
            print(f'  warn  Check Z: Canonical URL empty/malformed on {len(cu_missing)} row(s) '
                  f'— sample {cu_missing[:5]}')
        else:
            ok(f'Check Z: Canonical URL present and well-formed ({len(rows)} rows)')

        cu_dupes = [u for u, c in Counter(
            canonical_url(r.get('Canonical URL', '')) for r in rows).items() if u and c > 1]
        if cu_dupes:
            fail(f'Check Z: duplicate Canonical URLs: {len(cu_dupes)}, sample {cu_dupes[:5]} '
                 f'— two stored URLs resolve to the same live page')
        else:
            ok('Check Z: Canonical URL values are unique')
    else:
        print('  skip  Check Z: Canonical URL column not present')

    # Check T — Speakers Present: non-null; the row's own Secretary must
    # appear (hard -- corpus_enrich_v1.py's speakers_present() guarantees
    # this by construction, so a violation here is a real bug, not noise);
    # no duplicate tokens (case-insensitive); soft warn on unusually high
    # speaker counts (regex-runaway signal). Skipped when absent.
    if 'Speakers Present' in cols:
        empty_sp = [r['Row Id'] for r in rows if not (r.get('Speakers Present') or '').strip()]
        own_sec_missing = []
        dup_tokens = []
        high_count = []
        unbracketed_sentinel = []
        for r in rows:
            sp = (r.get('Speakers Present') or '').strip()
            if not sp:
                continue
            tokens = [t.strip() for t in sp.split(';') if t.strip()]
            if r.get('Secretary') not in tokens:
                own_sec_missing.append(r['Row Id'])
            upper_tokens = [t.upper() for t in tokens]
            if len(upper_tokens) != len(set(upper_tokens)):
                dup_tokens.append(r['Row Id'])
            if len(tokens) > 50:
                high_count.append((r['Row Id'], len(tokens)))
            for t in tokens:
                # Anonymous-role sentinels (QUESTION, MODERATOR, ...) are
                # bracket-wrapped by corpus_enrich_v1.py's
                # normalize_speaker_label() specifically so they read as
                # structural placeholders, not proper-noun names, sitting
                # in the same list as "Blinken"/"Kerry". A bare (unbracketed)
                # sentinel word would mean that guarantee broke.
                if t.upper() in _SENTINEL_WORDS and not (t.startswith('[') and t.endswith(']')):
                    unbracketed_sentinel.append((r['Row Id'], t))
        if empty_sp:
            fail(f'Check T: Speakers Present empty: {len(empty_sp)} rows, sample {empty_sp[:5]}')
        else:
            ok(f'Check T: Speakers Present non-empty ({len(rows)} rows)')
        if own_sec_missing:
            fail(f"Check T: row's own Secretary missing from Speakers Present: {len(own_sec_missing)} rows, sample {own_sec_missing[:5]}")
        else:
            ok("Check T: row's own Secretary present in Speakers Present for all rows")
        if dup_tokens:
            fail(f'Check T: duplicate speaker tokens: {len(dup_tokens)} rows, sample {dup_tokens[:5]}')
        else:
            ok('Check T: no duplicate speaker tokens')
        if unbracketed_sentinel:
            fail(f'Check T: anonymous-role sentinel not bracket-wrapped: {len(unbracketed_sentinel)}, sample {unbracketed_sentinel[:5]}')
        else:
            ok('Check T: anonymous-role sentinels are all bracket-wrapped')
        if high_count:
            print(f'  warn  Check T: >50 speakers (possible regex runaway): {len(high_count)} rows — {high_count[:10]}')
    else:
        print('  skip  Check T: Speakers Present column not present (not enriched)')

    # Check U — Speakers Present distributional sanity (soft warns only; not
    # correctness bugs, just things worth a human glance). Thresholds set
    # from an actual full-corpus distributional pass, not guessed:
    #   - token length <=2 chars (excl. the 'Q' sentinel): real short
    #     surnames exist (Lu, Le, Wu -- Chinese/Vietnamese officials, kept),
    #     but a single letter like "D" is more often a redaction/anonymization
    #     or extraction artifact -- worth a look, not worth hard-failing.
    #   - token length >45 chars: the corpus's longest genuine single-entity
    #     title is "Costa Rican Second Vice President Rebeca Grynspan" (~50
    #     chars); most >45-char tokens found in practice were same-line
    #     regex-runaway prose that slipped past the 7-word cap in
    #     corpus_enrich_v1.py's _plausible_label filter (e.g. "Secretary
    #     Rumsfeld Would Tell You. Mr. Donaldson").
    #   - >15 speakers in one row: distribution has mean=1.8, p99=7, and a
    #     natural drop-off after ~10-12; rows above 15 are rare (14/17595)
    #     but were all confirmed legitimate large multi-participant events
    #     (UN Security Council sessions, cabinet meetings) on manual review
    #     -- kept as an ongoing radar for future corpus updates, not a
    #     current failure.
    if 'Speakers Present' in cols:
        short_tokens = []
        long_tokens = []
        elbow_high = []
        for r in rows:
            tokens = [t.strip() for t in (r.get('Speakers Present') or '').split(';') if t.strip()]
            if len(tokens) > 15:
                elbow_high.append((r['Row Id'], len(tokens)))
            for t in tokens:
                if len(t) <= 2 and t != 'Q':
                    short_tokens.append((r['Row Id'], t))
                if len(t) > 45:
                    long_tokens.append((r['Row Id'], t))
        if short_tokens:
            print(f'  warn  Check U: Speakers Present token <=2 chars (possible redaction/artifact): {len(short_tokens)} — {short_tokens[:10]}')
        else:
            ok('Check U: no <=2-char Speakers Present tokens')
        if long_tokens:
            print(f'  warn  Check U: Speakers Present token >45 chars (possible residual same-line garbage label): {len(long_tokens)} — {[t for _, t in long_tokens[:5]]}')
        else:
            ok('Check U: no >45-char Speakers Present tokens')
        if elbow_high:
            print(f'  warn  Check U: >15 speakers in one row: {len(elbow_high)} rows — {elbow_high[:10]}')
        else:
            ok('Check U: no rows with >15 speakers')
    else:
        print('  skip  Check U: Speakers Present column not present (not enriched)')

    # Check V — People Mentioned (Natural Text)/(SOS Text): both columns
    # are legitimately empty for many rows (a speech may name no third
    # parties), so there's no non-empty check here (unlike Check T's
    # Speakers Present, which is always non-empty by construction). Hard
    # checks: no duplicate tokens per column per row (case-insensitive); no
    # token containing ':' (a leaked speaker-label fragment would be a real
    # bug, not noise); token length <=60 chars (corpus_enrich_v1.py's
    # people_mentioned() enforces this cap at write time via
    # _MAX_PERSON_NAME_CHARS, so a violation here means the two have drifted
    # out of sync); SOS Text mentions must be a subset of Natural Text
    # mentions for the same row (structural guarantee by construction --
    # people_mentioned() only ever adds a name to the SOS list when it's
    # already been added to the Natural list in the same pass, so a
    # violation is a real bug). Soft warn: tokens <=2 chars (short real
    # names exist -- "Xi", "Li" -- but worth a glance, mirrors Check U's
    # approach for Speakers Present).
    if 'People Mentioned (Natural Text)' in cols and 'People Mentioned (SOS Text)' in cols:
        dup_rows = []
        colon_rows = []
        long_rows = []
        short_tokens = []
        not_subset_rows = []
        own_sec_present = []
        for r in rows:
            nat_tokens = [t.strip() for t in (r.get('People Mentioned (Natural Text)') or '').split(';') if t.strip()]
            sos_tokens = [t.strip() for t in (r.get('People Mentioned (SOS Text)') or '').split(';') if t.strip()]
            for col_name, tokens in (('Natural Text', nat_tokens), ('SOS Text', sos_tokens)):
                upper = [t.upper() for t in tokens]
                if len(upper) != len(set(upper)):
                    dup_rows.append((r['Row Id'], col_name))
                for t in tokens:
                    if ':' in t:
                        colon_rows.append((r['Row Id'], col_name, t))
                    if len(t) > 60:
                        long_rows.append((r['Row Id'], col_name, t))
                    if len(t) <= 2:
                        short_tokens.append((r['Row Id'], col_name, t))
            nat_upper = {t.upper() for t in nat_tokens}
            if not all(t.upper() in nat_upper for t in sos_tokens):
                not_subset_rows.append(r['Row Id'])
            sec = (r.get('Secretary') or '').upper()
            if sec and any(sec in t.upper() for t in nat_tokens):
                own_sec_present.append(r['Row Id'])
        if dup_rows:
            fail(f'Check V: duplicate People Mentioned tokens: {len(dup_rows)}, sample {dup_rows[:5]}')
        else:
            ok('Check V: no duplicate People Mentioned tokens')
        if colon_rows:
            fail(f"Check V: People Mentioned token contains ':' (label leakage): {len(colon_rows)}, sample {colon_rows[:5]}")
        else:
            ok("Check V: no People Mentioned tokens containing ':'")
        if long_rows:
            fail(f'Check V: People Mentioned token >60 chars: {len(long_rows)}, sample {[t for *_, t in long_rows[:5]]}')
        else:
            ok('Check V: no People Mentioned tokens >60 chars')
        if not_subset_rows:
            fail(f'Check V: People Mentioned (SOS Text) not a subset of (Natural Text): {len(not_subset_rows)}, sample {not_subset_rows[:5]}')
        else:
            ok('Check V: People Mentioned (SOS Text) is always a subset of (Natural Text)')
        if short_tokens:
            print(f'  warn  Check V: People Mentioned token <=2 chars: {len(short_tokens)} — {short_tokens[:10]}')
        if own_sec_present:
            # Base rate from a full-corpus run: 1,215/17,595 rows (6.9%) --
            # not overwhelming noise (contrast a "row's own Location
            # country absent from Countries Mentioned" check, which fired
            # on 80% of rows and was rejected as useless signal). A
            # legitimate case (another speaker referring to the Secretary
            # in the third person, e.g. "Secretary Blinken noted...") is
            # expected and correctly kept -- this is visibility, not
            # evidence of a bug, hence soft warn not hard fail.
            print(f'  warn  Check V: row\'s own Secretary present in People Mentioned (Natural Text): {len(own_sec_present)} rows — {own_sec_present[:10]}')
    else:
        print('  skip  Check V: People Mentioned columns not present (not enriched)')

    # Check W — Countries Mentioned / Country ISO Codes Mentioned: entries
    # drawn only from the known-canonical set (else = alias-table gap --
    # only checked if _ISO3_BY_CANONICAL_NAME imported successfully, see
    # module-level guarded import above); ISO entries exactly 3 uppercase
    # letters OR the literal 'N/A' placeholder; per-row entry count ALWAYS
    # equal between the two columns (corpus_enrich_v1.py's
    # countries_mentioned() writes 'N/A' at a no-ISO-code entry's position
    # rather than dropping it -- fixed after an output review found the
    # original drop-the-blank design put 1,521/17,595 rows (8.6%) out of
    # positional alignment between the two columns); and 'N/A' appears at
    # position i if and only if names[i] is a documented no-code entry
    # (_NO_ISO_CODE_COUNTRIES), checked positionally, not just as a set.
    if 'Countries Mentioned' in cols and 'Country ISO Codes Mentioned' in cols:
        unknown_names = []
        bad_iso = []
        count_mismatch = []
        position_mismatch = []
        for r in rows:
            names = [t.strip() for t in (r.get('Countries Mentioned') or '').split(';') if t.strip()]
            codes = [t.strip() for t in (r.get('Country ISO Codes Mentioned') or '').split(';') if t.strip()]
            if _ISO3_BY_CANONICAL_NAME is not None:
                for n in names:
                    if n not in _ISO3_BY_CANONICAL_NAME:
                        unknown_names.append((r['Row Id'], n))
            for c in codes:
                if c != 'N/A' and not re.fullmatch(r'[A-Z]{3}', c):
                    bad_iso.append((r['Row Id'], c))
            if len(names) != len(codes):
                count_mismatch.append((r['Row Id'], len(names), len(codes)))
                continue
            for i, (n, c) in enumerate(zip(names, codes)):
                if (c == 'N/A') != (n in _NO_ISO_CODE_COUNTRIES):
                    position_mismatch.append((r['Row Id'], i, n, c))
        if _ISO3_BY_CANONICAL_NAME is not None:
            if unknown_names:
                fail(f'Check W: Countries Mentioned entries outside known vocabulary (alias-table gap): {len(unknown_names)}, sample {unknown_names[:5]}')
            else:
                ok('Check W: all Countries Mentioned entries in known vocabulary')
        else:
            print('  skip  Check W: known-vocabulary sub-check (could not import _ISO3_BY_CANONICAL_NAME)')
        if bad_iso:
            fail(f'Check W: malformed ISO code (not 3 uppercase letters or N/A): {len(bad_iso)}, sample {bad_iso[:5]}')
        else:
            ok('Check W: all ISO codes well-formed')
        if count_mismatch:
            fail(f'Check W: Countries/ISO code count mismatch (positional alignment broken): {len(count_mismatch)}, sample {count_mismatch[:5]}')
        else:
            ok('Check W: Countries/ISO code counts always match (positionally aligned)')
        if position_mismatch:
            fail(f"Check W: 'N/A' placeholder doesn't align with a documented no-code country: {len(position_mismatch)}, sample {position_mismatch[:5]}")
        else:
            ok("Check W: 'N/A' placeholders correctly positioned at no-code entries only")
    else:
        print('  skip  Check W: Countries Mentioned columns not present (not enriched)')

    # Check X — regression guards for the NER-derived columns, mirroring
    # existing checks already applied to SOS/Natural Text: mojibake
    # artifacts (Checks H/I's pattern) and digit-containing tokens.
    # Currently 0 violations on the full corpus for all of these (verified
    # 2026-08-24) -- added as permanent guards against a future regression
    # (e.g. an encoding bug reintroduced upstream, or a new NOT_A_NAME_RE
    # exclusion in corpus_enrich_v1.py accidentally loosened), not because
    # a live problem was found. Countries Mentioned/ISO codes are excluded
    # from the digit check since 'N/A'-style codes and country names never
    # legitimately contain digits either, but that's already covered by
    # Check W's stricter format checks -- no need to duplicate here.
    _derived_text_cols = [c for c in (
        'Speakers Present', 'People Mentioned (Natural Text)',
        'People Mentioned (SOS Text)', 'Countries Mentioned',
    ) if c in cols]
    if _derived_text_cols:
        mojibake_hits = []
        for col in _derived_text_cols:
            for r in rows:
                if _MOJIBAKE_VERIF.search(r.get(col, '') or ''):
                    mojibake_hits.append((r['Row Id'], col))
        if mojibake_hits:
            fail(f'Check X: mojibake artifacts in derived columns: {len(mojibake_hits)}, sample {mojibake_hits[:5]}')
        else:
            ok('Check X: no mojibake artifacts in derived columns')
    else:
        print('  skip  Check X: mojibake sub-check (no derived text columns present)')

    _digit_check_cols = [c for c in ('People Mentioned (Natural Text)', 'People Mentioned (SOS Text)') if c in cols]
    if _digit_check_cols:
        digit_hits = []
        for col in _digit_check_cols:
            for r in rows:
                for t in (r.get(col) or '').split(';'):
                    t = t.strip()
                    if t and any(ch.isdigit() for ch in t):
                        digit_hits.append((r['Row Id'], col, t))
        if digit_hits:
            fail(f'Check X: digit-containing People Mentioned token (non-name, e.g. "F-16s"/"Resolution 1441"): {len(digit_hits)}, sample {digit_hits[:5]}')
        else:
            ok('Check X: no digit-containing People Mentioned tokens')
    else:
        print('  skip  Check X: digit-token sub-check (no People Mentioned columns present)')

    # Check Y — distributional near-duplicate scan over unique component
    # values (soft warn only; a design-judgment flag, not a correctness
    # violation). Method: collapse every distinct token in a column,
    # corpus-wide, to a normalized key (lowercase, non-alphanumeric chars
    # stripped) and group by that key. A group with >1 distinct raw
    # spelling where EACH spelling recurs at least twice (not a one-off
    # typo/coincidence) is flagged for human review. This is exactly the
    # analysis method that found two real bugs during development, both
    # already fixed in corpus_enrich_v1.py before this check was written:
    # (1) all-caps source passages producing "BIDEN" as a token distinct
    # from the same person's normal-case "Biden" elsewhere in the corpus
    # (65 candidate groups found, dominated by this pattern -- fixed via
    # _clean_person_name() title-casing genuinely-all-caps spans), and
    # (2) a possessive-stripping regex missing bare trailing apostrophes
    # ("Ross'"/"Abbas'" not merging with "Ross"/"Abbas" -- fixed). This
    # check exists to catch a FUTURE regression of the same shape, not
    # because a live problem remains (0 candidate groups expected on a
    # freshly-generated corpus at time of writing -- see corpus_enrich_v1.py
    # for the fix). Deliberately does NOT try to auto-resolve genuine
    # transliteration/spelling variance (e.g. "Kim Jong-un"/"Kim Jong Un",
    # "Bashar al-Assad"/"Bashar Al-Assad") -- those are legitimate source
    # variance across different speeches/eras, not a casing bug, and
    # forcing a single "correct" romanization would be presumptuous; they
    # still surface here for visibility, just aren't treated as failures.
    _NORM_KEY_RE = re.compile(r'[^a-z0-9]')

    def _near_dup_groups(col: str) -> list[tuple[str, list[tuple[str, int]]]]:
        counts: Counter = Counter()
        for r in rows:
            for t in (r.get(col) or '').split(';'):
                t = t.strip()
                if t:
                    counts[t] += 1
        groups: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for tok, cnt in counts.items():
            key = _NORM_KEY_RE.sub('', tok.lower())
            if key:
                groups[key].append((tok, cnt))
        return [
            (k, v) for k, v in groups.items()
            if len(v) > 1 and min(c for _, c in v) >= 2
        ]

    for col in ('Speakers Present', 'People Mentioned (Natural Text)', 'Countries Mentioned'):
        if col not in cols:
            print(f'  skip  Check Y: {col} not present (not enriched)')
            continue
        candidates = _near_dup_groups(col)
        if candidates:
            candidates.sort(key=lambda kv: -sum(c for _, c in kv[1]))
            sample = [v for _, v in candidates[:8]]
            print(f'  warn  Check Y: {col} — {len(candidates)} possible near-duplicate/casing-drift group(s) — {sample}')
        else:
            ok(f'Check Y: {col} — no near-duplicate/casing-drift groups found')

    # Check AA — state.gov page chrome that the v2026.10.02 patch strips: the
    # Clinton/Kerry business-sidebar nav block ("Go Back Highlights Learn More
    # Business ...") anywhere in SOS/Natural Text, and a translation-link header
    # ("... Translation [PDF]", "Translations: Arabic (top PDF)") at the start.
    aa_nav = re.compile(r'(?i)\bGo\s+Back\s+Highlights\s+Learn\s+More\s+Business\b')
    aa_tr = re.compile(r'^\s*(?:PDF\s+version\s+)?(?:As\s+Prepared\s+)?(?:[A-Z][a-z]+\s+)?Translations?\s*'
                       r'(?:\[PDF\]|:\s*[A-Z][a-z]+\s*\((?:top|bottom)\s+PDF\))')
    aa_bad = [(r.get('Row Id'), col) for r in rows for col in ('SOS Text', 'Natural Text')
              if aa_nav.search(r.get(col, '') or '') or aa_tr.search(r.get(col, '') or '')]
    if aa_bad:
        fail(f'Check AA: state.gov nav block / translation-link header in text: {len(aa_bad)} field(s), sample {aa_bad[:5]}')
    else:
        ok('Check AA: no state.gov business-sidebar nav or translation-link headers in SOS/Natural Text')

    # Check AB — the "ã——" paragraph-separator mojibake (v2026.10.03 patch).  Check H's
    # Ã-pattern does not see it.  Raw Text keeps it: Raw Text is the archival copy.
    ab_bad = [(r.get('Row Id'), col) for r in rows for col in ('SOS Text', 'Natural Text')
              if 'ã——' in (r.get(col, '') or '')]
    if ab_bad:
        fail(f'Check AB: "ã——" separator mojibake in text: {len(ab_bad)} field(s), sample {ab_bad[:5]}')
    else:
        ok('Check AB: no "ã——" separator mojibake in SOS/Natural Text')

    by_sec: dict[str, list[date]] = defaultdict(list)
    for r in rows:
        try:
            by_sec[r['Secretary']].append(date.fromisoformat(r['Date']))
        except Exception:
            fail(f'Bad date format row {r.get("Row Id")}: {r.get("Date")}')
    large_gaps = []
    for sec, dates in sorted(by_sec.items()):
        unique = sorted(set(dates))
        for a, b in zip(unique, unique[1:]):
            gap = (b - a).days
            if gap > args.max_gap_days:
                large_gaps.append((sec, gap, a.isoformat(), b.isoformat()))
    if large_gaps:
        fail(f'Date coverage gaps > {args.max_gap_days} days: {large_gaps[:10]}')
    else:
        ok(f'No per-secretary date gaps > {args.max_gap_days} days')

    print()
    if failures:
        print(f'FAILED — {len(failures)} research-grade check(s) failed')
        sys.exit(1)
    print('PASSED — research-grade checks passed')
    sys.exit(0)


if __name__ == '__main__':
    main()
