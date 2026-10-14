#!/usr/bin/env python3
"""Push corpus CSV + Parquet mirror to Hugging Face Hub dataset repo.

Requires: HF write token in HF_TOKEN env var or via `huggingface-cli login`.
Creates repo if it doesn't exist (dataset visibility follows HF default).

Usage:
    python publish_hf.py --csv corpus.csv --commit-message "Your message"
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

REPO_ID = "JayRevolinsky/sos-secretary-speeches"


def _import_deps():
    """Import the publish dependencies, reporting the REAL failure.

    pyarrow is imported explicitly rather than left to pandas. pandas'
    get_engine() catches whatever pyarrow raises and re-reports it as
    "Unable to find a usable engine; tried using: 'pyarrow', 'fastparquet'",
    which reads like a missing package even when pip installed one
    successfully. On 2026-10-12 the real cause was an ABI mismatch -- an
    analysis-only numpy<2 pin (tomotopy) had been resolved into the pipeline
    environment, so a numpy-2-built pyarrow wheel could not import. Print the
    three versions so that class of failure is diagnosable from the CI log.
    """
    try:
        from huggingface_hub import HfApi
        import pandas as pd
        import numpy as np
        import pyarrow
    except Exception as e:
        sys.exit(
            f'ERROR: publish dependencies are not usable: '
            f'{type(e).__name__}: {e}\n'
            f'       Needed: huggingface_hub, pandas, pyarrow (and a numpy '
            f'matching the pyarrow wheel).\n'
            f'       These are pinned in scripts/requirements.txt. If the '
            f'failure is an ABI/import error rather than a missing package, '
            f'something has reintroduced a numpy cap -- check that the '
            f'analysis stack in requirements-analysis.txt has not leaked '
            f'back into requirements.txt.'
        )
    print(
        f'  deps: pandas {pd.__version__}, numpy {np.__version__}, '
        f'pyarrow {pyarrow.__version__}'
    )
    return HfApi, pd


def main() -> None:
    ap = argparse.ArgumentParser(
        description='Push corpus to Hugging Face Hub'
    )
    ap.add_argument('--csv', help='Input CSV file (10-column schema)')
    ap.add_argument('--commit-message', help='Commit message for HF push')
    ap.add_argument(
        '--check-deps', action='store_true',
        help='Verify the publish dependencies import, then exit. Run this '
             'BEFORE cutting a release: a failure here used to surface only '
             'after the release was published, leaving the HF mirror behind.'
    )
    args = ap.parse_args()

    if args.check_deps:
        _import_deps()
        print('✓ publish dependencies OK')
        return

    for required in ('csv', 'commit_message'):
        if not getattr(args, required):
            ap.error(f'--{required.replace("_", "-")} is required unless --check-deps is given')

    csv_path = Path(args.csv)
    if not csv_path.exists():
        sys.exit(f'ERROR: CSV not found: {csv_path}')

    HfApi, pd = _import_deps()

    print(f'Loading CSV: {csv_path}')
    csv.field_size_limit(10 ** 8)
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    print(f'  {len(df):,} rows, {len(df.columns)} columns')

    # Convert to Parquet
    parquet_path = csv_path.with_suffix('.parquet')
    print(f'Converting to Parquet: {parquet_path}')
    df.to_parquet(parquet_path, index=False)

    # Push to HF
    api = HfApi()
    print(f'Creating/accessing HF dataset repo: {REPO_ID}')
    api.create_repo(REPO_ID, repo_type='dataset', exist_ok=True)

    for src_path, dest_name in [(str(csv_path), 'corpus.csv'), (str(parquet_path), 'corpus.parquet')]:
        print(f'Uploading {dest_name} to {REPO_ID}...')
        try:
            api.upload_file(
                path_or_fileobj=src_path,
                path_in_repo=dest_name,
                repo_id=REPO_ID,
                repo_type='dataset',
                commit_message=args.commit_message,
            )
            print(f'  ✓ {dest_name} uploaded')
        except Exception as e:
            sys.exit(f'ERROR uploading {dest_name}: {e}')

    print(f'\n✓ Successfully pushed {len(df):,} rows to {REPO_ID}')


if __name__ == '__main__':
    main()
