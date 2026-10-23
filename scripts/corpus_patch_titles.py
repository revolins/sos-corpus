#!/usr/bin/env python3
"""Corpus patch: make every Title the state.gov page headline, and restore the
text the old titles took with them.

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

Text: corpus_finalize_v4_30.strip_leading_title deleted the leading lines that
repeated the Title, so where the Title was the first sentence, the sentence is
gone from the text (~640 rows: written statements' opening sentence, interviews'
first Secretary answer in SOS Text). corpus_title.restore_row puts it back from
Raw Text / Natural Text, using the OLD title -- so it runs before the repair.

Changed rows get word counts, Row Hash, Speakers Present (enrich_row) and, for
rows whose text changed, People/Countries Mentioned (enrich_rows_ner)
recomputed. Writes the patched CSV, its manifest and an audit CSV (Row Id,
field, old/new value, reasons, source) for the verifier's --allow-changed-rows;
rows the text restore declined are listed with field "review".

    python corpus_patch_titles.py \\
        --input ../data/release_v2026.10.19/corpus_v2026.10.19.csv \\
        --output ../data/release_v2026.10.2x/corpus_v2026.10.2x.csv
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from pathlib import Path

from corpus_enrich_v1 import enrich_row, enrich_rows_ner, get_nlp, read_csv, write_manifest
from corpus_finalize_v4_30 import normalize_encoding, strip_stage_directions
from corpus_title import RESTORE_FAILURES, repair_title, restore_row

DEFAULT_OVERRIDES = Path(__file__).resolve().parent.parent / 'data' / 'title_overrides.csv'
AUDIT_COLUMNS = ['Row Id', 'Secretary', 'field', 'URL', 'old_value', 'new_value', 'reasons', 'source']
TEXT_FIELDS = ('SOS Text', 'Natural Text')
_HEAD = 300  # chars of a text field kept in the audit


def load_overrides(path: Path) -> dict[str, str]:
    """{URL: title} for the rows marked "override"."""
    if not path.exists():
        return {}
    with path.open(newline='', encoding='utf-8') as f:
        return {a['URL']: a['title'] for a in csv.DictReader(f)
                if a.get('status') == 'override' and a.get('title')}


def patch_rows(rows: list[dict], overrides: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """Restore removed text, then repair Title, in place.

    Returns (audit records, rows whose text changed -- they need NER).
    """
    audit, text_changed = [], []
    for r in rows:
        old = r.get('Title', '') or ''
        base = {'Row Id': r['Row Id'], 'Secretary': r.get('Secretary', ''), 'URL': r.get('URL', '')}
        restored, status = restore_row(r, old, normalize_encoding)
        if 'SOS Text' in restored:
            # The restored head comes from Natural Text, so it still carries the
            # stage directions finalize strips from SOS Text ("(Laughter.)"); the
            # rest of the text has had this pass already, so it is idempotent there.
            sos = strip_stage_directions(restored['SOS Text'])
            # a stage direction removed from the restored head can leave an extra
            # blank line at the seam
            old_core = (r.get('SOS Text') or '').lstrip()
            if old_core and sos.endswith(old_core):
                head = sos[:len(sos) - len(old_core)]
                sos = re.sub(r'\n{3,}', '\n\n', head) + old_core
            if sos == (r.get('SOS Text') or ''):
                del restored['SOS Text']
            else:
                restored['SOS Text'] = sos
        for field, text in restored.items():
            audit.append(dict(base, field=field, old_value=(r.get(field) or '')[:_HEAD],
                              new_value=text[:_HEAD], reasons='title_sentence_removed',
                              source='Raw Text' if field == 'Natural Text' else 'Natural Text'))
            r[field] = text
        failed = sorted(f for f, s in status.items() if s in RESTORE_FAILURES)
        if failed:
            audit.append(dict(base, field='review', old_value=old, new_value='',
                              reasons='text_restore_declined',
                              source='; '.join(f'{f}: {status[f]}' for f in failed)))
        new, reasons, source = repair_title(r)
        if not new and r.get('URL', '') in overrides:
            new, source = overrides[r['URL']], 'title_overrides.csv'
            reasons = reasons or ['override']
        title_changed = bool(new) and new != old
        if title_changed:
            r['Title'] = new
            audit.append(dict(base, field='Title', old_value=old, new_value=new,
                              reasons='|'.join(reasons), source=source))
        if restored or title_changed:
            enrich_row(r, incremental=False)
        if restored:
            text_changed.append(r)
    return audit, text_changed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--input', required=True, type=Path)
    ap.add_argument('--output', required=True, type=Path)
    ap.add_argument('--audit', type=Path, default=None, help='default <output stem>_title_audit.csv')
    ap.add_argument('--overrides', type=Path, default=DEFAULT_OVERRIDES)
    ap.add_argument('--spacy-model', default='en_core_web_lg')
    args = ap.parse_args()
    audit_path = args.audit or args.output.with_name(args.output.stem + '_title_audit.csv')

    with args.input.open(newline='', encoding='utf-8') as f:
        fieldnames = csv.DictReader(f).fieldnames
    rows = read_csv(args.input)
    audit, text_changed = patch_rows(rows, load_overrides(args.overrides))
    if text_changed:
        enrich_rows_ner(text_changed, get_nlp(args.spacy_model), incremental=False)

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

    titles = [a for a in audit if a['field'] == 'Title']
    review = [a for a in audit if a['field'] == 'review']
    print(f'rows: {len(rows):,}; titles changed: {len(titles):,}; rows with text restored: '
          f'{len(text_changed):,}; text restores declined (review): {len(review)}')
    for k, v in Counter(a['reasons'] for a in titles).most_common():
        print(f'  {v:>6,}  {k}')
    for k, v in Counter(a['Secretary'] for a in titles).most_common():
        print(f'  {v:>6,}  {k}')
    print('  text restored by secretary:', dict(Counter(r.get('Secretary', '') for r in text_changed)))
    print(f'wrote {args.output}\n      {manifest}\n      {audit_path}')


if __name__ == '__main__':
    main()
