#!/usr/bin/env python3
"""Distribution audit of a corpus release: every row and every column, no sampling.

Text columns (SOS Text, Natural Text), per row:
  tokens, unique  word tokens and distinct words
  ttr_z           distinct words against the corpus-wide length curve
                  (log unique ~ a + b log tokens, Heaps' law), as a z-score:
                  very low = repeated text, very high = word lists / menus
  hapax_share     share of the row's distinct words that no other row uses
                  (garbled or run-together words: "theQUESTION", "governmenttoward")
  upper_share     share of all-capital tokens (speaker labels left in the text)
  not_in_natural  SOS Text words that never occur in Natural Text
  not_in_raw      words that never occur in Raw Text

Across rows:
  boilerplate     8-word sequences that recur in many rows' SOS Text
  near_duplicates pairs of rows sharing most of their SOS Text

Categorical and list columns: value frequencies, with the rare values listed.

Writes <out>/rows.csv (one line per row with every metric and a flags list),
<out>/boilerplate.csv, <out>/near_duplicates.csv, <out>/values_<column>.csv and
<out>/summary.txt.

    python corpus_distribution_audit.py --input corpus.csv --out-dir audit/
"""

from __future__ import annotations

import argparse
import csv
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

csv.field_size_limit(10 ** 9)

TEXT_COLUMNS = ('SOS Text', 'Natural Text')
CATEGORICAL = ('Secretary', 'Speech Type', 'Location')
LIST_COLUMNS = ('Speakers Present', 'People Mentioned (Natural Text)',
                'People Mentioned (SOS Text)', 'Countries Mentioned')
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?")
_UPPER_RE = re.compile(r"^[A-Z][A-Z'’.\-]{2,}$")
_GRAM = 8


try:
    from corpus_finalize_v4_30 import normalize_encoding as _norm
except ImportError:  # pragma: no cover
    def _norm(t: str) -> str:
        return t


def words(text: str) -> list[str]:
    t = (text or '').replace('’', "'").replace('‘', "'")
    return [w.lower() for w in _WORD_RE.findall(t)]


