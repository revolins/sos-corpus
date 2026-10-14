"""U.S. Secretary of State speech corpus loader.

Fetches the latest corpus release from GitHub and optionally loads it as a Pandas DataFrame.
Caches downloaded CSVs locally so repeated calls don't re-download.

Examples:
    Download and cache:
        >>> import sos_speeches
        >>> csv_path = sos_speeches.load()
        >>> df = sos_speeches.load(as_dataframe=True)

    With a custom cache location:
        >>> df = sos_speeches.load(as_dataframe=True, cache_dir="/data/my_cache")
"""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.request import urlopen

if TYPE_CHECKING:
    import pandas as pd

__version__ = '0.1.0'

GITHUB_REPO = 'revolins/sos-corpus'
GITHUB_RELEASES_API = f'https://api.github.com/repos/{GITHUB_REPO}/releases/latest'


def _get_cache_dir(custom_dir: str | None = None) -> Path:
    """Return cache directory, creating it if needed."""
    if custom_dir:
        cache_dir = Path(custom_dir)
    else:
        cache_dir = Path.home() / '.cache' / 'sos_speeches'
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _fetch_latest_release() -> dict:
    """Fetch the latest release metadata from GitHub API."""
    try:
        with urlopen(GITHUB_RELEASES_API, timeout=10) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        raise RuntimeError(
            f'Failed to fetch latest release from {GITHUB_RELEASES_API}: {e}'
        ) from e


def load(
    as_dataframe: bool = False,
    cache_dir: str | None = None,
) -> str | 'pd.DataFrame':
    """Download the latest corpus release CSV and return its local path or DataFrame.

    Args:
        as_dataframe: If True, return a Pandas DataFrame; otherwise return the CSV path (str).
        cache_dir: Custom cache directory. Defaults to ~/.cache/sos_speeches/.

    Returns:
        Path to cached CSV (str), or a Pandas DataFrame if as_dataframe=True.

    Raises:
        RuntimeError: If the release fetch or download fails.
        ImportError: If as_dataframe=True but pandas is not installed.
    """
    cache_path = _get_cache_dir(cache_dir)

    # Fetch latest release metadata
    release = _fetch_latest_release()
    tag = release.get('tag_name', 'unknown')
    csv_filename = f'corpus_{tag}.csv'
    local_csv = cache_path / csv_filename

    # Download if not cached
    if not local_csv.exists():
        assets = release.get('assets', [])
        csv_asset = next(
            (a for a in assets if a['name'].startswith('corpus_') and a['name'].endswith('.csv')),
            None,
        )
        if not csv_asset:
            raise RuntimeError(
                f'No corpus CSV found in release {tag}. '
                f'Available assets: {[a["name"] for a in assets]}'
            )

        download_url = csv_asset['browser_download_url']
        print(f'Downloading {csv_asset["name"]} from {tag}...')
        try:
            with urlopen(download_url, timeout=300) as response:
                with open(local_csv, 'wb') as out:
                    shutil.copyfileobj(response, out)
            print(f'  ✓ Cached: {local_csv}')
        except Exception as e:
            raise RuntimeError(f'Failed to download {download_url}: {e}') from e
    else:
        print(f'Using cached CSV: {local_csv}')

    if as_dataframe:
        try:
            import pandas as pd
        except ImportError as e:
            raise ImportError(
                'pandas is required for as_dataframe=True. '
                'Install with: pip install sos-speeches[pandas]'
            ) from e

        csv.field_size_limit(10 ** 8)
        return pd.read_csv(local_csv, dtype=str, keep_default_na=False)

    return str(local_csv)
