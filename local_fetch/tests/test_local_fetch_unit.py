"""local_fetch/ offline: a synthetic bundle built from the scraper's test fixtures
passes every check, and each kind of tampering or staleness is caught."""
from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
import sys
from contextlib import redirect_stderr
from datetime import date
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent / 'tests'))
import bundle as B  # noqa: E402
import local_fetch as LF  # noqa: E402
from test_rubio_backfill_pipeline import (  # noqa: E402,F401 -- _offline is autouse
    FakeSession, _e2e_routes, _e2e_setup, _offline, rb)

TAG = 'v2026.11.02'


def _rewrite(path: Path, text: str, bundle: Path, sub: str) -> None:
    path.write_text(text, encoding='utf-8')
    m = B.load_manifest(bundle)
    m[sub][path.name] = B.sha256(path)
    (bundle / 'manifest.json').write_text(json.dumps(m, indent=1, sort_keys=True) + '\n')


@pytest.fixture
def made(tmp_path, monkeypatch):
    """(bundle, release_dir): a bundle recorded in-process with the fixture routes."""
    (tmp_path / 'src').mkdir()
    corpus, out, excl, supp = _e2e_setup(tmp_path / 'src')
    release = tmp_path / 'release'
    release.mkdir()
    shutil.copyfile(corpus, release / 'corpus.csv')
    shutil.copyfile(out / 'rubio_pending.csv', release / 'rubio_pending.csv')
    shutil.copyfile(excl, release / 'rubio_coverage_exclusions.csv')
    shutil.copyfile(supp, release / 'rubio_supplemental_urls.txt')
    bundle, run = tmp_path / 'bundles' / '2026-11-23', tmp_path / 'run'
    (bundle / 'inputs').mkdir(parents=True)
    run.mkdir()
    for p in release.iterdir():
        shutil.copyfile(p, run / p.name)
        if p.name != 'corpus.csv':
            shutil.copyfile(p, bundle / 'inputs' / p.name)
    monkeypatch.setattr(rb.requests, 'Session', lambda: FakeSession(_e2e_routes(tmp_path)))
    monkeypatch.setattr(rb.time, 'sleep', lambda s: None)
    monkeypatch.setattr(sys, 'argv', ['rubio_backfill.py', *rb.ci_args(run, 'incremental'),
                                      '--rate-limit', '2', '--record', str(bundle / 'http')])
    with redirect_stderr(io.StringIO()):
        rb.main()
    (bundle / 'outputs').mkdir()
    for name in B.OUTPUTS + B.OPTIONAL_OUTPUTS:
        if (run / name).exists():
            shutil.copyfile(run / name, bundle / 'outputs' / name)
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=B.REPO, capture_output=True,
                            text=True).stdout.strip()
    m = {'format': 1, 'base_tag': TAG, 'corpus_sha256': B.sha256(release / 'corpus.csv'),
         'commit': commit, 'mode': 'incremental',
         'ci_args': B.relative_args(rb.ci_args(run, 'incremental'), run),
         'extra_args': ['--rate-limit', '2'], 'env': B.env_fingerprint(),
         'inputs': {p.name: B.sha256(p) for p in (bundle / 'inputs').iterdir()},
         'outputs': {p.name: B.sha256(p) for p in (bundle / 'outputs').iterdir()}}
    (bundle / 'manifest.json').write_text(json.dumps(m, indent=1, sort_keys=True) + '\n')
    return bundle, release


def test_a_good_bundle_passes_everything(made):
    bundle, release = made
    assert B.check_hygiene(bundle) == []
    assert B.check_base(bundle, release, TAG) == []
    assert B.check_commit(bundle, 'HEAD') == []
    assert B.check_args(bundle) == []
    assert B.check_env(bundle) == []
    assert B.replay(bundle, release / 'corpus.csv') == []          # separate process, no network
    assert B.check_forward(bundle, release / 'corpus.csv') == []
    with open(bundle / 'outputs' / 'rubio_new.csv', newline='', encoding='utf-8') as f:
        assert len(list(csv.DictReader(f))) == 4


