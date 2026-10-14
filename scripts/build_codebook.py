#!/usr/bin/env python3
"""Build the researcher codebook PDF from a corpus CSV.

The wording lives in docs/codebook/codebook_template.tex and is never edited by
this script; only the <<KEY>> markers are filled, from statistics computed on
the CSV being released. So every release's codebook has the same format and
current numbers.

    python scripts/build_codebook.py --input data/corpus_updated.csv --version v2026.10.13 \
        --out data/codebook_v2026.10.13.pdf

--exclude-secretaries (default: Christopher) leaves those rows out of every
statistic and list in the codebook. It does not touch the CSV.

Stdlib only, so it runs in the weekly pipeline's environment. Compiles with
tectonic if it is on PATH, otherwise pdflatex (two passes, for longtable).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter
from pathlib import Path

csv.field_size_limit(10 ** 8)

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / 'docs' / 'codebook' / 'codebook_template.tex'
MARKER = re.compile(r'<<([A-Z0-9_]+)>>')

# Stable example: URL is fixed once released (Row Id is not). Falls back to the
# first multi-speaker press availability if this row ever leaves the corpus.
EXAMPLE_URL = 'https://2009-2017.state.gov/secretary/remarks/2014/11/233869.htm'
NO_ISO = 'N/A'


def tex(s: str) -> str:
    """Escape for LaTeX body text; fold characters outside Latin-1 to ASCII."""
    s = ''.join(c if ord(c) < 256 else unicodedata.normalize('NFKD', c).encode('ascii', 'ignore').decode()
                for c in s)
    rep = {'\\': r'\textbackslash{}', '&': r'\&', '%': r'\%', '$': r'\$', '#': r'\#', '_': r'\_',
           '{': r'\{', '}': r'\}', '~': r'\textasciitilde{}', '^': r'\textasciicircum{}'}
    return ''.join(rep.get(c, c) for c in s)


def items(cell: str) -> list[str]:
    return [t.strip() for t in (cell or '').split(';') if t.strip()]


def n(x: int) -> str:
    return f'{x:,}'


def f1(x: float) -> str:
    return f'{x:,.1f}'


def median(xs: list[int]) -> str:
    m = statistics.median(xs)
    return n(int(m)) if m == int(m) else f1(m)


def shorten(text: str, words: int) -> str:
    w = ' '.join(text.split()).split(' ')
    return tex(' '.join(w[:words])) + (r' \ldots' if len(w) > words else '')


def short_list(cell: str, k: int = 5) -> str:
    t = items(cell)
    return tex('; '.join(t[:k])) + (r'; \ldots' if len(t) > k else '')


def version_of(path: Path, given: str | None) -> str:
    if given:
        return given if given.startswith('v') else f'v{given}'
    m = re.search(r'v(\d{4}\.\d{2}\.\d{2})', path.name)
    if not m:
        sys.exit('ERROR: cannot infer the release version from the file name; pass --version')
    return f'v{m.group(1)}'


def stats(path: Path, version: str, exclude: set[str]) -> dict[str, str]:
    with open(path, newline='', encoding='utf-8') as fh:
        reader = csv.DictReader(fh)
        cols = reader.fieldnames or []
        rows = [r for r in reader if r['Secretary'] not in exclude]
    if not rows:
        sys.exit('ERROR: no rows left after --exclude-secretaries')
    N = len(rows)
    pct = lambda k: str(round(100 * k / N))

    # Coverage, in order of first appearance
    first, last, count = {}, {}, Counter()
    for r in rows:
        s, d = r['Secretary'], r['Date']
        count[s] += 1
        first[s] = min(first.get(s, d), d)
        last[s] = max(last.get(s, d), d)
    secs = sorted(count, key=lambda s: (first[s], last[s]))
    dmin, dmax = min(first.values()), max(last.values())
    cov = [f'{tex(s)} & {n(count[s])} & {first[s]} & {last[s]} \\\\' for s in secs]
    cov += [r'\midrule', f'Total & {n(N)} & {dmin} & {dmax} \\\\']
    overlaps = [(a, b) for a, b in zip(secs, secs[1:]) if first[b] < last[a]]
    if not overlaps:
        sys.exit('ERROR: no tenure overlap found; the Section 2 note in the template needs rewording')
    a, b = overlaps[-1]
    overlap = f"{tex(b)} {first[b]}, {tex(a)}'s last row {last[a]}"

    # Speech types: three column pairs, filled down then across, largest first
    types = Counter(r['Speech Type'] for r in rows).most_common()
    h = -(-len(types) // 3)
    cells = [f'{tex(t)} & {n(c)}' for t, c in types] + [' & '] * (3 * h - len(types))
    type_rows = [' & '.join(cells[j * h + i] for j in range(3)) + r' \\' for i in range(h)]

    sos_wc = [int(r['SOS Word Count']) for r in rows]
    nat_wc = [int(r['Natural Word Count']) for r in rows]
    speakers = [items(r['Speakers Present']) for r in rows]
    sentinels = Counter(t for sp in speakers for t in sp if t.startswith('[') and t.endswith(']'))
    no_iso = Counter(c for r in rows
                     for c, k in zip(items(r['Countries Mentioned']), items(r['Country ISO Codes Mentioned']))
                     if k == NO_ISO)
    raw_max = max(len(r['Raw Text']) for r in rows)
    mag = 10 ** max(len(str(raw_max)) - 2, 0)

    # Example row
    ex = next((r for r in rows if r['URL'] == EXAMPLE_URL), None) or next(
        r for r in rows if r['Speech Type'] == 'Press Availability' and len(items(r['Speakers Present'])) >= 3)
    example = [
        (r'\col{Secretary} / \col{Date}', f"{tex(ex['Secretary'])} / {ex['Date']}"),
        (r'\col{Title} / \col{Speech Type}', f"{tex(ex['Title'])} / {tex(ex['Speech Type'])}"),
        (r'\col{Location}', tex(ex['Location'])),
        (r'\col{URL}', r'\texttt{' + tex(ex['URL']) + '}'),
        (r'\col{SOS Text}', shorten(ex['SOS Text'], 12)),
        (r'\col{Natural Text}', shorten(ex['Natural Text'], 9)),
        ('Word counts (SOS / Natural)', f"{n(int(ex['SOS Word Count']))} / {n(int(ex['Natural Word Count']))}"),
        (r'\col{Speakers Present}', short_list(ex['Speakers Present'])),
        (r'\col{People Mentioned (SOS Text)}', short_list(ex['People Mentioned (SOS Text)'])),
        (r'\col{Countries Mentioned}', short_list(ex['Countries Mentioned'])),
        (r'\col{Country ISO Codes Mentioned}', short_list(ex['Country ISO Codes Mentioned'])),
    ]

    h256 = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h256.update(chunk)
    sha = h256.hexdigest()

    sos_eq = sum(r['SOS Text'] == r['Natural Text'] for r in rows)
    return {
        'VERSION': tex(version), 'N_ROWS': n(N), 'N_COLS': str(len(cols)), 'DATE_MIN': dmin, 'DATE_MAX': dmax,
        'SEC_FIRST': tex(secs[0]), 'SEC_LAST': tex(secs[-1]), 'SEC_LIST': ', '.join(map(tex, secs)),
        'RAW_MAX_CHARS': n(raw_max // mag * mag),
        'COVERAGE_ROWS': '\n'.join(cov), 'OVERLAP_EXAMPLE': overlap,
        'N_TYPES': str(len(types)), 'SPEECH_TYPE_ROWS': '\n'.join(type_rows),
        'WASH_PCT': pct(sum(r['Location'] == 'Washington, D.C.' for r in rows)),
        'SOS_EQ_NAT': n(sos_eq), 'SOS_EQ_NAT_PCT': pct(sos_eq),
        'SOS_WC_MEAN': f1(statistics.fmean(sos_wc)), 'SOS_WC_MEDIAN': median(sos_wc),
        'SOS_WC_MIN': n(min(sos_wc)), 'SOS_WC_MAX': n(max(sos_wc)), 'SOS_WC_SHORT': n(sum(x < 20 for x in sos_wc)),
        'NAT_WC_MEAN': f1(statistics.fmean(nat_wc)), 'NAT_WC_MEDIAN': median(nat_wc),
        'SENTINELS': ', '.join(r'\texttt{' + tex(t) + '}' for t, _ in sentinels.most_common(7)),
        'SPEAKERS_MEAN': f1(statistics.fmean(len(sp) for sp in speakers)),
        'PEOPLE_NAT_EMPTY': n(sum(not items(r['People Mentioned (Natural Text)']) for r in rows)),
        'PEOPLE_SOS_EMPTY': n(sum(not items(r['People Mentioned (SOS Text)']) for r in rows)),
        'COUNTRIES_EMPTY': n(sum(not items(r['Countries Mentioned']) for r in rows)),
        'NO_ISO_LIST': ', '.join(tex(c) for c, _ in no_iso.most_common()),
        'CANON_DIFF': n(sum((r.get('Canonical URL') or r['URL']) != r['URL'] for r in rows)),
        'EX_ROW_ID': ex['Row Id'], 'EXAMPLE_ROWS': '\n'.join(f'{k} & {v} \\\\' for k, v in example),
        'SHA_HEAD': sha[:8], 'SHA_TAIL': sha[-7:],
    }


def render(values: dict[str, str]) -> str:
    text = TEMPLATE.read_text(encoding='utf-8')
    used = set(MARKER.findall(text))
    if used - values.keys() or values.keys() - used:
        sys.exit(f'ERROR: template/statistics mismatch: unfilled {sorted(used - values.keys())}, '
                 f'unused {sorted(values.keys() - used)}')
    return MARKER.sub(lambda m: values[m.group(1)], text)


def compile_pdf(tex_src: str, out: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / 'codebook.tex'
        src.write_text(tex_src, encoding='utf-8')
        if shutil.which('tectonic'):
            cmds = [['tectonic', '--keep-logs', str(src)]]
        elif shutil.which('pdflatex'):
            cmds = [['pdflatex', '-interaction=nonstopmode', '-halt-on-error', src.name]] * 2
        else:
            sys.exit('ERROR: neither tectonic nor pdflatex is on PATH')
        for cmd in cmds:
            p = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True)
            if p.returncode:
                sys.exit(f'ERROR: {cmd[0]} failed\n{p.stdout[-3000:]}\n{p.stderr[-3000:]}')
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(tmp) / 'codebook.pdf', out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--input', required=True, type=Path, help='corpus CSV to describe')
    ap.add_argument('--out', required=True, type=Path, help='output PDF; the filled .tex is written beside it')
    ap.add_argument('--version', help='release tag, e.g. v2026.10.13 (default: parsed from the file name)')
    ap.add_argument('--exclude-secretaries', nargs='*', default=['Christopher'], metavar='LASTNAME',
                    help='rows left out of the codebook statistics (default: Christopher)')
    args = ap.parse_args()

    values = stats(args.input, version_of(args.input, args.version), set(args.exclude_secretaries))
    tex_src = render(values)
    args.out.with_suffix('.tex').parent.mkdir(parents=True, exist_ok=True)
    args.out.with_suffix('.tex').write_text(tex_src, encoding='utf-8')
    compile_pdf(tex_src, args.out)
    print(f"codebook: {args.out} ({values['N_ROWS']} rows, {values['VERSION']})")


if __name__ == '__main__':
    main()
