#!/usr/bin/env python3
"""Catch-up corpus patch (2026-10): Rubio re-extraction, row fixes, Title repair.

One hash-changing release instead of several. In order:

1. Rubio text (docs/rubio_coverage_audit.md §6 step 5). rubio_reextract.py has
   re-run every Rubio row from its stored Raw Text into --reextract-dir:
     SOS Text      replaced where it changed and was accepted automatically
                   (recall did not drop, leak did not rise), or is a hand-review
                   row listed in --approve
     Natural Text  replaced, after the weekly path's cleaning, wherever it differs:
                   the legacy Natural Text of ~120 rows starts at Rubio's first
                   turn and drops the speakers before him ("all speakers", README)
2. Row fixes (--fixes, data/catchup_fixes_2026_10.csv): Date, Location, Speech
   Type and hand-fixed texts by Row Id, checked against URL; Speakers Present after enrichment, which would
   otherwise recompute it; and rows removed as out of scope.
3. Title repair and lost-sentence restore (corpus_patch_titles.patch_rows).
3b. The 2026-10-27 whole-corpus audit's text repairs (corpus_audit_repairs:
   other speakers' turns in SOS Text, stage directions, cut first words, short
   Natural Text, page chrome, lost characters, doubled paragraphs), and Speakers
   Present recomputed where the enrichment label fixes change it.
4. Enrichment of every changed row (word counts, Row Hash, Speakers Present;
   NER for rows whose text changed), then removal and dense renumbering.

The audit CSV lists every change for the verifier's --allow-changed-rows, with
the patched output's Row Ids; removed rows are listed with field "(row removed)"
and their baseline Row Id.

    python rubio_reextract.py --corpus corpus_v2026.10.26.csv --out-dir RX
    python corpus_patch_catchup.py --input corpus_v2026.10.26.csv --reextract-dir RX \\
        --approve 16951,16997,17095,17237,17347 --output corpus_v2026.10.2x.csv
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path

from corpus_audit_repairs import repair_row
from corpus_enrich_v1 import enrich_row, enrich_rows_ner, get_nlp, read_csv, speakers_present, write_manifest
from corpus_finalize_v4_30 import (
    normalize_encoding,
    strip_boilerplate,
    strip_footer,
    strip_leading_headers,
)
from corpus_patch_titles import AUDIT_COLUMNS, DEFAULT_OVERRIDES, load_overrides, patch_rows

csv.field_size_limit(10 ** 9)

_DATA = Path(__file__).resolve().parent.parent / 'data'
DEFAULT_FIXES = _DATA / 'catchup_fixes_2026_10.csv'
REMOVED = '(row removed)'
HASHED_FIXES = ('Date', 'Location', 'Speech Type', 'SOS Text', 'Natural Text')
POST_ENRICH_FIXES = ('Speakers Present',)
_HEAD = 300


def clean_natural(text: str, title: str) -> str:
    """The Natural Text half of corpus_rubio_update._clean_new_row."""
    t = normalize_encoding(text or '')
    return strip_boilerplate(strip_footer(strip_leading_headers(t)), title)


def _same(a: str, b: str) -> bool:
    return ' '.join((a or '').split()) == ' '.join((b or '').split())


def load_reextract(d: Path) -> tuple[dict, dict]:
    with (d / 'reextract_diff.csv').open(newline='', encoding='utf-8') as f:
        diff = {r['Row Id']: r for r in csv.DictReader(f)}
    with (d / 'reextract_texts.csv').open(newline='', encoding='utf-8') as f:
        texts = {r['Row Id']: r for r in csv.DictReader(f)}
    return diff, texts


def load_fixes(path: Path) -> list[dict]:
    with path.open(newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def apply_reextract(rows: list[dict], diff: dict, texts: dict, approve: set[str]) -> list[dict]:
    audit = []
    by_id = {r['Row Id']: r for r in rows}
    for rid, d in diff.items():
        r = by_id.get(rid)
        if r is None or rid not in texts:
            raise SystemExit(f'reextract row {rid} is not in the input corpus')
        base = {'Row Id': rid, 'Secretary': r.get('Secretary', ''), 'URL': r.get('URL', '')}
        if d['changed'] == '1' and (d['accepted'] == '1' or rid in approve):
            new = texts[rid]['SOS Text']
            audit.append(dict(base, field='SOS Text', old_value=r['SOS Text'][:_HEAD],
                              new_value=new[:_HEAD], source='rubio_reextract',
                              reasons=f"recall {d['old_rubio_recall']}->{d['new_rubio_recall']}; "
                                      f"leak {d['old_foreign_leak_words']}->{d['new_foreign_leak_words']}"
                                      + ('; hand-reviewed' if rid in approve else '')))
            r['SOS Text'] = new
        nat = clean_natural(texts[rid]['Natural Text'], r.get('Title', ''))
        if nat.strip() and not _same(nat, r.get('Natural Text', '')):
            audit.append(dict(base, field='Natural Text', old_value=r['Natural Text'][:_HEAD],
                              new_value=nat[:_HEAD], source='rubio_reextract',
                              reasons=f"words {len(r['Natural Text'].split())}->{len(nat.split())}"))
            r['Natural Text'] = nat
    return audit


def apply_fixes(rows: list[dict], fixes: list[dict], fields: tuple[str, ...]) -> list[dict]:
    audit = []
    by_id = {r['Row Id']: r for r in rows}
    for fx in fixes:
        if fx['field'] not in fields:
            continue
        r = by_id.get(fx['Row Id'])
        if r is None or r.get('URL') != fx['URL']:
            raise SystemExit(f"fix {fx['Row Id']} {fx['field']}: Row Id/URL do not match the input")
        if r.get(fx['field'], '') == fx['value']:
            continue
        audit.append({'Row Id': fx['Row Id'], 'Secretary': r.get('Secretary', ''), 'URL': fx['URL'],
                      'field': fx['field'], 'old_value': r.get(fx['field'], '')[:_HEAD],
                      'new_value': fx['value'][:_HEAD], 'reasons': fx['reason'], 'source': 'fixes'})
        r[fx['field']] = fx['value']
    return audit


def apply_audit_repairs(rows: list[dict]) -> list[dict]:
    """corpus_audit_repairs.repair_row on every row (the 2026-10-27 audit)."""
    audit = []
    for r in rows:
        out, fired = repair_row(r)
        for field, text in out.items():
            audit.append({'Row Id': r['Row Id'], 'Secretary': r.get('Secretary', ''),
                          'URL': r.get('URL', ''), 'field': field,
                          'old_value': (r.get(field) or '')[:_HEAD], 'new_value': text[:_HEAD],
                          'reasons': '|'.join(fired), 'source': 'corpus_audit_repairs'})
            r[field] = text
    return audit


def refresh_speakers(rows: list[dict]) -> list[dict]:
    """Speakers Present recomputed where the enrichment fixes change it (fragment
    labels, "Sa'Ar" casing, section headings); not hashed, so no Row Hash change."""
    audit = []
    for r in rows:
        new = speakers_present(r.get('Natural Text', ''), r.get('Secretary', ''))
        if new != r.get('Speakers Present', ''):
            audit.append({'Row Id': r['Row Id'], 'Secretary': r.get('Secretary', ''),
                          'URL': r.get('URL', ''), 'field': 'Speakers Present',
                          'old_value': r.get('Speakers Present', ''), 'new_value': new,
                          'reasons': 'speaker label fix', 'source': 'corpus_enrich_v1'})
            r['Speakers Present'] = new
    return audit


def remove_rows(rows: list[dict], fixes: list[dict], audit: list[dict]) -> tuple[list[dict], list[dict]]:
    """Drop the rows marked removed, renumber densely, and move audit Row Ids to
    the new numbering (removed rows keep their baseline id)."""
    drop = {fx['Row Id']: fx for fx in fixes if fx['field'] == REMOVED}
    by_id = {r['Row Id']: r for r in rows}
    for rid, fx in drop.items():
        if rid not in by_id or by_id[rid].get('URL') != fx['URL']:
            raise SystemExit(f'remove {rid}: Row Id/URL do not match the input')
    kept = [r for r in rows if r['Row Id'] not in drop]
    remap = {r['Row Id']: str(i) for i, r in enumerate(kept)}
    for r in kept:
        r['Row Id'] = remap[r['Row Id']]
    out = []
    for a in audit:
        if a['Row Id'] in drop:
            continue            # a change to a row that is removed anyway
        out.append(dict(a, **{'Row Id': remap[a['Row Id']]}))
    for rid, fx in drop.items():
        r = by_id[rid]
        out.append({'Row Id': rid, 'Secretary': r.get('Secretary', ''), 'URL': fx['URL'],
                    'field': REMOVED, 'old_value': r.get('Title', ''), 'new_value': '',
                    'reasons': fx['reason'], 'source': 'fixes'})
    return kept, out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--input', required=True, type=Path)
    ap.add_argument('--output', required=True, type=Path)
    ap.add_argument('--reextract-dir', required=True, type=Path)
    ap.add_argument('--approve', default='', help='hand-review Row Ids whose re-extracted SOS Text is accepted')
    ap.add_argument('--fixes', type=Path, default=DEFAULT_FIXES)
    ap.add_argument('--overrides', type=Path, default=DEFAULT_OVERRIDES)
    ap.add_argument('--audit', type=Path, default=None, help='default <output stem>_audit.csv')
    ap.add_argument('--spacy-model', default='en_core_web_lg')
    args = ap.parse_args()
    audit_path = args.audit or args.output.with_name(args.output.stem + '_audit.csv')

    with args.input.open(newline='', encoding='utf-8') as f:
        fieldnames = csv.DictReader(f).fieldnames
    rows = read_csv(args.input)
    diff, texts = load_reextract(args.reextract_dir)
    fixes = load_fixes(args.fixes)
    approve = {x.strip() for x in args.approve.split(',') if x.strip()}

    audit = apply_reextract(rows, diff, texts, approve)
    audit += apply_fixes(rows, fixes, HASHED_FIXES)
    pre_title = {a['Row Id'] for a in audit}
    text_pre = {a['Row Id'] for a in audit if a['field'] in ('SOS Text', 'Natural Text')}

    title_audit, title_text_changed = patch_rows(rows, load_overrides(args.overrides))
    audit += title_audit
    repair_audit = apply_audit_repairs(rows)
    audit += repair_audit
    by_id = {r['Row Id']: r for r in rows}
    for rid in pre_title | {a['Row Id'] for a in repair_audit}:
        enrich_row(by_id[rid], incremental=False)
    ner = {id(r): r for r in title_text_changed}
    ner.update({id(by_id[rid]): by_id[rid] for rid in text_pre | {a['Row Id'] for a in repair_audit}})
    if ner:
        enrich_rows_ner(list(ner.values()), get_nlp(args.spacy_model), incremental=False)
    audit += refresh_speakers(rows)
    audit += apply_fixes(rows, fixes, POST_ENRICH_FIXES)

    rows, audit = remove_rows(rows, fixes, audit)

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

    print(f'rows: {len(rows):,}; NER re-run on {len(ner):,} rows')
    for (field, sec), n in sorted(Counter((a['field'], a['Secretary']) for a in audit).items()):
        print(f'  {n:>6,}  {field:<18} {sec}')
    print(f'wrote {args.output}\n      {manifest}\n      {audit_path}')


if __name__ == '__main__':
    main()