def test_an_edited_row_fails_the_replay(made):
    bundle, release = made
    p = bundle / 'outputs' / 'rubio_new.csv'
    _rewrite(p, p.read_text(encoding='utf-8').replace('Good afternoon', 'Good evening', 1),
             bundle, 'outputs')
    assert B.check_hygiene(bundle) == []                      # sha updated: only replay can tell
    assert any('rubio_new.csv differs' in x for x in B.replay(bundle, release / 'corpus.csv'))


def test_a_stale_bundle_is_rejected(made):
    bundle, release = made
    assert any('latest release is now' in x for x in B.check_base(bundle, release, 'v2026.11.30'))
    (release / 'rubio_pending.csv').write_text('url,first_seen,attempts,last_error,source\n')
    assert any('rubio_pending.csv has changed' in x for x in B.check_base(bundle, release, TAG))
    (release / 'rubio_held.csv').write_text('url\n')
    assert any('rubio_held.csv' in x for x in B.check_base(bundle, release, TAG))


def test_a_different_corpus_is_rejected(made):
    bundle, release = made
    with open(release / 'corpus.csv', 'a', encoding='utf-8') as f:
        f.write('3,Rubio,2026-09-11,https://www.state.gov/x\n')
    assert any('corpus.csv is not' in x for x in B.check_base(bundle, release, TAG))


def test_commit_not_on_main_is_rejected(made):
    bundle, _ = made
    m = B.load_manifest(bundle)
    m['commit'] = '0' * 40
    (bundle / 'manifest.json').write_text(json.dumps(m))
    assert B.check_commit(bundle, 'HEAD')


def test_wrong_arguments_or_user_agent_are_rejected(made):
    bundle, _ = made
    m = B.load_manifest(bundle)
    m['extra_args'] = ['--rate-limit', '0.2']
    (bundle / 'manifest.json').write_text(json.dumps(m))
    assert any('--rate-limit' in x for x in B.check_args(bundle))
    m['extra_args'] = ['--rate-limit', '2']
    m['ci_args'] = m['ci_args'] + ['--full-rescan']
    (bundle / 'manifest.json').write_text(json.dumps(m))
    assert any('differ from the weekly' in x for x in B.check_args(bundle))
    m['ci_args'] = m['ci_args'][:-1]
    (bundle / 'manifest.json').write_text(json.dumps(m))
    meta_p = bundle / 'http' / 'meta.json'
    meta = json.loads(meta_p.read_text())
    meta['user_agent'] = 'Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0'
    meta_p.write_text(json.dumps(meta))
    assert any('User-Agent' in x for x in B.check_args(bundle))


def test_stray_files_and_local_paths_are_rejected(made):
    bundle, _ = made
    (bundle / 'notes.txt').write_text('hello')
    (bundle / 'outputs' / 'extra.csv').write_text('a\n')
    probs = B.check_hygiene(bundle)
    assert any('unexpected entries' in x for x in probs)
    assert any('unexpected files in outputs/' in x for x in probs)
    (bundle / 'notes.txt').unlink()
    (bundle / 'outputs' / 'extra.csv').unlink()
    p = bundle / 'outputs' / 'rubio_backfill_summary.json'
    _rewrite(p, p.read_text().replace('"mode"', f'"where": "{Path.home()}", "mode"'), bundle, 'outputs')
    assert any('local path' in x for x in B.check_hygiene(bundle))


def test_a_vanished_pending_url_is_caught(made):
    bundle, release = made
    p = bundle / 'inputs' / 'rubio_pending.csv'
    _rewrite(p, p.read_text() + 'https://www.state.gov/releases/office-of-the-spokesman/2026/09/'
                                'quietly-dropped/,2026-10-05,1,fetch_error: x,\n', bundle, 'inputs')
    assert any('dropped without' in x for x in B.check_forward(bundle, release / 'corpus.csv'))


