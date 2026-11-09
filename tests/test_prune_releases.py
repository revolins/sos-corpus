"""Release retention rule (scripts/prune_releases.py)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import prune_releases as pr  # noqa: E402

NOW = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)


def rel(tag, created, pre=False, body=''):
    return {'tagName': tag, 'createdAt': f'{created}T12:00:00Z', 'isPrerelease': pre, 'body': body}


RELEASES = [
    rel('v2026.07.14', '2026-10-14'),
    rel('v2026.10.26', '2026-10-26'),
    rel('v2026.10.27', '2026-10-27'),
    rel('gt-2026.10.20', '2026-10-20', pre=True),
    rel('gt-2026.10.27', '2026-10-27', pre=True),
    rel('v2026.11.02', '2026-11-02'),
    rel('v2026.09.03-enriched', '2026-11-10'),      # republished paper release
    rel('v2026.11.16', '2026-11-16'),
    rel('some-other-thing', '2026-01-01'),
]


def test_keeps_latest_superseded_paper_and_newest_gt():
    delete, keep = pr.plan(RELEASES, NOW, 'v2026.11.16')
    assert keep['v2026.11.16'] == 'latest'
    assert keep['v2026.11.02'].startswith('superseded')
    assert keep['v2026.09.03-enriched'].startswith('protected')
    assert keep['gt-2026.10.27'] == 'newest ground truth'
    assert delete == ['v2026.07.14', 'gt-2026.10.20', 'v2026.10.26', 'v2026.10.27']
    assert 'some-other-thing' not in delete


def test_grace_keeps_anything_younger_than_a_week():
    delete, keep = pr.plan(RELEASES, datetime(2026, 11, 1, tzinfo=timezone.utc), 'v2026.10.27')
    assert 'v2026.10.26' in keep and 'v2026.10.27' in keep
    assert delete == ['v2026.07.14', 'gt-2026.10.20']      # 12 days old, not the newest gt


def test_keep_marker_protects_any_release():
    rels = RELEASES + [rel('v2026.08.24-enriched', '2026-10-14', body=f'Frozen.\n{pr.KEEP_MARKER}')]
    delete, keep = pr.plan(rels, NOW, 'v2026.11.16')
    assert 'v2026.08.24-enriched' in keep and 'v2026.08.24-enriched' not in delete


def test_paper_release_is_never_the_superseded_slot():
    # The paper release is newer by creation date than v2026.11.02 but is not a
    # weekly release; the superseded slot must still go to v2026.11.02.
    delete, keep = pr.plan(RELEASES, NOW, 'v2026.11.16')
    assert keep['v2026.11.02'].startswith('superseded')


def test_unknown_latest_refuses():
    with pytest.raises(ValueError):
        pr.plan(RELEASES, NOW, 'v2099.01.01')
    with pytest.raises(ValueError):
        pr.plan(RELEASES, NOW, 'gt-2026.10.27')
