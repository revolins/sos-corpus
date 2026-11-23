"""Offline pages: state.gov pages saved by hand in a browser, read by the unchanged
scraper (scripts/offline_pages.py, rubio_backfill --offline-urls/--offline-pages).

No test touches the network (the autouse _offline fixture of the pipeline tests).
"""
from __future__ import annotations

import csv
import io
import json
import sys
from contextlib import redirect_stderr
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / 'scripts'))
sys.path.insert(0, str(_HERE))

import offline_pages as op  # noqa: E402
from test_rubio_backfill_pipeline import (  # noqa: E402,F401 -- _offline is autouse
    RUBIO, SPOKES, _e2e_setup, _offline, _rows, page_html, rb)

REMARKS = f'{SPOKES}/2026/10/remarks-new'


def _saved(html: str, url: str = '', how: str = 'canonical') -> str:
    """A page as a browser saves it: the server's page plus the browser's own marks."""
    tag = {'canonical': f'<link rel="canonical" href="{url}">',
           'og': f'<meta property="og:url" content="{url}">', 'none': ''}[how]
    html = html.replace('<head>', f'<head>{tag}', 1)
    return (f'<!-- saved from url=(0040){url} -->\n' if how == 'none' and url else '') + html


def _remarks_page() -> str:
    return page_html('Remarks to the Press', 'Remarks', [RUBIO], ['Joint Base Andrews'],
                     'October 14, 2026',
                     ['SECRETARY RUBIO: Good afternoon. A few words before we take off today.',
                      'QUESTION: Mr. Secretary, will you meet the minister?',
                      'SECRETARY RUBIO: Yes, tomorrow morning, and we will talk about trade.'],
                     published='2026-10-14T10:00:00-04:00')


def test_index_reads_canonical_then_og_then_saved_from(tmp_path):
    (tmp_path / 'a.htm').write_text(_saved(_remarks_page(), REMARKS), encoding='utf-8')
    (tmp_path / 'b.html').write_text(_saved('<html><head></head></html>', f'{SPOKES}/2026/11/b', 'og'),
                                     encoding='utf-8')
    (tmp_path / 'c.htm').write_text(_saved('<html><head></head></html>', f'{SPOKES}/2026/11/c', 'none'),
                                    encoding='utf-8')
    (tmp_path / 'cert.htm').write_text('<html><head><title>Certificate</title></head></html>',
                                       encoding='utf-8')
    (tmp_path / 'other.htm').write_text(_saved('<html><head></head></html>', 'https://example.org/x'),
                                        encoding='utf-8')
    (tmp_path / 'a_files').mkdir()                      # the browser's assets folder
    (tmp_path / 'a_files' / 'inner.htm').write_text(_saved('<html><head></head></html>',
                                                           f'{SPOKES}/2026/11/inner'))
    (tmp_path / 'notes.txt').write_text(REMARKS)
    idx = op.index_pages(tmp_path)
    assert idx.urls == [REMARKS, f'{SPOKES}/2026/11/b', f'{SPOKES}/2026/11/c']
    assert sorted(n for n, _ in idx.skipped) == ['cert.htm', 'other.htm']


def test_duplicate_saves_must_agree(tmp_path):
    page = _saved(_remarks_page(), REMARKS)
    (tmp_path / 'a.htm').write_text(page, encoding='utf-8')
    (tmp_path / 'a (1).htm').write_text(page, encoding='utf-8')
    idx = op.index_pages(tmp_path)
    assert idx.urls == [REMARKS] and len(idx.skipped) == 1
    (tmp_path / 'a (1).htm').write_text(page.replace('Good afternoon', 'Good evening'),
                                        encoding='utf-8')
    with pytest.raises(ValueError, match='differ'):
        op.index_pages(tmp_path)


def test_session_serves_saved_pages_and_404s_the_rest(tmp_path):
    (tmp_path / 'a.htm').write_text(_saved(_remarks_page(), REMARKS), encoding='utf-8')
    s = op.OfflineSession(tmp_path)
    for asked in (REMARKS, REMARKS + '/', REMARKS + '/?utm_source=x'):
        r = s.get(asked)
        assert r.status_code == 200 and r.url == REMARKS and 'Good afternoon' in r.text
    miss = s.get('https://www.state.gov/robots.txt')
    assert miss.status_code == 404
    with pytest.raises(rb.requests.HTTPError):
        miss.raise_for_status()