def test_import_copies_outputs_and_reports(made, tmp_path, monkeypatch):
    bundle, release = made
    gh = tmp_path / 'gh_out'
    monkeypatch.setenv('GITHUB_OUTPUT', str(gh))
    monkeypatch.setattr(B, 'check_commit', lambda b, ref='origin/main': [])
    assert B.import_bundle(bundle, release, TAG) == 0
    assert (release / 'rubio_new.csv').read_bytes() == (bundle / 'outputs' / 'rubio_new.csv').read_bytes()
    kv = dict(l.split('=', 1) for l in gh.read_text().splitlines())
    assert kv['rows_written'] == '4' and kv['local_bundle'] == '2026-11-23'
    assert 'replay-verified' in kv['local_note']
    assert B.import_bundle(bundle, release, 'v2026.11.30') == 1   # stale now


# --- the driver ---------------------------------------------------------------

def test_once_a_week_guard(tmp_path):
    (tmp_path / '2026-11-20').mkdir()
    with pytest.raises(LF.Stop):
        LF.guard_once_a_week(date(2026, 11, 23), False, tmp_path)
    LF.guard_once_a_week(date(2026, 11, 23), True, tmp_path)          # --force
    LF.guard_once_a_week(date(2026, 11, 26), False, tmp_path)         # 6 days later
    with pytest.raises(LF.Stop):                                       # unmerged PR branch
        LF.guard_once_a_week(date(2026, 11, 30), False, tmp_path, ['local-fetch/2026-11-27'])


def test_a_refusal_commits_nothing(tmp_path, monkeypatch, capsys):
    bundles = tmp_path / 'bundles'
    monkeypatch.setattr(B, 'BUNDLES', bundles)
    monkeypatch.setattr(LF, 'WORK', tmp_path / 'work')
    monkeypatch.setattr(LF, 'preflight', lambda today, force: 'abc123')
    release = tmp_path / 'work' / 'release'

    def stage(rel):
        rel.mkdir(parents=True, exist_ok=True)
        (rel / 'corpus.csv').write_text('Row Id,Secretary,Date,URL\n')
        return TAG
    monkeypatch.setattr(LF, 'stage_release', stage)

    def refused(run_dir, mode, extra, log=None):
        (run_dir / 'rubio_backfill_summary.json').write_text(json.dumps(
            {'access_refused': True, 'refused_status': 405,
             'refused_url': 'https://www.state.gov/remarks-secretary-rubio/'}))
        return 3
    monkeypatch.setattr(B, 'run_backfill', refused)
    calls = []
    monkeypatch.setattr(LF, 'commit_and_push', lambda *a, **k: calls.append(a))
    assert LF.main([]) == LF.EXIT_REFUSED
    assert calls == [] and not any(bundles.glob('*'))                 # nothing kept or committed
    assert 'refused the crawler (405' in capsys.readouterr().out
    assert release.exists()


def test_rate_limit_below_one_second_is_refused():
    with pytest.raises(SystemExit):
        LF.main(['--rate-limit', '0.5'])


def test_a_different_parsing_environment_is_rejected(made):
    bundle, _ = made
    m = B.load_manifest(bundle)
    m['env']['packages']['beautifulsoup4'] = '4.12.0'
    (bundle / 'manifest.json').write_text(json.dumps(m))
    assert any('parsing environment differs' in x for x in B.check_env(bundle))


def test_env_problems_names_the_mismatch():
    fp = {'python': '3.12', 'packages': {'beautifulsoup4': '4.15.0', 'lxml': '5.3.0'}}
    probs = B.env_problems(fp)
    assert any('Python 3.12' in p for p in probs) and any(p.startswith('lxml 5.3.0') for p in probs)
