#!/usr/bin/env python3
"""Corpus patch: make every Title the state.gov page headline.

Found by the 2026-10-22 Title audit (docs/title_repair.md). About 5,400 of
v2026.10.19's 17,708 titles were written by the one-off v4.4-v4.11 finalize
passes from something other than the headline:

  speech_sentence  the speech's first sentence ("That's right.", "On behalf of the
                   United States of America, I congratulate ...")
  slug_derived     the URL slug, title-cased, often with words after the
                   secretary's name cut ("And Greet With Mission Kuwait",
                   "Kazakhstans Republic Day")
  split_heading    half of a 1997-2001 heading split over two lines
                   ("And Italian Foreign Minister Lamberto Dini")
  headline_mismatch  any other difference on a 2017+ page, where the headline is
                   exactly the page's own (another item's title copied onto the
                   row, a <title> shortened from the on-page headline, ...)

corpus_title.repair_title() recovers the headline from Raw Text; for the ~130
rows whose Raw Text has no page header, data/title_overrides.csv carries the
title the original scrape recorded (status "override"), or marks the row
"unresolved" so the old title stays and Check AJ only warns.

Only Title changes. Row Hash covers Title, so it is recomputed (enrich_row);
the text and therefore word counts, Speakers Present and NER are untouched.
Writes the patched CSV, its manifest and an audit CSV (Row Id, field, old/new
title, reasons, source) for the verifier's --allow-changed-rows.

    python corpus_patch_titles.py \\
        --input ../data/release_v2026.10.19/corpus_v2026.10.19.csv \\
        --output ../data/release_v2026.10.2x/corpus_v2026.10.2x.csv
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path

from corpus_enrich_v1 import enrich_row, read_csv, write_manifest
from corpus_title import repair_title

DEFAULT_OVERRIDES = Path(__file__).resolve().parent.parent / 'data' / 'title_overrides.csv'
AUDIT_COLUMNS = ['Row Id', 'Secretary', 'field', 'URL', 'old_title', 'new_title', 'reasons', 'source']


def load_overrides(path: Path) -> dict[str, str]:
    """{URL: title} for the rows marked "override"."""
    if not path.exists():
        return {}
    with path.open(newline='', encoding='utf-8') as f:
        return {a['URL']: a['title'] for a in csv.DictReader(f)
                if a.get('status') == 'override' and a.get('title')}


def patch_rows(rows: list[dict], overrides: dict[str, str]) -> list[dict]:
    """Repair Title in place; return one audit record per changed row."""
    audit = []
    for r in rows:
        old = r.get('Title', '') or ''
        new, reasons, source = repair_title(r)
        if not new and r.get('URL', '') in overrides:
            new, source = overrides[r['URL']], 'title_overrides.csv'
            reasons = reasons or ['override']
        if not new or new == old:
            continue
        r['Title'] = new
        enrich_row(r, incremental=False)
        audit.append({'Row Id': r['Row Id'], 'Secretary': r.get('Secretary', ''), 'field': 'Title',
                      'URL': r.get('URL', ''), 'old_title': old, 'new_title': new,
                      'reasons': '|'.join(reasons), 'source': source})
    return audit


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--input', required=True, type=Path)
    ap.add_argument('--output', required=True, type=Path)
    ap.add_argument('--audit', type=Path, default=None, help='default <output stem>_title_audit.csv')
    ap.add_argument('--overrides', type=Path, default=DEFAULT_OVERRIDES)
    args = ap.parse_args()
    audit_path = args.audit or args.output.with_name(args.output.stem + '_title_audit.csv')

    with args.input.open(newline='', encoding='utf-8') as f:
        fieldnames = csv.DictReader(f).fieldnames
    rows = read_csv(args.input)
    audit = patch_rows(rows, load_overrides(args.overrides))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='raise')
        w.writeheader()
        w.writerows(rows)
    manifest = args.output.with_name(args.output.name + '.manifest.json')
    write_manifest(manifest, args.output, rows)
    with audit_path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=AUDIT_COLUMNS)
        w.writeheader()
        w.writerows(audit)

    print(f'rows: {len(rows):,}; titles changed: {len(audit):,}')
    for k, v in Counter(a['reasons'] for a in audit).most_common():
        print(f'  {v:>6,}  {k}')
    for k, v in Counter(a['Secretary'] for a in audit).most_common():
        print(f'  {v:>6,}  {k}')
    print(f'wrote {args.output}\n      {manifest}\n      {audit_path}')


if __name__ == '__main__':
    main()
