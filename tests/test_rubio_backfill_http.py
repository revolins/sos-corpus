"""robots.txt, retry policy, the shared CI arguments, and record -> replay
(rubio_backfill.py + http_record.py), all offline."""
from __future__ import annotations

import io
import json
import shutil
import sys
from contextlib import redirect_stderr

import pytest
import requests

from test_rubio_backfill_pipeline import (  # noqa: F401 -- _offline is an autouse fixture
    IDX, SPOKES, FakeResp, FakeSession, _e2e_routes, _e2e_setup, _offline, _rows, rb)

ROBOTS = rb._ROBOTS_URL
ITEM = f'{SPOKES}/2026/10/remarks-new/'


def _main(monkeypatch, session, corpus, out, excl, supp, *extra):
    monkeypatch.setattr(rb.requests, 'Session', lambda: session)
    monkeypatch.setattr(sys, 'argv', [
        'rubio_backfill.py', '--enriched', str(corpus), '--output', str(out / 'rubio_new.csv'),
        '--exclusions', str(excl), '--urls', str(supp), '--rate-limit', '0', *extra])
    with redirect_stderr(io.StringIO()):
        rb.main()


# --- robots.txt --------------------------------------------------------------

def test_robots_disallowed_item_is_never_requested_and_leaves_pending(tmp_path, monkeypatch):
    corpus, out, excl, supp = _e2e_setup(tmp_path)
    routes = _e2e_routes(tmp_path)
    routes[ROBOTS] = ('User-agent: sos-corpus-research\n'
                      'Disallow: /releases/office-of-the-spokesman/2026/10/remarks-new/\n'
                      'Disallow: /releases/office-of-the-spokesman/2026/09/pending-from-last-week/\n')
    s = FakeSession(routes)
    _main(monkeypatch, s, corpus, out, excl, supp)
    assert ITEM not in s.calls and not any('pending-from-last-week' in c for c in s.calls)
    skipped = {(r['url'], r['reason']) for r in _rows(out / 'rubio_skipped.csv')}
    assert (ITEM, 'robots_disallowed') in skipped
    assert not any('pending-from-last-week' in p['url'] for p in _rows(out / 'rubio_pending.csv'))
    assert ITEM not in {r['url'] for r in _rows(out / 'rubio_new.csv')}


def test_robots_disallowed_index_stops_the_run(tmp_path, monkeypatch):
    corpus, out, excl, supp = _e2e_setup(tmp_path)
    routes = _e2e_routes(tmp_path)
    routes[ROBOTS] = 'User-agent: *\nDisallow: /remarks-secretary-rubio/\n'
    s = FakeSession(routes)
    with pytest.raises(SystemExit) as ei:
        _main(monkeypatch, s, corpus, out, excl, supp)
    assert ei.value.code == rb.EXIT_ACCESS_REFUSED and s.calls == [ROBOTS]
    summ = json.loads((out / 'rubio_backfill_summary.json').read_text())
    assert summ['refused_status'] == 'robots.txt'


def test_robots_crawl_delay_raises_the_rate_limit(tmp_path, monkeypatch):
    corpus, out, excl, supp = _e2e_setup(tmp_path)
    routes = _e2e_routes(tmp_path)
    routes[ROBOTS] = 'User-agent: *\nCrawl-delay: 7\n'
    slept = []
    monkeypatch.setattr(rb.time, 'sleep', slept.append)
    _main(monkeypatch, FakeSession(routes), corpus, out, excl, supp)
    rate = [x for x in slept if x]                 # 0.0s are retry backoffs (patched to 0)
    assert len(rate) >= 5 and set(rate) == {7.0}


def test_robots_refused_is_a_refusal(tmp_path, monkeypatch):
    corpus, out, excl, supp = _e2e_setup(tmp_path)
    s = FakeSession({ROBOTS: FakeResp(ROBOTS, 403, 'Exception: forbidden')})
    with pytest.raises(SystemExit) as ei:
        _main(monkeypatch, s, corpus, out, excl, supp)
    assert ei.value.code == rb.EXIT_ACCESS_REFUSED and s.calls == [ROBOTS]


def test_robots_unreachable_crawls_nothing(tmp_path, monkeypatch):
    corpus, out, excl, supp = _e2e_setup(tmp_path)
    s = FakeSession({ROBOTS: FakeResp(ROBOTS, 503, 'busy')})
    with pytest.raises(SystemExit) as ei:
        _main(monkeypatch, s, corpus, out, excl, supp)
    assert 'robots.txt' in str(ei.value.code) and set(s.calls) == {ROBOTS}
    assert len(s.calls) == rb._FETCH_ATTEMPTS                       # 5xx: retried


def test_robots_missing_means_no_rules(tmp_path, monkeypatch):
    corpus, out, excl, supp = _e2e_setup(tmp_path)
    s = FakeSession(_e2e_routes(tmp_path))                          # robots.txt -> 404
    _main(monkeypatch, s, corpus, out, excl, supp)
    assert s.calls.count(ROBOTS) == 1 and ITEM in s.calls


def test_permanent_4xx_is_not_retried_but_429_is():
    s = FakeSession({ITEM: FakeResp(ITEM, 404, 'gone')})
    with pytest.raises(requests.HTTPError):
        rb._get_with_retry(s, ITEM)
    assert s.calls == [ITEM]
    s = FakeSession({ITEM: FakeResp(ITEM, 429, 'slow down')})
    with pytest.raises(requests.HTTPError):
        rb._get_with_retry(s, ITEM)
    assert s.calls == [ITEM] * rb._FETCH_ATTEMPTS


# --- the shared CI arguments -------------------------------------------------

