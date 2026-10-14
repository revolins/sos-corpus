#!/usr/bin/env python3
"""Corpus patch v2026.10.02: strip two kinds of state.gov page chrome from the text.

Found by the 2026-10-02 boilerplate audit of the STM filler lexicon:

  1. a Clinton/Kerry business-sidebar nav block scraped onto the END of 7 rows'
     SOS Text ("... Go Back Highlights Learn More Business Business Support:
     Frequently Asked Questions (FAQ) Office of Global Partnerships ...");
  2. a translation-link header at the START of 2 rows' SOS and Natural Text
     ("PDF version As Prepared Persian Translation [PDF] ...",
     "Translations: Arabic (top PDF)| Hebrew (bottom PDF) ...").

Both rules now live in corpus_finalize_v4_30.py (FOOTER_RES entry and
strip_translation_links), so future weekly rows are cleaned at finalize time.
This script applies ONLY those two rules to an existing release -- not the whole
finalize chain, which could touch rows unrelated to this patch -- and recomputes
the derived columns of the changed rows: SOS/Natural Word Count, Row Hash,
Speakers Present (enrich_row) and People/Countries Mentioned (enrich_rows_ner).

Writes the patched CSV, its manifest sidecar, and an audit CSV (Row Id, field,
chars before/after, removed text) that the verifier's --allow-changed-rows uses
so Check Q accepts exactly these edits and nothing else.

    python corpus_patch_v2026_10_02.py \\
        --input ../data/corpus_v2026.09.03-enriched.csv \\
        --output ../data/corpus_v2026.10.02-enriched.csv
"""

from __future__ import annotations

import argparse
import csv
import difflib
from pathlib import Path

from corpus_enrich_v1 import enrich_row, enrich_rows_ner, get_nlp, read_csv, write_manifest
from corpus_finalize_v4_30 import FOOTER_RES, strip_translation_links

TEXT_FIELDS = ("SOS Text", "Natural Text")
NAV_RE = next(p for p in FOOTER_RES if "Highlights" in p.pattern)


def patch_text(text: str) -> str:
    text = NAV_RE.sub("", text or "").rstrip() if NAV_RE.search(text or "") else (text or "")
    return strip_translation_links(text)


def apply_patch(input_csv: Path, output_csv: Path, audit_path: Path | None, patch_text,
                spacy_model: str = "en_core_web_lg") -> None:
    """Apply ``patch_text`` to SOS/Natural Text of every row of a release, recompute the
    changed rows' derived columns, and write the CSV, its manifest and an audit CSV."""
    audit_path = audit_path or output_csv.with_name(output_csv.stem + "_patch_audit.csv")
    with input_csv.open(newline="", encoding="utf-8") as f:
        fieldnames = csv.DictReader(f).fieldnames
    rows = read_csv(input_csv)

    audit, changed = [], []
    for r in rows:
        touched = False
        for col in TEXT_FIELDS:
            before = r.get(col, "") or ""
            after = patch_text(before)  # the rule passed in (this script's own by default)
            if after != before:
                # Every deleted/replaced segment, wherever it is in the text (a prefix/
                # suffix shortcut misreports edits in the middle of a document).
                ops = difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes()
                segs = [before[i1:i2] for tag, i1, i2, _, _ in ops if tag in ("delete", "replace")]
                audit.append({"Row Id": r["Row Id"], "Secretary": r.get("Secretary", ""), "field": col,
                              "chars_before": len(before), "chars_after": len(after), "n_edits": len(segs),
                              "removed_text": " | ".join(x.strip() or repr(x) for x in segs)[:400]})
                r[col] = after
                touched = True
        if touched:
            changed.append(r)

    for r in changed:
        enrich_row(r, incremental=False)
    if changed:
        enrich_rows_ner(changed, get_nlp(spacy_model), incremental=False)

    with output_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="raise")
        w.writeheader()
        w.writerows(rows)
    manifest = output_csv.with_name(output_csv.name + ".manifest.json")
    write_manifest(manifest, output_csv, rows)
    with audit_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["Row Id", "Secretary", "field", "chars_before", "chars_after", "n_edits", "removed_text"])
        w.writeheader()
        w.writerows(audit)

    print(f"rows: {len(rows):,}; changed rows: {len(changed)}; changed fields: {len(audit)}")
    for a in audit:
        print(f"  [{a['Row Id']}] {a['Secretary']:9s} {a['field']:12s} {a['chars_before']:>6} -> {a['chars_after']:<6} "
              f"removed: {a['removed_text'][:90]!r}")
    print(f"wrote {output_csv}\n      {manifest}\n      {audit_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--audit", type=Path, default=None, help="default <output stem>_patch_audit.csv")
    ap.add_argument("--spacy-model", default="en_core_web_lg")
    args = ap.parse_args()
    apply_patch(args.input, args.output, args.audit, patch_text, args.spacy_model)


if __name__ == "__main__":
    main()
