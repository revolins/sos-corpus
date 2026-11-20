"""Alignment tests for ONE real bundle: may CI build a release from it?

Run by local_fetch.py before it commits, and by CI on the PR and before import:
  SOS_BUNDLE      the bundle directory (local_fetch/bundles/<date>)
  SOS_DATA        what was downloaded for the latest release, untouched: corpus.csv,
                  the state assets, rubio_gt_missing.txt, the checked-in seed files
  SOS_LATEST_TAG  the latest release's tag
Skipped when SOS_BUNDLE is unset (the ordinary test suite has no bundle).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bundle as B  # noqa: E402

BUNDLE = os.environ.get('SOS_BUNDLE')
pytestmark = pytest.mark.skipif(not BUNDLE, reason='SOS_BUNDLE not set: no bundle to check')


@pytest.fixture(scope='module')
def env():
    data, tag = os.environ.get('SOS_DATA'), os.environ.get('SOS_LATEST_TAG')
    assert data and tag, 'SOS_DATA and SOS_LATEST_TAG must be set with SOS_BUNDLE'
    return Path(BUNDLE), Path(data), tag


def _ok(problems):
    assert not problems, '\n'.join(problems)


def test_bundle_is_well_formed_and_clean(env):
    _ok(B.check_hygiene(env[0]))


def test_bundle_starts_from_the_current_release(env):
    _ok(B.check_base(*env))


def test_fetched_with_code_that_is_on_main(env):
    _ok(B.check_commit(env[0], os.environ.get('SOS_MAIN_REF', 'origin/main')))


def test_ran_the_weekly_scrape_command(env):
    _ok(B.check_args(env[0]))


def test_replay_uses_the_fetch_parsing_environment(env):
    _ok(B.check_env(env[0]))


def test_replay_reproduces_every_output(env):
    _ok(B.replay(env[0], env[1] / 'corpus.csv'))


def test_run_state_only_moves_forward(env):
    _ok(B.check_forward(env[0], env[1] / 'corpus.csv'))