def test_url_list_holds_urls_only(tmp_path):
    pages = tmp_path / 'pages'
    pages.mkdir()
    (pages / 'Remarks - United States Department of State.htm').write_text(
        _saved(_remarks_page(), REMARKS), encoding='utf-8')
    out = tmp_path / 'urls.txt'
    assert op.write_url_list(op.index_pages(pages), out) == 1
    text = out.read_text(encoding='utf-8')
    assert REMARKS in text and 'United States Department' not in text and str(tmp_path) not in text
    assert rb.read_url_list(out) == [REMARKS]


def _offline_main(monkeypatch, tmp_path, *extra, state=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    corpus, out, excl, _ = _e2e_setup(tmp_path)
    if state is not None:
        (out / rb._STATE_NAME).write_text(json.dumps(state), encoding='utf-8')
    pages = tmp_path / 'pages'
    pages.mkdir(exist_ok=True)
    (pages / 'remarks.htm').write_text(_saved(_remarks_page(), REMARKS), encoding='utf-8')
    urls = out / rb._OFFLINE_URLS_NAME
    op.write_url_list(op.index_pages(pages), urls)
    args = ['rubio_backfill.py', *rb.ci_args(out, 'offline')]
    args[args.index('--enriched') + 1] = str(corpus)
    args[args.index('--exclusions') + 1] = str(excl)
    monkeypatch.setattr(sys, 'argv', [*args, '--rate-limit', '0', *extra])
    with redirect_stderr(io.StringIO()):
        rb.main()
    return out, pages


def test_offline_run_reads_only_saved_pages_and_moves_nothing(tmp_path, monkeypatch):
    out, _ = _offline_main(monkeypatch, tmp_path, '--offline-pages', str(tmp_path / 'pages'),
                           '--record', str(tmp_path / 'http'),
                           state={'last_complete_floor': '2026-08-18'})
    rows = _rows(out / 'rubio_new.csv')
    assert [r['url'].rstrip('/') for r in rows] == [REMARKS]
    assert rows[0]['discovery_source'] == 'offline' and rows[0]['modality'] == 'spoken'
    # The pending page was not saved: left exactly as it was, not counted as a failure.
    pending = _rows(out / rb._PENDING_NAME)
    assert [(p['url'].rsplit('/', 2)[-2], p['attempts']) for p in pending] == [('pending-from-last-week', '1')]
    # Nothing discovered, so the floor cannot move; the summary says where rows came from.
    state = json.loads((out / rb._STATE_NAME).read_text(encoding='utf-8'))
    assert state['last_complete_floor'] == '2026-08-18'
    summary = json.loads((out / rb._SUMMARY_NAME).read_text(encoding='utf-8'))
    assert summary['source'] == 'offline_pages' and summary['discovery_complete'] is False
    # Recorded: one page read, no robots.txt or listing, and no crawler identity claimed.
    recorded = [json.loads(l)['url'] for l in (tmp_path / 'http' / 'requests.jsonl').open()]
    assert recorded == [REMARKS]
    meta = json.loads((tmp_path / 'http' / 'meta.json').read_text(encoding='utf-8'))
    assert meta['source'] == 'offline_pages' and meta['user_agent'] is None


def test_offline_recording_replays_identically(tmp_path, monkeypatch):
    out, _ = _offline_main(monkeypatch, tmp_path, '--offline-pages', str(tmp_path / 'pages'),
                           '--record', str(tmp_path / 'http'))
    first = (out / 'rubio_new.csv').read_bytes()
    out2, _ = _offline_main(monkeypatch, tmp_path / 'again', '--replay', str(tmp_path / 'http'))
    assert (out2 / 'rubio_new.csv').read_bytes() == first


def test_offline_urls_never_reach_the_network(tmp_path, monkeypatch):
    corpus, out, excl, _ = _e2e_setup(tmp_path)
    urls = out / 'u.txt'
    urls.write_text(REMARKS + '\n')
    for extra, msg in ((['--offline-urls', str(urls)], 'needs --offline-pages or --replay'),
                       (['--offline-pages', str(tmp_path)], 'needs --offline-urls'),
                       (['--offline-urls', str(urls), '--offline-pages', str(tmp_path),
                         '--replay', str(tmp_path)], 'exclusive')):
        monkeypatch.setattr(sys, 'argv', ['rubio_backfill.py', '--enriched', str(corpus), *extra])
        err = io.StringIO()
        with redirect_stderr(err), pytest.raises(SystemExit):
            rb.main()
        assert msg in err.getvalue()


def test_ci_args_offline_mode(tmp_path):
    a = rb.ci_args(tmp_path, 'offline')
    assert a[a.index('--offline-urls') + 1] == str(tmp_path / rb._OFFLINE_URLS_NAME)
    assert '--supplemental-only' in a and '--urls' not in a and '--offline-pages' not in a
