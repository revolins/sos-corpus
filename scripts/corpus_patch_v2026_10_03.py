#!/usr/bin/env python3
"""Corpus patch v2026.10.03: remove the "ã——" paragraph-separator mojibake.

17 Clinton/Kerry rows carry 259 copies of "ã——" where a paragraph-separator
character was mis-encoded on the source pages (it is in Raw Text too).  It always
sits at a paragraph boundary and carries no content, but it hides sentence breaks:
one Kerry speech became a single 7,087-character "sentence" that overflowed the
alignment labeller's context window.

The rule (corpus_finalize_v4_30.strip_separator_mojibake, also applied inside
normalize_encoding so new weekly rows are repaired at finalize time) removes the
artifact and keeps the paragraph break.  As with every text-cleaning pass, it is
applied to SOS Text and Natural Text only; Raw Text stays the archival copy.

Uses the same machinery as the v2026.10.02 patch (corpus_patch_v2026_10_02.apply_patch):
changed rows get word counts, Row Hash, Speakers Present and NER recomputed, and an
audit CSV lists every change for the verifier's --allow-changed-rows.

    python corpus_patch_v2026_10_03.py \\
        --input ../data/release_v2026.10.02/corpus_v2026.10.02.csv \\
        --output ../data/release_v2026.10.03/corpus_v2026.10.03.csv
"""

from __future__ import annotations

import argparse
from pathlib import Path

from corpus_finalize_v4_30 import strip_separator_mojibake
from corpus_patch_v2026_10_02 import apply_patch


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--audit", type=Path, default=None, help="default <output stem>_patch_audit.csv")
    ap.add_argument("--spacy-model", default="en_core_web_lg")
    args = ap.parse_args()
    apply_patch(args.input, args.output, args.audit, strip_separator_mojibake, args.spacy_model)


if __name__ == "__main__":
    main()
