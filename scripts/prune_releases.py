#!/usr/bin/env python3
"""Keep GitHub releases down to: the latest corpus release, the paper release, and
the newest ground truth.

Retention rule (decided 2026-11-09):
  * the latest corpus release (GitHub's "Latest");
  * the corpus release it superseded and any release younger than GRACE_DAYS, so
    the lab server's daily mirror (scripts/mirror_releases.sh) has a week to back
    a release up before it disappears;
  * protected releases: the tags in PROTECTED (the frozen corpus the paper and the
    replication package cite) and any release whose notes contain KEEP_MARKER;
  * the newest gt-* ground-truth prerelease (the weekly run downloads it).
Everything else that looks like a corpus (vYYYY.MM.DD...) or ground-truth (gt-...)
release is deleted together with its tag. Other releases are never touched.

  python scripts/prune_releases.py --dry-run     # print the plan only
  python scripts/prune_releases.py               # delete
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

PROTECTED = frozenset({'v2026.09.03-enriched'})
KEEP_MARKER = '<!-- keep: paper -->'
GRACE_DAYS = 7
_CORPUS_RE = re.compile(r'^v\d{4}\.\d{2}\.\d{2}')
_GT_RE = re.compile(r'^gt-\d{4}\.\d{2}\.\d{2}')


def _ts(rel: dict) -> datetime:
    return datetime.fromisoformat(rel['createdAt'].replace('Z', '+00:00'))


def plan(releases: list[dict], now: datetime, latest_tag: str) -> tuple[list[str], dict[str, str]]:
    """Return (tags to delete, {kept tag: reason}).

    `releases`: dicts with tagName, createdAt (ISO), isPrerelease, body.
    Raises ValueError when the latest release is not a corpus release in the list --
    deleting against an unknown "latest" could remove the only good release.
    """
    by_tag = {r['tagName']: r for r in releases}
    if latest_tag not in by_tag or not _CORPUS_RE.match(latest_tag):
        raise ValueError(f'latest release {latest_tag!r} is not a corpus release in the list')
    keep: dict[str, str] = {latest_tag: 'latest'}
    for r in releases:
        tag = r['tagName']
        if tag in PROTECTED or KEEP_MARKER in (r.get('body') or ''):
            keep.setdefault(tag, 'protected (paper)')
    corpus = sorted((r for r in releases
                     if _CORPUS_RE.match(r['tagName']) and not r.get('isPrerelease')
                     and r['tagName'] not in keep),
                    key=_ts, reverse=True)
    older = [r for r in corpus if _ts(r) <= _ts(by_tag[latest_tag])]
    if older:
        keep.setdefault(older[0]['tagName'], 'superseded by latest (mirror grace)')
    for r in releases:
        if (_CORPUS_RE.match(r['tagName']) or _GT_RE.match(r['tagName'])) \
                and now - _ts(r) <= timedelta(days=GRACE_DAYS):
            keep.setdefault(r['tagName'], f'younger than {GRACE_DAYS} days')
    gts = sorted((r for r in releases if _GT_RE.match(r['tagName'])), key=_ts, reverse=True)
    if gts:
        keep.setdefault(gts[0]['tagName'], 'newest ground truth')
    delete = [r['tagName'] for r in sorted(releases, key=_ts)
              if r['tagName'] not in keep
              and (_CORPUS_RE.match(r['tagName']) or _GT_RE.match(r['tagName']))]
    return delete, keep


def _gh(*args: str) -> str:
    return subprocess.run(['gh', *args], check=True, capture_output=True, text=True).stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--repo', default=None, help='owner/name (default: the current repo)')
    ap.add_argument('--dry-run', action='store_true', help='print the plan, delete nothing')
    args = ap.parse_args(argv)
    repo = ['-R', args.repo] if args.repo else []

    listed = json.loads(_gh('release', 'list', *repo, '--limit', '200',
                            '--json', 'tagName,createdAt,isPrerelease,isLatest'))
    latest = [r['tagName'] for r in listed if r.get('isLatest')]
    if len(latest) != 1:
        print(f'[prune_releases] ERROR: expected one Latest release, got {latest}', file=sys.stderr)
        return 1
    for r in listed:          # notes are not in `release list`; only needed for the marker
        r['body'] = _gh('release', 'view', r['tagName'], *repo, '--json', 'body', '-q', '.body')
    delete, keep = plan(listed, datetime.now(timezone.utc), latest[0])

    for tag, why in sorted(keep.items()):
        print(f'[prune_releases] keep    {tag:28s} {why}')
    for tag in delete:
        print(f'[prune_releases] {"would delete" if args.dry_run else "delete"}  {tag}')
    if args.dry_run or not delete:
        return 0
    for tag in delete:
        _gh('release', 'delete', tag, *repo, '--cleanup-tag', '--yes')
    print(f'[prune_releases] deleted {len(delete)} release(s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