def _fit(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Least squares y = a + b x; returns (a, b, residual sd)."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs) or 1.0
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    sd = math.sqrt(sum((y - a - b * x) ** 2 for x, y in zip(xs, ys)) / max(n - 2, 1)) or 1.0
    return a, b, sd


def audit(rows: list[dict], out: Path, boiler_min_rows: int = 25) -> str:
    out.mkdir(parents=True, exist_ok=True)
    rid = [r['Row Id'] for r in rows]
    toks = {c: [words(r.get(c, '')) for r in rows] for c in TEXT_COLUMNS}
    raw_sets = [set(words(_norm(r.get('Raw Text', '')))) for r in rows]
    df = {c: Counter() for c in TEXT_COLUMNS}
    for c in TEXT_COLUMNS:
        for t in toks[c]:
            df[c].update(set(t))

    metrics = [dict(rid=i, secretary=r.get('Secretary', ''), title=r.get('Title', '')[:90])
               for i, r in zip(rid, rows)]
    for c in TEXT_COLUMNS:
        key = 'sos' if c == 'SOS Text' else 'nat'
        fit_x, fit_y = [], []
        for t in toks[c]:
            if len(t) >= 20:
                fit_x.append(math.log(len(t)))
                fit_y.append(math.log(len(set(t))))
        a, b, sd = _fit(fit_x, fit_y)
        for m, r, t, rs in zip(metrics, rows, toks[c], raw_sets):
            u = set(t)
            m[f'{key}_tokens'], m[f'{key}_unique'] = len(t), len(u)
            m[f'{key}_ttr_z'] = round((math.log(len(u)) - a - b * math.log(len(t))) / sd, 2) \
                if len(t) >= 20 else ''
            hap = sorted(w for w in u if df[c][w] == 1)
            m[f'{key}_hapax_share'] = round(len(hap) / len(u), 3) if u else 0
            m[f'{key}_hapax'] = ' '.join(hap[:12])
            raw_toks = r.get(c, '').split()
            m[f'{key}_upper_share'] = round(
                sum(1 for w in raw_toks if _UPPER_RE.match(w)) / len(raw_toks), 3) if raw_toks else 0
            m[f'{key}_not_in_raw'] = len(u - rs)
            m[f'{key}_not_in_raw_words'] = ' '.join(sorted(u - rs)[:12])
    for m, s, n in zip(metrics, toks['SOS Text'], toks['Natural Text']):
        miss = set(s) - set(n)
        m['sos_not_in_natural'] = len(miss)
        m['sos_not_in_natural_words'] = ' '.join(sorted(miss)[:12])

    # flags: tails of each distribution
    def tail(key, hi=None, lo=None):
        vals = [m[key] for m in metrics if m[key] != '']
        return vals
    for m in metrics:
        f = []
        for key in ('sos', 'nat'):
            z = m[f'{key}_ttr_z']
            if z != '' and z <= -4:
                f.append(f'{key}_repetitive')
            if z != '' and z >= 4:
                f.append(f'{key}_wordlist')
            if m[f'{key}_tokens'] >= 30 and m[f'{key}_hapax_share'] >= 0.08:
                f.append(f'{key}_many_unique_words')
            if m[f'{key}_tokens'] >= 30 and m[f'{key}_upper_share'] >= 0.04:
                f.append(f'{key}_upper_case')
            if m[f'{key}_not_in_raw'] >= 3:
                f.append(f'{key}_words_not_on_page')
        if m['sos_not_in_natural'] >= 3:
            f.append('sos_words_not_in_natural')
        m['flags'] = ' '.join(f)
    cols = list(metrics[0].keys())
    with (out / 'rows.csv').open('w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(metrics)

    # boilerplate: 8-grams recurring across rows of SOS Text
    gram_rows: dict[tuple, set] = defaultdict(set)
    for i, t in enumerate(toks['SOS Text']):
        for g in {tuple(t[k:k + _GRAM]) for k in range(len(t) - _GRAM + 1)}:
            gram_rows[g].add(i)
    boiler = sorted(((len(v), ' '.join(g)) for g, v in gram_rows.items() if len(v) >= boiler_min_rows),
                    reverse=True)
    with (out / 'boilerplate.csv').open('w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(['rows', 'phrase', 'example_row_ids'])
        for n, g in boiler:
            w.writerow([n, g, ' '.join(rid[i] for i in sorted(gram_rows[tuple(g.split())])[:8])])

    # near duplicates: rows sharing >= 50% of the smaller row's rare 8-grams
    shared = defaultdict(int)
    size = [0] * len(rows)
    for g, v in gram_rows.items():
        if 2 <= len(v) <= 3:
            for i in v:
                size[i] += 1
            v = sorted(v)
            for x in range(len(v)):
                for y in range(x + 1, len(v)):
                    shared[(v[x], v[y])] += 1
    pairs = []
    for (x, y), n in shared.items():
        nx = max(len(toks['SOS Text'][x]) - _GRAM + 1, 1)
        ny = max(len(toks['SOS Text'][y]) - _GRAM + 1, 1)
        frac = n / min(nx, ny)
        if frac >= 0.5 and min(nx, ny) >= 10:
            pairs.append((round(frac, 3), rid[x], rid[y]))
    pairs.sort(reverse=True)
    with (out / 'near_duplicates.csv').open('w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(['shared_share', 'row_a', 'row_b', 'title_a', 'title_b', 'date_a', 'date_b'])
        for frac, a_, b_ in pairs:
            ra, rb = rows[rid.index(a_)], rows[rid.index(b_)]
            w.writerow([frac, a_, b_, ra.get('Title', '')[:80], rb.get('Title', '')[:80],
                        ra.get('Date', ''), rb.get('Date', '')])

    # categorical and list columns
    lines = []
    for c in CATEGORICAL + LIST_COLUMNS:
        cnt = Counter()
        where = defaultdict(list)
        for r in rows:
            vals = [v for v in (r.get(c, '') or '').split('; ') if v] if c in LIST_COLUMNS \
                else [r.get(c, '')]
            for v in vals:
                cnt[v] += 1
                if len(where[v]) < 5:
                    where[v].append(r['Row Id'])
        safe = re.sub(r'\W+', '_', c).strip('_').lower()
        with (out / f'values_{safe}.csv').open('w', newline='', encoding='utf-8') as fh:
            w = csv.writer(fh)
            w.writerow(['value', 'rows', 'example_row_ids'])
            for v, n in cnt.most_common():
                w.writerow([v, n, ' '.join(where[v])])
        lines.append(f'{c}: {len(cnt):,} distinct values, {sum(1 for n in cnt.values() if n == 1):,} '
                     f'used by one row')

    flag_counts = Counter(f for m in metrics for f in m['flags'].split())
    lines.append('row flags: ' + ', '.join(f'{k} {v}' for k, v in flag_counts.most_common()))
    lines.append(f'boilerplate 8-grams in >= {boiler_min_rows} rows: {len(boiler):,}')
    lines.append(f'near-duplicate row pairs (>= 50% shared rare 8-grams): {len(pairs):,}')
    summary = '\n'.join(lines)
    (out / 'summary.txt').write_text(summary + '\n', encoding='utf-8')
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--input', required=True, type=Path)
    ap.add_argument('--out-dir', required=True, type=Path)
    ap.add_argument('--boilerplate-min-rows', type=int, default=25)
    args = ap.parse_args()
    with args.input.open(newline='', encoding='utf-8') as fh:
        rows = list(csv.DictReader(fh))
    print(audit(rows, args.out_dir, args.boilerplate_min_rows))


if __name__ == '__main__':
    main()
