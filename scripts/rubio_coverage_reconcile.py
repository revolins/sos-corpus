#!/usr/bin/env python3
"""rubio_coverage_reconcile.py — Rubio coverage harness (plan step 8, audit §5a).

Two levels, matching the decided coverage target (docs/rubio_coverage_audit.md):

Item level (needs --ground-truth, the ground_truth.csv of rubio_ground_truth.py)
  Every ground-truth row is matched to the corpus on its canonical URL key (query
  and fragment dropped, office-of-the-spokesperson -> office-of-the-spokesman folded
  on www.state.gov only) OR on its (slug, Date) key -- never on the slug alone: the
  annual "<country>-national-day" statements reuse slugs. Status per item:
    present       matched to a corpus row
    excluded      listed in data/rubio_coverage_exclusions.csv (a recorded decision)
    out_of_scope  gt_class not one of the four in-scope classes, in_scope = 0, or
                  an event date before the tenure floor (2025-01-21)
    unknown       in_scope empty (fetch_error / not yet probed) and not in the corpus
    missing       in scope and in none of the above
  A missing item first seen less than --grace-days before the as-of date (the
  corpus max Date), or still in rubio_pending.csv / rubio_held.csv with fewer than
  two attempts, is a warning, not a failure: the weekly job feeds missing items to
  the scraper (--missing-urls-out) and they get one run to arrive. Recall is
  reported per month and per gt_class, and numbered slug families with a hole
  ("remarks-to-press-3", "-5" but no "-4") are flagged for a human look.

Turn level (every Rubio row)
  The Raw Text content block is segmented with rubio_turns (the same reference
  rubio_reextract.py scores against). For a row with >= 1 Rubio label:
    rubio_recall        share of his turns' 6-gram shingles present in SOS Text
    foreign_leak_words  words of SOS Text covered only by other speakers' shingles
  For every row: untranscribed "(In Spanish.)" segments, inline uppercase labels
  anywhere in SOS Text (Unicode-aware, X-04), a mid-word start (SOS Text opens
  lowercase where Raw Text has a letter right before it, X-08), a state.gov
  "Related ... Notifications" nav tail, and a non-Rubio byline (C-02).

Exit 1 when an in-scope item is missing (outside the grace above), a labelled row
has rubio_recall < 0.98, or a row leaks (foreign_leak_words > 0 or an inline label).
An --allowlist row (row_id or url, reason, expires) turns its failure into a warning
until the expiry date. --legacy-warn-only downgrades the turn-level failures so the
harness can report on a corpus released before the extraction fix (v2026.10.19).

Outputs (--out): coverage_items.csv, coverage_turns.csv, coverage_summary.json,
coverage_report.md, and with --missing-urls-out a URL list for rubio_backfill --urls.

Usage:
    python3 scripts/rubio_coverage_reconcile.py --corpus data/corpus.csv \\
        [--ground-truth ground_truth.csv] [--exclusions data/rubio_coverage_exclusions.csv] \\
        [--listing-snapshot data/rubio_listing_snapshot.csv] [--pending ...] [--held ...] \\
        [--allowlist allow.csv] [--legacy-warn-only] --out DIR
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Stdlib-only modules: the verifier imports this file and must stay importable
# without the scraping stack. The scoring helpers (rubio_reextract ->
# rubio_backfill, pandas, ftfy) are imported lazily by _scoring().
from corpus_keys import (  # noqa: E402
    _DELIM, canonical_url_key, canonicalize_live_url, slug_date_key, slug_ym_key, url_slug,
    url_year_month,
)
import rubio_turns  # noqa: E402

csv.field_size_limit(10 ** 8)

_REPO = Path(__file__).resolve().parent.parent
DEFAULT_EXCLUSIONS = _REPO / 'data' / 'rubio_coverage_exclusions.csv'

RECALL_MIN = 0.98
SCOPE_FLOOR = '2025-01-21'
GRACE_DAYS = 7
PENDING_GRACE_ATTEMPTS = 2
AGE_WARN_DAYS = 21

# Mirrors rubio_ground_truth.IN_SCOPE_CLASSES (not imported: that module needs requests).
IN_SCOPE_CLASSES = ('rubio_spoken', 'rubio_written_byline', 'rubio_video_transcript', 'rubio_joint')

ITEM_COLUMNS = [
    'url_key', 'slug_date_key', 'canonical_url', 'gt_class', 'in_scope', 'modality',
    'event_date', 'month', 'first_seen', 'status', 'matched_by', 'corpus_row_id',
    'grace', 'allowlisted', 'note',
]
TURN_COLUMNS = [
    'Row Id', 'URL', 'Date', 'Speech Type', 'scored', 'n_rubio_turns', 'n_other_turns',
    'rubio_ref_words', 'sos_words', 'rubio_recall', 'foreign_leak_words',
    'untranscribed_segments', 'inline_labels', 'mid_word_start', 'nav_tail',
    'byline', 'byline_reason', 'non_rubio_byline', 'unresolved_labels',
    'status', 'reasons',
]


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def item_key(url: str) -> str:
    """Canonical URL key, query/fragment dropped, live path renames applied.

    Same definition as rubio_ground_truth.url_key and rubio_backfill._live_key;
    the rename fold is host-scoped to www.state.gov inside canonicalize_live_url.
    """
    raw = (url or '').strip().split('#', 1)[0].split('?', 1)[0]
    if raw and '://' not in raw:
        raw = 'https://' + raw          # exclusions store keys without a scheme
    return canonical_url_key(canonicalize_live_url(raw))


def norm_slug_date(key: str) -> str:
    """'slug|YYYY-MM-DD' (exclusions CSV) or 'slug\\x1fYYYY-MM-DD' -> corpus_keys form."""
    return (key or '').strip().replace('|', _DELIM)


def _month_of(row: dict) -> str:
    for v in (row.get('event_date'), row.get('Date')):
        if v and re.match(r'\d{4}-\d{2}', v):
            return v[:7]
    sd = norm_slug_date(row.get('slug_date_key', ''))
    if _DELIM in sd:
        return sd.split(_DELIM, 1)[1][:7]
    ym = url_year_month(row.get('canonical_url') or row.get('url') or '')
    return f'{ym[0]:04d}-{ym[1]:02d}' if ym else ''


class CorpusIndex:
    """Rubio rows keyed by item_key (URL and Canonical URL) and by (slug, Date)."""

    def __init__(self, rows: list[dict]):
        self.by_url: dict[str, str] = {}
        self.by_slug_date: dict[str, str] = {}
        self.max_date = ''
        for r in rows:
            rid = r.get('Row Id', '')
            for col in ('URL', 'Canonical URL'):
                k = item_key(r.get(col, ''))
                if k:
                    self.by_url.setdefault(k, rid)
            sd = slug_date_key(r.get('URL', ''), r.get('Date', ''))
            if sd:
                self.by_slug_date.setdefault(sd, rid)
            d = (r.get('Date') or '').strip()
            if d > self.max_date:
                self.max_date = d

    def match(self, url: str, sd_key: str = '') -> tuple[str, str]:
        """(row_id, 'url_key' | 'slug_date') or ('', '')."""
        k = item_key(url)
        if k and k in self.by_url:
            return self.by_url[k], 'url_key'
        sd = norm_slug_date(sd_key)
        if sd and sd in self.by_slug_date:
            return self.by_slug_date[sd], 'slug_date'
        return '', ''


# ---------------------------------------------------------------------------
# Side inputs
# ---------------------------------------------------------------------------

def _read_csv(path) -> list[dict]:
    if not path or not Path(path).exists():
        return []
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def load_exclusions(path) -> tuple[dict[str, str], dict[str, str]]:
    """({item_key: reason}, {slug_date_key: reason}); missing file -> empty."""
    by_url, by_sd = {}, {}
    for r in _read_csv(path):
        reason = (r.get('reason') or '').strip()
        k = item_key(r.get('url_key', ''))
        if k:
            by_url[k] = reason
        sd = norm_slug_date(r.get('slug_date_key', ''))
        if sd:
            by_sd[sd] = reason
    return by_url, by_sd


def load_queue(path, date_col: str) -> dict[str, dict]:
    """rubio_pending.csv / rubio_held.csv -> {item_key: {'since': date, 'attempts': int}}."""
    out = {}
    for r in _read_csv(path):
        k = item_key(r.get('url', ''))
        if not k:
            continue
        try:
            att = int(r.get('attempts') or 0)
        except ValueError:
            att = 0
        out[k] = {'since': (r.get(date_col) or '')[:10], 'attempts': att, 'url': r.get('url', '')}
    return out


class Allowlist:
    """CSV row_id,url,reason,expires (ISO date). An entry downgrades a failure to a
    warning while today <= expires; an empty expires never expires (discouraged)."""

    def __init__(self, path=None, today: date | None = None):
        self.today = today or datetime.now(timezone.utc).date()
        self.by_row: dict[str, dict] = {}
        self.by_url: dict[str, dict] = {}
        self.expired: list[dict] = []
        for r in _read_csv(path):
            exp = (r.get('expires') or '').strip()
            try:
                live = not exp or date.fromisoformat(exp[:10]) >= self.today
            except ValueError:
                live = False
            if not live:
                self.expired.append(r)
                continue
            if (r.get('row_id') or '').strip():
                self.by_row[r['row_id'].strip()] = r
            if (r.get('url') or '').strip():
                self.by_url[item_key(r['url'])] = r

    def get(self, row_id: str = '', url: str = '') -> dict | None:
        if row_id and row_id in self.by_row:
            return self.by_row[row_id]
        k = item_key(url) if url else ''
        return self.by_url.get(k) if k else None


# ---------------------------------------------------------------------------
# Item level
# ---------------------------------------------------------------------------

def reconcile_items(gt_rows: list[dict], index: CorpusIndex, exclusions,
                    pending: dict | None = None, held: dict | None = None,
                    allow: Allowlist | None = None, as_of: str = '',
                    grace_days: int = GRACE_DAYS,
                    scope_floor: str = SCOPE_FLOOR) -> list[dict]:
    """One output row per ground-truth row; see the module docstring for statuses."""
    ex_url, ex_sd = exclusions
    pending, held = pending or {}, held or {}
    allow = allow or Allowlist()
    as_of = as_of or index.max_date
    try:
        grace_from = (date.fromisoformat(as_of[:10]) - timedelta(days=grace_days)).isoformat()
    except ValueError:
        grace_from = ''
    out = []
    for g in gt_rows:
        url = g.get('canonical_url') or g.get('url') or ''
        k = item_key(url) or item_key(g.get('url_key', ''))
        sd = norm_slug_date(g.get('slug_date_key', ''))
        cls = (g.get('gt_class') or '').strip()
        in_scope = str(g.get('in_scope', '')).strip()
        ev = (g.get('event_date') or '').strip()
        row = {
            'url_key': k, 'slug_date_key': sd.replace(_DELIM, '|'), 'canonical_url': url,
            'gt_class': cls, 'in_scope': in_scope, 'modality': g.get('modality', ''),
            'event_date': ev, 'month': _month_of(g), 'first_seen': g.get('first_seen', ''),
            'status': '', 'matched_by': '', 'corpus_row_id': '', 'grace': 0,
            'allowlisted': '', 'note': '',
        }
        rid, how = index.match(url or g.get('url_key', ''), sd)
        row['corpus_row_id'], row['matched_by'] = rid, how
        if k in ex_url or (sd and sd in ex_sd):
            row['status'] = 'excluded'
            row['note'] = ex_url.get(k) or ex_sd.get(sd, '')
        elif in_scope == '0' or (cls and cls not in IN_SCOPE_CLASSES and in_scope != ''):
            row['status'] = 'out_of_scope'
            if rid:
                row['note'] = 'in corpus although out of scope (precision)'
        elif ev and ev < scope_floor:
            row['status'] = 'out_of_scope'
            row['note'] = f'event_date before {scope_floor}'
        elif rid:
            row['status'] = 'present'
        elif in_scope == '':
            row['status'] = 'unknown'
            row['note'] = f'in_scope not decided ({cls or "unclassified"})'
        else:
            row['status'] = 'missing'
            q = pending.get(k) or held.get(k)
            fs = (row['first_seen'] or '')[:10]
            if q and q['attempts'] < PENDING_GRACE_ATTEMPTS:
                row['grace'] = 1
                row['note'] = f'queued (attempts={q["attempts"]})'
            elif fs and grace_from and fs > grace_from:
                row['grace'] = 1
                row['note'] = f'first seen {fs}, within {grace_days}-day grace'
            a = allow.get(url=url)
            if a is not None:
                row['allowlisted'] = f'{a.get("reason", "")} (expires {a.get("expires", "")})'
        out.append(row)
    return out


def item_failures(items: list[dict]) -> list[dict]:
    return [i for i in items if i['status'] == 'missing' and not i['grace'] and not i['allowlisted']]


def recall_table(items: list[dict], key: str) -> list[dict]:
    """Per-`key` item recall over in-scope items (present / (present + missing))."""
    agg: dict[str, Counter] = defaultdict(Counter)
    for i in items:
        agg[i.get(key) or '?'][i['status']] += 1
    out = []
    for k in sorted(agg):
        c = agg[k]
        denom = c['present'] + c['missing']
        out.append({key: k, 'present': c['present'], 'missing': c['missing'],
                    'excluded': c['excluded'], 'out_of_scope': c['out_of_scope'],
                    'unknown': c['unknown'],
                    'recall': round(c['present'] / denom, 4) if denom else None})
    return out


_FAMILY_RE = re.compile(r'^(?P<base>[a-z][a-z0-9-]*?)(?:-(?P<n>\d{1,2}))?$')


def slug_family_gaps(urls, min_max: int = 3) -> list[dict]:
    """Numbered slug families ("remarks-to-press", "-2", "-4") with holes.

    The bare slug counts as number 1. Only families reaching at least `min_max`
    are considered. A hole is NOT necessarily a lost item -- WordPress numbers slugs
    site-wide, so "remarks-to-press-4" may belong to someone else -- it is a review
    flag for the thin-window check (D-09).
    """
    fam: dict[str, set[int]] = defaultdict(set)
    for u in urls:
        s = url_slug(u)
        m = _FAMILY_RE.match(s or '')
        if not m:
            continue
        n = int(m.group('n')) if m.group('n') else 1
        fam[m.group('base')].add(n)
    out = []
    for base, nums in sorted(fam.items()):
        hi = max(nums)
        if hi < min_max or len(nums) < 2:
            continue
        gaps = [n for n in range(1, hi + 1) if n not in nums]
        if gaps:
            out.append({'family': base, 'have': sorted(nums), 'gaps': gaps})
    return out


# ---------------------------------------------------------------------------
# Turn level
# ---------------------------------------------------------------------------

_SCORING = None


def _scoring():
    """Lazy import of the scoring helpers (rubio_reextract -> rubio_backfill, pandas,
    ftfy). Raises ImportError when the scraping stack is unavailable."""
    global _SCORING
    if _SCORING is None:
        import rubio_reextract as rx
        from rubio_backfill import _byline_decision, _parse_block_metadata
        _SCORING = (rx, _parse_block_metadata, _byline_decision)
    return _SCORING


def _tok_is_upper(t: str) -> bool:
    core = t.strip(".,'’-")
    if not core or core != core.upper() or not any(c.isalpha() for c in core):
        return False
    return all(c.isalpha() or c in ".'’-" or unicodedata.category(c).startswith('M') for c in t)


def inline_labels(text: str) -> list[str]:
    """Uppercase speaker labels anywhere in `text`, not only at a line start.

    Walks back from every ':' (not a clock time) over the run of all-uppercase
    tokens on the same line -- Unicode-aware, so "FOREIGN MINISTER RAMÍREZ:" counts
    (X-04). A single word counts only when it is a known role word (QUESTION,
    MODERATOR, ...) or a Rubio label, so "NATO: ..." does not.
    """
    out = []
    text = (text or '').replace('\xa0', ' ')
    for m in re.finditer(r':(?!\d)', text):
        head = text[max(0, m.start() - 90):m.start()].split('\n')[-1]
        if not head or head[-1].isspace():
            continue
        lab: list[str] = []
        for t in reversed(re.split(r'[ \t]+', head)):
            if len(lab) >= 7 or not _tok_is_upper(t):
                break
            lab.insert(0, t)
        if not lab:
            continue
        s = ' '.join(lab)
        if sum(c.isalpha() for c in s) < 2 and s != 'Q':
            continue
        if (len(lab) > 1 or s.strip('.') in rubio_turns._INLINE_SINGLE_WORD
                or rubio_turns.is_rubio_label(s)):
            out.append(s)
    return out


_WS_RE = re.compile(r'\s+')
_NAV_TAIL_RE = re.compile(r'(?s)\bRelated\b.{0,2500}\bNotifications\W*$')


def mid_word_start(sos: str, raw: str) -> bool:
    """SOS Text opens lowercase where Raw Text has a letter right before it (17095
    "hat they believe..." -- the X-08 half-text fallback cut inside a word)."""
    s = _WS_RE.sub(' ', (sos or '')).strip()
    if not s or not s[0].islower():
        return False
    probe = s[:40]
    r = _WS_RE.sub(' ', raw or '')
    p = r.find(probe)
    while p != -1:
        if p > 0 and r[p - 1].isalpha():
            return True
        p = r.find(probe, p + 1)
    return False


def nav_tail(sos: str) -> bool:
    """A state.gov / whitehouse.gov "Related ... Notifications" rail at the end."""
    return bool(_NAV_TAIL_RE.search((sos or '')[-3000:]))


def row_coverage(row: dict) -> dict:
    """Turn-level metrics for one corpus row (dict with Row Id, URL, Title, SOS Text,
    Raw Text, ...). Needs the scoring stack (see _scoring)."""
    rx, parse_meta, byline_decision = _scoring()
    raw = row.get('Raw Text', '') or ''
    sos = row.get('SOS Text', '') or ''
    rubio_ref, foreign_ref, n_rt = rx.reference(raw)
    _, st = rubio_turns.rubio_text(_block_for_stats(raw))
    out = {
        'Row Id': row.get('Row Id', ''), 'URL': row.get('URL', ''),
        'Date': row.get('Date', ''), 'Speech Type': row.get('Speech Type', ''),
        'n_rubio_turns': n_rt, 'n_other_turns': st.get('n_other_turns', 0),
        'untranscribed_segments': st.get('untranscribed_segments', 0),
        'unresolved_labels': '; '.join(st.get('unresolved_labels', [])),
        'sos_words': len(sos.split()),
    }
    if n_rt:
        ref_sh = rx._shingles(rx._tokens(rubio_ref))
        foreign_only = rx._shingles(rx._tokens(foreign_ref)) - ref_sh
        _, rec, leak = rx.score(sos, ref_sh, foreign_only)
        out.update(scored='turns', rubio_ref_words=len(rubio_ref.split()),
                   rubio_recall=round(rec, 4), foreign_leak_words=leak)
    elif rubio_ref.strip():
        # Label-less page (written statement, video message): recall against the
        # whole content block, reported but not gated -- block headers and captions
        # make it noisy.
        ref_sh = rx._shingles(rx._tokens(rubio_ref))
        _, rec, _ = rx.score(sos, ref_sh, set())
        out.update(scored='block', rubio_ref_words=len(rubio_ref.split()),
                   rubio_recall=round(rec, 4), foreign_leak_words=0)
    else:
        out.update(scored='none', rubio_ref_words=0, rubio_recall='', foreign_leak_words=0)
    labs = inline_labels(sos)
    out['inline_labels'] = '; '.join(dict.fromkeys(labs))
    out['mid_word_start'] = int(mid_word_start(sos, raw))
    out['nav_tail'] = int(nav_tail(sos))
    meta = parse_meta(raw) or {}
    keep, reason, _joint = byline_decision(meta, row.get('Title', ''), n_rt)
    out['byline'] = meta.get('block_byline', '') or ''
    out['byline_reason'] = reason
    out['non_rubio_byline'] = int(not keep)
    return out


def _block_for_stats(raw: str) -> str:
    """The errata-applied content block reference() segments (for the label stats)."""
    from rubio_backfill import _END_MARK_RE, _apply_footnote_errata, _rubio_content_block
    block, _ = _rubio_content_block({'raw_text': raw})
    if not block.strip():
        return ''
    block, _ = _apply_footnote_errata(block)
    return _END_MARK_RE.sub('', block).rstrip()


def turn_failures(m: dict) -> list[str]:
    """Gating reasons for one row_coverage() result ([] = passes)."""
    reasons = []
    if m.get('scored') == 'turns' and m.get('rubio_recall') != '' \
            and float(m['rubio_recall']) < RECALL_MIN:
        reasons.append(f'recall {m["rubio_recall"]} < {RECALL_MIN}')
    if int(m.get('foreign_leak_words') or 0) > 0:
        reasons.append(f'foreign_leak_words {m["foreign_leak_words"]}')
    if m.get('inline_labels'):
        reasons.append(f'inline label(s) {m["inline_labels"]}')
    return reasons


def turn_flags(m: dict) -> list[str]:
    """Non-gating review flags (the verifier's AF/AG make them hard)."""
    flags = []
    if m.get('mid_word_start'):
        flags.append('mid_word_start')
    if m.get('nav_tail'):
        flags.append('nav_tail')
    if m.get('non_rubio_byline'):
        flags.append(f'non_rubio_byline ({m.get("byline_reason")}: {m.get("byline")})')
    return flags


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

def load_rubio_rows(path, rows_filter: set[str] | None = None) -> list[dict]:
    keep = ('Row Id', 'Secretary', 'Date', 'Title', 'Speech Type', 'URL',
            'Canonical URL', 'SOS Text', 'Raw Text')
    out = []
    with open(path, newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            if 'Rubio' not in (r.get('Secretary') or ''):
                continue
            if rows_filter and r.get('Row Id') not in rows_filter:
                continue
            out.append({k: r.get(k, '') or '' for k in keep})
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _write(path: Path, cols: list[str], rows: list[dict]) -> None:
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def run(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    today = date.fromisoformat(args.today) if args.today else datetime.now(timezone.utc).date()
    allow = Allowlist(args.allowlist, today=today)
    want = {r.strip() for r in (args.rows or '').split(',') if r.strip()} or None
    rows = load_rubio_rows(args.corpus, want)
    index = CorpusIndex(rows)
    print(f'[rubio_coverage_reconcile] {len(rows):,} Rubio rows from {Path(args.corpus).name} '
          f'(max Date {index.max_date or "?"})', file=sys.stderr)

    # ── Item level ─────────────────────────────────────────────────────────
    # The exclusions file ships with the repo; if it is absent the "decided out of
    # scope" items would all read as missing, so stop instead of reporting that.
    if args.exclusions and not Path(args.exclusions).exists():
        raise SystemExit(f'[rubio_coverage_reconcile] ERROR: exclusions file {args.exclusions} '
                         'not found (it is checked in under data/).')
    exclusions = load_exclusions(args.exclusions)
    pending = load_queue(args.pending, 'first_seen')
    held = load_queue(args.held, 'first_held')
    gt_rows = _read_csv(args.ground_truth)
    items = []
    if args.ground_truth and not gt_rows:
        print(f'[rubio_coverage_reconcile] WARNING ground truth {args.ground_truth} '
              f'missing or empty; item level skipped', file=sys.stderr)
    if gt_rows:
        items = reconcile_items(gt_rows, index, exclusions, pending, held, allow,
                                as_of=args.as_of or index.max_date, grace_days=args.grace_days)
    _write(out / 'coverage_items.csv', ITEM_COLUMNS, items)
    missing_fail = item_failures(items)
    missing_all = [i for i in items if i['status'] == 'missing']
    excluded_in_corpus = sorted(
        r['Row Id'] for r in rows
        if item_key(r['URL']) in exclusions[0]
        or slug_date_key(r['URL'], r['Date']) in exclusions[1])

    snapshot_urls = [r.get('href', '') for r in _read_csv(args.listing_snapshot)]
    families = slug_family_gaps([r['URL'] for r in rows]
                                + [i['canonical_url'] for i in items] + snapshot_urls)

    if args.missing_urls_out:
        miss = [i['canonical_url'] for i in missing_all if i['canonical_url']]
        miss = miss[:args.missing_urls_max]
        Path(args.missing_urls_out).write_text(
            '# in-scope ground-truth items missing from the corpus '
            '(rubio_coverage_reconcile.py)\n' + ''.join(u + '\n' for u in miss),
            encoding='utf-8')
        print(f'[rubio_coverage_reconcile] wrote {len(miss)} missing URL(s) to '
              f'{args.missing_urls_out}', file=sys.stderr)

    # ── Turn level ─────────────────────────────────────────────────────────
    turns, turn_fail, turn_warn = [], [], []
    try:
        _scoring()
        scoring_ok = True
    except Exception as e:  # noqa: BLE001 -- report and carry on with item level
        scoring_ok = False
        print(f'[rubio_coverage_reconcile] WARNING turn level skipped: cannot import '
              f'scoring helpers ({type(e).__name__}: {e})', file=sys.stderr)
    if scoring_ok and not args.items_only:
        for r in rows:
            m = row_coverage(r)
            reasons = turn_failures(m)
            flags = turn_flags(m)
            a = allow.get(m['Row Id'], m['URL'])
            if reasons and a is not None:
                m['status'] = 'allowlisted'
                turn_warn.append(m)
            elif reasons and args.legacy_warn_only:
                m['status'] = 'warn'
                turn_warn.append(m)
            elif reasons:
                m['status'] = 'fail'
                turn_fail.append(m)
            else:
                m['status'] = 'flag' if flags else 'ok'
            m['reasons'] = ' | '.join(reasons + flags)
            turns.append(m)
    _write(out / 'coverage_turns.csv', TURN_COLUMNS, turns)

    # ── Summary ────────────────────────────────────────────────────────────
    scored = [t for t in turns if t['scored'] == 'turns']
    low = [t for t in scored if float(t['rubio_recall']) < RECALL_MIN]
    leak = [t for t in turns if int(t['foreign_leak_words']) > 0]
    inline = [t for t in turns if t['inline_labels']]
    summary = {
        'generated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'corpus': str(args.corpus), 'rubio_rows': len(rows), 'max_date': index.max_date,
        'ground_truth': str(args.ground_truth or ''), 'legacy_warn_only': bool(args.legacy_warn_only),
        'items': {
            'total': len(items),
            'by_status': dict(Counter(i['status'] for i in items)),
            'missing_failing': [i['canonical_url'] for i in missing_fail],
            'missing_in_grace': [i['canonical_url'] for i in missing_all if i['grace']],
            'item_recall': (round(sum(i['status'] == 'present' for i in items)
                                  / max(1, sum(i['status'] in ('present', 'missing') for i in items)), 4)
                            if items else None),
            'by_month': recall_table(items, 'month'),
            'by_gt_class': recall_table(items, 'gt_class'),
        },
        'corpus_rows_on_exclusion_list': excluded_in_corpus,
        'slug_family_gaps': families,
        'turns': {
            'scored_rows': len(scored),
            'block_rows': sum(t['scored'] == 'block' for t in turns),
            'rows_recall_below_min': len(low),
            'rows_recall_below_min_ids': [t['Row Id'] for t in low],
            'mean_recall_scored': (round(sum(float(t['rubio_recall']) for t in scored) / len(scored), 4)
                                   if scored else None),
            'rows_with_leak': len(leak), 'leak_words': sum(int(t['foreign_leak_words']) for t in leak),
            'rows_with_leak_ids': [t['Row Id'] for t in leak],
            'rows_with_inline_labels_ids': [t['Row Id'] for t in inline],
            'untranscribed_segments': sum(int(t['untranscribed_segments']) for t in turns),
            'untranscribed_rows': sum(int(t['untranscribed_segments']) > 0 for t in turns),
            'mid_word_start_ids': [t['Row Id'] for t in turns if t['mid_word_start']],
            'nav_tail_ids': [t['Row Id'] for t in turns if t['nav_tail']],
            'non_rubio_byline_ids': [t['Row Id'] for t in turns if t['non_rubio_byline']],
            'failing_ids': [t['Row Id'] for t in turn_fail],
            'warned_ids': [t['Row Id'] for t in turn_warn],
        } if turns else None,
        'allowlist_expired': [dict(r) for r in allow.expired],
    }
    failed = bool(missing_fail or turn_fail)
    summary['verdict'] = 'FAIL' if failed else 'PASS'
    (out / 'coverage_summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    report = render_report(summary)
    (out / 'coverage_report.md').write_text(report, encoding='utf-8')
    print(report)
    print(f'[rubio_coverage_reconcile] wrote {out}/coverage_items.csv, coverage_turns.csv, '
          f'coverage_summary.json, coverage_report.md', file=sys.stderr)
    return 1 if failed else 0


def render_report(s: dict) -> str:
    it, tu = s['items'], s['turns']
    lines = [f'## Rubio coverage — {s["verdict"]}', '',
             f'Corpus `{Path(s["corpus"]).name}`: {s["rubio_rows"]:,} Rubio rows, max Date {s["max_date"]}.', '']
    if it['total']:
        bs = it['by_status']
        lines += ['### Items (against ground truth)', '',
                  f'- Item recall (in scope): **{it["item_recall"]}**',
                  '- ' + ', '.join(f'{k}: {v}' for k, v in sorted(bs.items())),
                  f'- Missing (failing): {len(it["missing_failing"])}; in grace: {len(it["missing_in_grace"])}']
        for u in it['missing_failing'][:25]:
            lines.append(f'  - {u}')
        lines += ['', '| gt_class | present | missing | recall |', '|---|---|---|---|']
        for r in it['by_gt_class']:
            lines.append(f'| {r["gt_class"]} | {r["present"]} | {r["missing"]} | {r["recall"]} |')
        thin = [r for r in it['by_month'] if r['recall'] is not None and r['recall'] < 1]
        if thin:
            lines += ['', 'Months below full recall: '
                      + ', '.join(f'{r["month"]} ({r["recall"]})' for r in thin)]
        lines.append('')
    else:
        lines += ['Items: no ground truth given — item level not run.', '']
    if s['corpus_rows_on_exclusion_list']:
        lines += [f'Corpus rows that are on the exclusion list (precision): '
                  f'{", ".join(s["corpus_rows_on_exclusion_list"])}', '']
    if tu:
        lines += ['### Turns (inside kept rows)', '',
                  f'- Rows scored on Rubio turns: {tu["scored_rows"]} (mean recall {tu["mean_recall_scored"]}); '
                  f'label-less rows: {tu["block_rows"]}',
                  f'- Rows with recall < {RECALL_MIN}: **{tu["rows_recall_below_min"]}**'
                  + (f' ({", ".join(tu["rows_recall_below_min_ids"][:40])})' if tu['rows_recall_below_min'] else ''),
                  f'- Rows with foreign leak: **{tu["rows_with_leak"]}** ({tu["leak_words"]:,} words)'
                  + (f' ({", ".join(tu["rows_with_leak_ids"][:40])})' if tu['rows_with_leak'] else ''),
                  f'- Inline labels in SOS Text: {", ".join(tu["rows_with_inline_labels_ids"]) or "none"}',
                  f'- Untranscribed Rubio segments: {tu["untranscribed_segments"]} in {tu["untranscribed_rows"]} rows',
                  f'- Mid-word start: {", ".join(tu["mid_word_start_ids"]) or "none"}; '
                  f'nav tail: {", ".join(tu["nav_tail_ids"]) or "none"}; '
                  f'non-Rubio byline: {", ".join(tu["non_rubio_byline_ids"]) or "none"}',
                  f'- Failing rows: {len(tu["failing_ids"])}; warned/allowlisted: {len(tu["warned_ids"])}'
                  + (' (--legacy-warn-only)' if s['legacy_warn_only'] else '')]
    else:
        lines.append('Turns: not run.')
    if s['slug_family_gaps']:
        lines += ['', f'Numbered slug families with holes (review only): {len(s["slug_family_gaps"])} — '
                  + '; '.join(f'{f["family"]} missing {f["gaps"][:6]}' for f in s['slug_family_gaps'][:10])]
    if s['allowlist_expired']:
        lines += ['', f'Expired allowlist entries (no longer downgrade): {len(s["allowlist_expired"])}']
    return '\n'.join(lines) + '\n'


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--corpus', required=True, help='Corpus CSV (with Raw Text)')
    ap.add_argument('--ground-truth', default=None, help='ground_truth.csv from rubio_ground_truth.py')
    ap.add_argument('--exclusions', default=str(DEFAULT_EXCLUSIONS),
                    help='Exclusions CSV (url_key, slug_date_key, reason, decided_by, date)')
    ap.add_argument('--listing-snapshot', default=None,
                    help='rubio_listing_snapshot.csv (adds listed slugs to the family-gap scan)')
    ap.add_argument('--pending', default=None, help='rubio_pending.csv (missing items queued here get grace)')
    ap.add_argument('--held', default=None, help='rubio_held.csv (missing items held here get grace)')
    ap.add_argument('--allowlist', default=None,
                    help='CSV row_id,url,reason,expires: failures become warnings until expires')
    ap.add_argument('--legacy-warn-only', action='store_true',
                    help='Turn-level failures are warnings (for a corpus released before the fix)')
    ap.add_argument('--grace-days', type=int, default=GRACE_DAYS,
                    help='Missing items first seen within this many days of --as-of are warnings')
    ap.add_argument('--as-of', default='', help='Date the grace is measured from (default: corpus max Date)')
    ap.add_argument('--today', default='', help='Date for allowlist expiry (default: today UTC)')
    ap.add_argument('--rows', default='', help='Comma-separated Row Ids to restrict the corpus to')
    ap.add_argument('--items-only', action='store_true', help='Skip the turn level')
    ap.add_argument('--missing-urls-out', default=None,
                    help='Write missing in-scope item URLs here (for rubio_backfill.py --urls)')
    ap.add_argument('--missing-urls-max', type=int, default=50,
                    help='Cap on --missing-urls-out entries (default %(default)s)')
    ap.add_argument('--out', required=True, help='Output directory')
    sys.exit(run(ap.parse_args()))


if __name__ == '__main__':
    main()