def test_ci_args(tmp_path):
    d = tmp_path / 'data'
    d.mkdir()
    base = ['--enriched', f'{d}/corpus.csv', '--output', f'{d}/rubio_new.csv',
            '--pending', f'{d}/rubio_pending.csv',
            '--discovery-state', f'{d}/rubio_discovery_state.json',
            '--exclusions', f'{d}/rubio_coverage_exclusions.csv',
            '--urls', f'{d}/rubio_supplemental_urls.txt']
    assert rb.ci_args(d) == base
    (d / 'rubio_held.csv').write_text('url\n')
    (d / 'rubio_gt_missing.txt').write_text('')
    assert rb.ci_args(d, 'supplemental') == base + [
        '--urls', f'{d}/rubio_held.csv', '--urls', f'{d}/rubio_gt_missing.txt',
        '--supplemental-only']
    assert rb.ci_args(d, 'full_rescan')[-1] == '--full-rescan'
    with pytest.raises(ValueError):
        rb.ci_args(d, 'everything')


def test_workflow_uses_ci_args():
    wf = (rb.Path(rb.__file__).resolve().parent.parent / '.github' / 'workflows'
          / 'rubio-update.yml').read_text(encoding='utf-8')
    assert '--print-ci-args' in wf


# --- record -> replay ----------------------------------------------------------

_OUTPUTS = ('rubio_new.csv', 'rubio_pending.csv', 'rubio_discovery_state.json',
            'rubio_skipped.csv', 'rubio_listing_snapshot.csv', 'rubio_backfill_summary.json')


def _record_and_replay(tmp_path, monkeypatch, routes):
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir(), b.mkdir()
    corpus, out, excl, supp = _e2e_setup(a)
    _, out_b, _, _ = _e2e_setup(b)                    # same starting state, elsewhere
    rec = tmp_path / 'http'
    _main(monkeypatch, FakeSession(routes), corpus, out, excl, supp, '--record', str(rec))

    def no_network():
        raise AssertionError('replay opened a network session')
    monkeypatch.setattr(rb.requests, 'Session', no_network)
    monkeypatch.setattr(sys, 'argv', [
        'rubio_backfill.py', '--enriched', str(corpus), '--output', str(out_b / 'rubio_new.csv'),
        '--exclusions', str(excl), '--urls', str(supp), '--rate-limit', '0',
        '--replay', str(rec)])
    return out, out_b, rec


def test_replay_reproduces_the_recorded_run_exactly(tmp_path, monkeypatch):
    routes = _e2e_routes(tmp_path)
    out, out_b, rec = _record_and_replay(tmp_path, monkeypatch, routes)
    meta = json.loads((rec / 'meta.json').read_text())
    assert meta['completed'] is True and meta['user_agent'] == rb._HEADERS['User-Agent']
    assert meta['requests_total'] == sum(meta['requests_by_status'].values())
    with redirect_stderr(io.StringIO()):
        rb.main()
    for name in _OUTPUTS:
        assert (out / name).read_bytes() == (out_b / name).read_bytes(), name
    assert len(_rows(out_b / 'rubio_new.csv')) == 4


def test_replay_of_an_edited_recording_fails(tmp_path, monkeypatch):
    out, out_b, rec = _record_and_replay(tmp_path, monkeypatch, _e2e_routes(tmp_path))
    log = (rec / 'requests.jsonl').read_text().splitlines()
    extra = json.loads(log[-1])
    extra.update(seq=len(log) + 1, url=f'{SPOKES}/2026/10/never-requested/')
    (rec / 'requests.jsonl').write_text('\n'.join(log + [json.dumps(extra)]) + '\n')
    with pytest.raises(SystemExit) as ei, redirect_stderr(io.StringIO()):
        rb.main()
    assert ei.value.code == rb.EXIT_REPLAY_MISMATCH


def test_replay_missing_a_request_fails_loudly(tmp_path, monkeypatch):
    out, out_b, rec = _record_and_replay(tmp_path, monkeypatch, _e2e_routes(tmp_path))
    log = [json.loads(x) for x in (rec / 'requests.jsonl').read_text().splitlines()]
    kept = [e for e in log if e['url'] != ITEM]
    (rec / 'requests.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in kept))
    from http_record import ReplayMiss
    with pytest.raises(ReplayMiss), redirect_stderr(io.StringIO()):
        rb.main()


def test_recorded_refusal_replays_as_a_refusal(tmp_path, monkeypatch):
    routes = _e2e_routes(tmp_path)
    routes[ITEM] = FakeResp(ITEM, 403, 'Exception: forbidden')
    a = tmp_path / 'a'
    a.mkdir()
    corpus, out, excl, supp = _e2e_setup(a)
    rec = tmp_path / 'http'
    with pytest.raises(SystemExit) as ei:
        _main(monkeypatch, FakeSession(routes), corpus, out, excl, supp, '--record', str(rec))
    assert ei.value.code == rb.EXIT_ACCESS_REFUSED
    assert json.loads((rec / 'meta.json').read_text())['requests_by_status']['403'] == 1
    shutil.rmtree(out)
    _, out, _, _ = _e2e_setup(a)
    monkeypatch.setattr(sys, 'argv', [
        'rubio_backfill.py', '--enriched', str(corpus), '--output', str(out / 'rubio_new.csv'),
        '--exclusions', str(excl), '--urls', str(supp), '--rate-limit', '0', '--replay', str(rec)])
    with pytest.raises(SystemExit) as ei, redirect_stderr(io.StringIO()):
        rb.main()
    assert ei.value.code == rb.EXIT_ACCESS_REFUSED
