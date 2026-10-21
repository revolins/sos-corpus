"""Offline tests for rubio_backfill.py discovery, fetch, classification and
run-to-run state (plan steps 1, 3, 4, 5; audit C-01..C-03, D-01..D-07, F-01/F-02).

No test touches the network: every HTTP call goes through FakeSession, and an
autouse fixture makes any real requests call fail loudly.
"""
from __future__ import annotations

import csv
import io
import json
import sys
from contextlib import redirect_stderr
from datetime import date
from pathlib import Path

import pytest
import requests

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / 'scripts'))

import rubio_backfill as rb  # noqa: E402

FIX = _HERE / 'fixtures' / 'backfill'
WWW = 'https://www.state.gov'
SPOKES = f'{WWW}/releases/office-of-the-spokesman'
RUBIO = 'Marco Rubio, Secretary of State'

csv.field_size_limit(10 ** 9)


# ---------------------------------------------------------------------------
# Offline HTTP
# ---------------------------------------------------------------------------

class FakeResp:
    def __init__(self, url, status=200, text='', final_url=None):
        self.status_code = status
        self.text = text
        self.url = final_url or url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} Error for url: {self.url}',
                                     response=self)


class FakeSession:
    """routes: url -> str (200 body) | FakeResp | Exception | callable(url, n) -> any."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None, **kw):
        self.calls.append(url)
        r = self.routes.get(url)
        if callable(r):
            r = r(url, self.calls.count(url))
        if r is None:
            return FakeResp(url, 404, 'not found')
        if isinstance(r, Exception):
            raise r
        if isinstance(r, FakeResp):
            if r.url == 'x':                 # placeholder: "the URL that was requested"
                return FakeResp(url, r.status_code, r.text)
            return r
        return FakeResp(url, 200, r)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def boom(*a, **k):
        raise AssertionError('network access attempted in an offline test')
    monkeypatch.setattr(requests.Session, 'get', boom)
    monkeypatch.setattr(requests, 'get', boom)
    monkeypatch.setattr(rb, '_FETCH_BACKOFF', 0.0)
    monkeypatch.delenv('GITHUB_OUTPUT', raising=False)
    monkeypatch.delenv('GITHUB_RUN_ID', raising=False)


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding='utf-8')


def page_html(title, typ, bylines, datelines, date_line, paras, published='') -> str:
    """A state.gov-shaped article page: nav, hide anchor, header block, body, Tags, footer."""
    ld = (f'<script type="application/ld+json">{{"@type": "Article", '
          f'"datePublished": "{published}"}}</script>' if published else '')
    body = ''.join(f'<p>{p}</p>' for p in [typ, *bylines, *datelines, date_line, *paras])
    return (f'<!doctype html><html><head><title>{title} - United States Department of State'
            f'</title>{ld}</head><body><header><a href="#main">Skip to content</a>'
            f'<nav><a href="/">Home</a></nav></header><main><button>hide</button>'
            f'<h1>{title}</h1>{body}<div class="related-tags"><h3>Tags</h3>'
            f'<a>Office of the Spokesperson</a></div></main><footer><a href="#top">Back to Top'
            f'</a><a>Contact Us</a></footer></body></html>')


def raw_to_html(raw: str, published: str) -> str:
    """Rebuild a page whose get_text('\\n') is `raw` (one <p> per line)."""
    import html as _h
    paras = ''.join(f'<p>{_h.escape(line)}</p>' for line in raw.split('\n'))
    return (f'<html><head><title>x</title><script type="application/ld+json">'
            f'{{"datePublished": "{published}"}}</script></head><body>{paras}</body></html>')


# ---------------------------------------------------------------------------
# D-07: query strings are stripped, not rejected
# ---------------------------------------------------------------------------

def test_is_excluded_strips_query_instead_of_rejecting():
    u = f'{SPOKES}/2026/01/some-statement/?utm_source=homepage&utm_medium=news_bar'
    assert not rb._is_excluded(u)
    assert rb._strip_query(u) == f'{SPOKES}/2026/01/some-statement/'
    assert rb._is_excluded(f'{WWW}/translations/spanish/2026/05/x/')
    assert rb._is_excluded(f'{WWW}/wp-content/uploads/x.pdf')
    assert rb._is_excluded(f'{WWW}/x.pdf?download=1')


# ---------------------------------------------------------------------------
# D-01/D-02: release sitemaps, unfiltered by 'rubio'
# ---------------------------------------------------------------------------

def _sitemap_routes():
    return {f'{WWW}/{n}': fx(n) for n in (
        'sitemap_index.xml', 'page-sitemap.xml', 'state_press_release-sitemap1.xml',
        'state_briefing-sitemap.xml', 'state_ext_content-sitemap.xml')}


def test_sitemaps_read_release_children_without_rubio_filter():
    s = FakeSession(_sitemap_routes())
    log = rb.SkipLog('t')
    stats: dict = {}
    got = rb.discover_sitemap_urls(s, 0, date(2025, 12, 1), skiplog=log, stats=stats)
    urls = {c.url: c.source for c in got}
    # No 'rubio' in the slug, still a candidate (the post-fetch gate decides).
    assert urls[f'{SPOKES}/2026/09/togo-independence-day/'] == 'sitemap_release'
    assert f'{SPOKES}/2025/12/secretary-of-state-marco-rubio-remarks-to-press-at-donald-j-trump-institute-of-peace/' in urls
    # Readout slugs are no longer blacklisted on release sitemaps.
    assert f'{SPOKES}/2026/05/secretary-rubios-call-with-foreign-minister-example/' in urls
    # Query string stripped (D-07), not dropped.
    who = [u for u in urls if 'world-health-organization' in u]
    assert who and '?' not in who[0]
    # URL month below the floor month: out, whatever its lastmod.
    assert f'{SPOKES}/2025/11/an-old-release/' not in urls
    # Undated URLs fall back to lastmod.
    assert f'{WWW}/external-content/statement-from-secretary-of-state-marco-rubio-2/' in urls
    assert f'{WWW}/external-content/an-old-external-item/' not in urls
    assert f'{WWW}/briefings/department-press-briefing-may-19-2026/' in urls
    # page-sitemaps keep the old 'rubio' + blacklist filter.
    assert urls[f'{WWW}/secretary-of-state-marco-rubio-remarks-to-press-at-the-miami-homestead-airport/'] == 'sitemap_page'
    assert f'{WWW}/some-other-page/' not in urls
    assert f'{WWW}/secretary-rubios-call-with-foo/' not in urls
    # A release child last modified before the floor is never fetched; others never read.
    assert f'{WWW}/state_press_release-sitemap2.xml' not in s.calls
    assert f'{WWW}/post_tag-sitemap.xml' not in s.calls
    assert stats['sitemaps_old'] == 1 and stats['sitemaps_failed'] == []
    # /translations/ is excluded AND recorded (C-03).
    assert [r['reason'] for r in log.rows] == ['excluded_path']
    assert '/translations/' in log.rows[0]['url']


def test_sitemap_failures_are_recorded_not_fatal():
    routes = _sitemap_routes()
    routes[f'{WWW}/state_briefing-sitemap.xml'] = FakeResp('x', 503, 'busy')
    stats: dict = {}
    got = rb.discover_sitemap_urls(FakeSession(routes), 0, date(2025, 12, 1), stats=stats)
    assert stats['sitemaps_failed'] == [f'{WWW}/state_briefing-sitemap.xml']
    assert any('togo' in c.url for c in got)


# ---------------------------------------------------------------------------
# D-03/D-05/D-06: verifiable archive walk
# ---------------------------------------------------------------------------

IDX = rb._REMARKS_INDEX


def _walk_routes():
    return {
        IDX: fx('remarks_p1.html'),
        f'{IDX}?cb=run1': fx('remarks_p1_cb.html'),
        f'{IDX}page/2/': fx('remarks_p2.html'),
        f'{IDX}page/2/?cb=run1': fx('remarks_p2.html'),
        f'{IDX}page/3/': fx('remarks_p3.html'),
        f'{IDX}page/3/?cb=run1': fx('remarks_p3.html'),
    }


def test_parse_listing_reads_total_pages_header_type_and_date():
    p = rb.parse_remarks_listing(fx('remarks_p1.html'))
    assert p['total_pages'] == 3 and p['max_link_page'] == 1
    assert p['items_count'] == 8 and '8 Items' in p['items_header']
    assert p['items'][0] == {'href': f'{SPOKES}/2026/10/item-a/', 'title': 'Item A',
                             'type': 'Remarks', 'list_date': 'October 6, 2026'}


def test_walk_rewalks_duplicate_slot_with_cache_buster_and_recovers_item():
    """An item inserted at the top mid-walk shifts item-c onto page 2 (a duplicate
    slot) and item-new is never seen; the cache-busted re-walk recovers it."""
    s = FakeSession(_walk_routes())
    snap: list = []
    stats: dict = {}
    got = rb.discover_remarks_urls(s, 0, date(2026, 1, 1), snapshot=snap, stats=stats,
                                   run_id='run1')
    urls = [c.url for c in got]
    assert f'{SPOKES}/2026/10/item-new/' in urls
    assert len(urls) == len(set(urls)) == 9
    assert stats['listed'] == 9 and stats['dup_slots'] == 1 and stats['dup_slots_after'] == 0
    assert stats['max_page'] == 3 and stats['rewalked_pages'] == [1, 2, 3]
    assert stats['complete'] is True and stats['unique'] == 9
    assert {r['pass'] for r in snap} == {1, 2} and len(snap) == 18
    # Snapshot list dates are ISO (Check AH windows and slug+date-matches them).
    import re as _re
    assert all(_re.fullmatch(r'\d{4}-\d{2}-\d{2}', r['list_date']) for r in snap), snap[:2]
    assert set(rb._SNAPSHOT_COLUMNS) <= set(snap[0])
    # Listing dates ride along for corpus_rubio_update's Date fallback.
    assert dict((c.url, c.listing_date) for c in got)[f'{SPOKES}/2026/10/item-new/'] == '2026-10-08'


def test_walk_page1_without_links_is_fatal():
    s = FakeSession({IDX: '<html><body><main><p>Nothing here</p></main></body></html>'})
    with pytest.raises(rb.DiscoveryError, match='no .collection-result'):
        rb.discover_remarks_urls(s, 0, date(2026, 1, 1))


def test_walk_full_page_without_pagination_is_fatal():
    items = ''.join(f'<li class="collection-result"><a href="{SPOKES}/2026/10/i{i}/">I{i}</a></li>'
                    for i in range(10))
    s = FakeSession({IDX: f'<html><body><ul>{items}</ul></body></html>'})
    with pytest.raises(rb.DiscoveryError, match='no pagination'):
        rb.discover_remarks_urls(s, 0, date(2026, 1, 1))


def _listing(items, total):
    li = ''.join(f'<li class="collection-result"><a href="{SPOKES}/{ym}/{slug}/">{slug}</a>'
                 f'<span>Remarks</span></li>' for slug, ym in items)
    return f'<html><body><ul>{li}</ul><input name="total_pages" value="{total}"></body></html>'


def test_walk_unreadable_in_window_page_is_fatal():
    s = FakeSession({IDX: _listing([('a', '2026/10'), ('b', '2026/10')], 3),
                     f'{IDX}page/2/': FakeResp('x', 504, 'timeout'),
                     f'{IDX}page/3/': _listing([('c', '2026/09')], 3)})
    with pytest.raises(rb.DiscoveryError, match='inside the 2026-09 window'):
        rb.discover_remarks_urls(s, 0, date(2026, 9, 1), run_id='r')
    # retried (3 attempts) and then once more with a cache-buster
    assert s.calls.count(f'{IDX}page/2/') == 3 and f'{IDX}page/2/?cb=r' in s.calls


def test_walk_unreadable_page_past_the_window_is_not_fatal_but_incomplete():
    s = FakeSession({IDX: _listing([('a', '2026/10'), ('b', '2026/10')], 4),
                     f'{IDX}page/2/': _listing([('c', '2026/04'), ('d', '2026/04')], 4),
                     f'{IDX}page/3/': FakeResp('x', 504, 'timeout'),
                     f'{IDX}page/4/': _listing([('e', '2026/03'), ('f', '2026/03')], 4)})
    stats: dict = {}
    got = rb.discover_remarks_urls(s, 0, date(2026, 9, 1), stats=stats)
    assert stats['stopped_at'] == 4 and stats['failed_pages'] == [3]
    assert stats['complete'] is False
    assert {c.url for c in got} == {f'{SPOKES}/2026/10/a/', f'{SPOKES}/2026/10/b/'}


def test_walk_keeps_old_links_not_in_corpus():
    """D-04: the month floor limits the walk, not membership: an old link the corpus
    lacks (late-listed, backdated) is still a candidate."""
    s = FakeSession({IDX: _listing([('new', '2026/10'), ('late-listed', '2026/02'),
                                    ('have-it', '2026/02')], 1)})
    got = rb.discover_remarks_urls(s, 0, date(2026, 9, 1),
                                   in_corpus=lambda u: 'have-it' in u)
    assert {c.url.rsplit('/', 2)[-2] for c in got} == {'new', 'late-listed'}
    # Without a corpus index the old month filter applies.
    got = rb.discover_remarks_urls(FakeSession(s.routes), 0, date(2026, 9, 1))
    assert {c.url.rsplit('/', 2)[-2] for c in got} == {'new'}


def test_candidate_dedup_folds_spokesperson_and_spokesman():
    a = rb.Candidate(f'{WWW}/releases/office-of-the-spokesman/2026/05/x/', 'supplemental')
    b = rb.Candidate(f'{WWW}/releases/office-of-the-spokesperson/2026/05/x/', 'supplemental')
    c = rb.Candidate(f'{WWW}/releases/office-of-the-spokesman/2026/05/x/?utm=1', 'sitemap_release')
    assert rb._dedup_candidates([a, b, c]) == [a]


# ---------------------------------------------------------------------------
# D-04: discovery floor
# ---------------------------------------------------------------------------

def _args(**kw):
    import argparse
    base = dict(full_rescan=False, since=None, since_buffer_days=60)
    base.update(kw)
    return argparse.Namespace(**base)


def _index(max_date):
    return rb.CorpusIndex({'x'}, set(), set(), 1, max_date)


def test_floor_is_min_of_last_complete_floor_and_buffer():
    idx = _index(date(2026, 10, 17))
    assert rb.resolve_since(_args(), idx, {}) == date(2026, 8, 18)
    assert rb.resolve_since(_args(), idx, {'last_complete_floor': '2026-07-01'}) == date(2026, 7, 1)
    assert rb.resolve_since(_args(), idx, {'last_complete_floor': '2026-09-30'}) == date(2026, 8, 18)
    assert rb.resolve_since(_args(full_rescan=True), idx, {'last_complete_floor': '2026-07-01'}) == rb.RUBIO_START
    assert rb.resolve_since(_args(since='2026-10-01'), idx, {'last_complete_floor': '2026-07-01'}) == date(2026, 10, 1)
    assert rb.resolve_since(_args(), _index(date(2025, 2, 1)), {}) == rb.RUBIO_START
    assert rb.resolve_since(_args(), idx, {'last_complete_floor': 'garbage'}) == date(2026, 8, 18)


def test_missing_state_file_warns(tmp_path):
    err = io.StringIO()
    with redirect_stderr(err):
        assert rb.load_discovery_state(tmp_path / 'nope.json') == {}
    assert 'no discovery state' in err.getvalue()


# ---------------------------------------------------------------------------
# F-01/F-02: fetch with retry; pending queue
# ---------------------------------------------------------------------------

def test_fetch_retries_then_succeeds():
    url = f'{SPOKES}/2026/10/a/'
    html = page_html('A', 'Press Statement', [RUBIO], [], 'October 1, 2026', ['Text.'])
    s = FakeSession({url: lambda u, n: FakeResp(u, 504, '') if n == 1 else FakeResp(u, 200, html)})
    with redirect_stderr(io.StringIO()):
        page = rb.fetch_rubio_page(url, s)
    assert page and page['final_url'] == url and s.calls == [url, url]
    assert '\nhide\n' in page['raw_text']


def test_fetch_failure_kinds():
    url = f'{SPOKES}/2026/10/gone/'
    s = FakeSession({})
    with redirect_stderr(io.StringIO()):
        assert rb.fetch_rubio_page(url, s) is None
        with pytest.raises(rb.FetchFailure) as ei:
            rb.fetch_rubio_page(url, s, raise_errors=True)
    assert ei.value.kind == 'fetch_error' and '404' in ei.value.detail
    # /external-content/ redirected to whitehouse.gov, which 403s (F-02).
    ext = f'{WWW}/external-content/statement-x'
    wh = 'https://www.whitehouse.gov/briefings-statements/2025/06/statement-x/'
    s = FakeSession({ext: FakeResp(ext, 403, 'denied', final_url=wh)})
    with redirect_stderr(io.StringIO()), pytest.raises(rb.FetchFailure) as ei:
        rb.fetch_rubio_page(ext, s, raise_errors=True, attempts=1)
    assert ei.value.kind == 'external_redirect' and ei.value.final_url == wh
    # A successful off-host redirect is parsed, with the final URL recorded.
    s = FakeSession({ext: FakeResp(ext, 200, '<html><body><p>x</p></body></html>', final_url=wh)})
    with redirect_stderr(io.StringIO()):
        assert rb.fetch_rubio_page(ext, s)['final_url'] == wh


def test_pending_lifecycle(tmp_path):
    k = rb._live_key
    prev = {
        k('https://www.state.gov/a/'): {'url': 'https://www.state.gov/a/', 'first_seen': '2026-10-01', 'attempts': 2, 'last_error': 'x'},
        k('https://www.state.gov/b/'): {'url': 'https://www.state.gov/b/', 'first_seen': '2026-10-01', 'attempts': 1, 'last_error': 'x'},
        k('https://www.state.gov/c/'): {'url': 'https://www.state.gov/c/', 'first_seen': '2026-10-01', 'attempts': 5, 'last_error': 'x'},
        k('https://www.state.gov/d/'): {'url': 'https://www.state.gov/d/', 'first_seen': '2026-10-08', 'attempts': 1, 'last_error': 'x'},
    }
    new = rb.update_pending(
        prev, fetched_ok={k('https://www.state.gov/b/')}, resolved={k('https://www.state.gov/c/')},
        failures={k('https://www.state.gov/a/'): ('https://www.state.gov/a/', 'fetch_error: 504'),
                  k('https://www.state.gov/e/'): ('https://www.state.gov/e/', 'external_redirect: 403')},
        today='2026-10-21')
    by = {r['url'].rsplit('/', 2)[-2]: r for r in new.values()}
    assert set(by) == {'a', 'd', 'e'}                      # b fetched, c in corpus: gone
    assert by['a']['attempts'] == 3 and by['a']['first_seen'] == '2026-10-01'
    assert by['a']['last_error'] == 'fetch_error: 504'
    assert by['d']['attempts'] == 1                        # not attempted: unchanged
    assert by['e'] == {'url': 'https://www.state.gov/e/', 'first_seen': '2026-10-21',
                       'attempts': 1, 'last_error': 'external_redirect: 403'}
    assert [r['url'] for r in rb.stuck_entries(new)] == ['https://www.state.gov/a/']
    p = tmp_path / 'pending.csv'
    rb.write_pending(p, new)
    assert rb.read_pending(p) == new


# ---------------------------------------------------------------------------
# C-01/C-02: classification
# ---------------------------------------------------------------------------

def _classify(url, raw, title='x'):
    page = {'raw_text': raw, 'title': title}
    bm = rb._parse_block_metadata(raw)
    with redirect_stderr(io.StringIO()):
        ex = rb.extract_row(url, page)
    return rb.classify_rubio_page(url, page, bm, ex), bm, ex


def _raw(html):
    return rb._make_soup(html).get_text('\n', strip=True)


CUBA = f'{SPOKES}/2026/05/secretary-rubios-message-to-the-cuban-people-on-their-independence-day'


def test_cuban_video_message_is_kept_with_translated_transcript():
    raw = fx('cuban_17483_raw.txt')
    # The old gate dropped it (audit C-01).
    with redirect_stderr(io.StringIO()):
        ex = rb.extract_row(CUBA, {'raw_text': raw})
    assert ex['speech_text'].startswith('Today, Secretary of State Marco Rubio')
    d, bm, _ = _classify(CUBA, raw)
    assert d.keep and d.reason == 'third_person_intro+marker:translated transcript'
    assert d.modality == 'video'
    assert d.speech_text.startswith('On a day like today, in 1902, the Cuban flag flew')
    assert 'Secretary of State Marco Rubio' not in d.speech_text[:300]
    assert 'courtesy translation' not in d.speech_text.lower()
    assert len(d.speech_text.split()) > 700
    assert bm['block_byline'] == RUBIO and bm['block_speech_type'] == 'Media Note'
    # Not a Speech Type of its own: the kept video message takes existing vocabulary.
    assert rb._resolve_speech_type({'title': 'x'}, bm, CUBA) == 'remarks'


def test_spokesperson_media_note_is_rejected_17461():
    url = (f'{WWW}/secretary-rubio-authorizes-visa-restrictions-on-colombian-government-'
           f'officials-and-their-immediate-family-members')
    d, bm, _ = _classify(url, fx('media_note_17461_raw.txt'))
    assert not d.keep and d.reason == 'spokesperson_byline'
    assert bm['block_bylines'] == ['Office of the Spokesperson']
    # The byline is no longer a Location (it used to become Washington, D.C. via it).
    assert 'block_location' not in bm
    assert rb._resolve_speech_type({'title': 'x'}, bm, url) != 'media note'


def test_byline_gate_and_modality():
    u = f'{SPOKES}/2026/10/x/'
    body = ['The United States today announced a decision of some importance to many partners.'] * 3
    # Rubio byline, written statement
    d, bm, _ = _classify(u, _raw(page_html('A', 'Press Statement', [RUBIO], [], 'October 1, 2026', body)))
    assert d.keep and d.reason == 'rubio_byline' and d.modality == 'written'
    # Department Spokesperson byline: third-person release, skipped
    d, _, _ = _classify(u, _raw(page_html('B', 'Press Statement', ['Tammy Bruce, Department Spokesperson'],
                                          [], 'October 1, 2026', body)))
    assert not d.keep and d.reason == 'spokesperson_byline'
    # Another principal only
    d, _, _ = _classify(u, _raw(page_html('C', 'Press Statement', ['Christopher Landau, Deputy Secretary of State'],
                                          [], 'October 1, 2026', body)))
    assert not d.keep and d.reason == 'other_byline'
    # Joint statement he co-signs: kept, Modality joint; the co-signer is not a Location
    raw = _raw(page_html('Joint Statement by Secretary of Health and Human Services Robert F. Kennedy and '
                         'Secretary of State Marco Rubio on X', 'Press Statement',
                         [RUBIO, 'Robert F. Kennedy, Secretary of Health and Human Services'], [],
                         'January 22, 2026', body))
    d, bm, _ = _classify(u, raw)
    assert d.keep and d.modality == 'joint' and 'block_location' not in bm
    assert bm['block_byline'] == f'{RUBIO} | Robert F. Kennedy, Secretary of Health and Human Services'
    # Spoken transcript with labels and a venue dateline
    raw = _raw(page_html('Remarks', 'Remarks', [RUBIO], ['Benjamin Franklin Room', 'Washington, D.C.'],
                         'October 1, 2026', ['SECRETARY RUBIO: Thank you all for being here today.',
                                             'QUESTION: Mr. Secretary, a question.',
                                             'SECRETARY RUBIO: And the answer is yes.']))
    d, bm, ex = _classify(u, raw)
    assert d.keep and d.modality == 'spoken' and 'rubio_turns' in ex['extraction_mode']
    assert bm['block_location'] == 'Washington, D.C.'
    # No byline at all (legacy / external layout): kept, the gate needs positive evidence
    assert rb._byline_decision({}, 'x', 0) == (True, 'no_byline', False)


def test_third_person_and_pattern_needs_a_readout_verb():
    u = f'{SPOKES}/2026/10/x/'
    assert rb._active_speaker_decision(
        u, 'Secretary Rubio and Foreign Minister Example met today in Washington.')[:2] == (False, 'readout')
    assert rb._active_speaker_decision(
        u, 'Secretary Rubio and the people of Example share a long friendship.')[0]
    assert not rb._is_active_speaker(u, 'The below is attributable to Spokesperson Tammy Bruce: ...')
    assert not rb._is_active_speaker(f'{WWW}/remarks-secretary-rubio/', 'anything')
    # Third-person intro followed by an "as prepared" marker: his text follows.
    keep, why, after = rb._active_speaker_decision(
        u, 'Secretary Rubio will deliver remarks.\nAs Prepared for Delivery\nGood morning, everyone.')
    assert keep and why == 'third_person_intro+marker:as prepared for delivery'
    assert after == 'Good morning, everyone.'
    # ... or by Rubio labels
    assert rb._active_speaker_decision(u, 'Secretary Rubio met the press.', '', 2)[0]


def test_exclusions_match_both_path_spellings(tmp_path):
    p = tmp_path / 'excl.csv'
    p.write_text('url_key,slug_date_key,reason,decided_by,date\n'
                 'state.gov/releases/office-of-the-spokesman/2026/10/secretary-rubios-meeting-with-x,,readout,t,2026-10-21\n'
                 ',media-note-slug|2025-01-26,media note,t,2026-10-21\n', encoding='utf-8')
    ex = rb.load_exclusions(p)
    assert rb._live_key(f'{WWW}/releases/office-of-the-spokesperson/2026/10/secretary-rubios-meeting-with-x/') in ex.url_keys
    from corpus_keys import slug_date_key
    assert slug_date_key(f'{WWW}/media-note-slug', '2025-01-26') in ex.slug_dates


def test_seed_files_parse():
    repo = _HERE.parent / 'data'
    urls = rb.read_url_list(repo / 'rubio_supplemental_urls.txt')
    assert len(urls) == 8 and all(u.startswith('https://www.state.gov/') for u in urls)
    assert any('cuban-people-on-their-independence-day' in u for u in urls)
    # The audit-documented Homestead URL (D-01) is seeded, not commented out.
    assert any('/releases/office-of-the-spokesperson/secretary-of-state-marco-rubio-remarks-to-press-'
               'at-the-miami-homestead-airport/' in u for u in urls)
    # the two IOP and the two Cuban spellings each collapse to one fetch; the two
    # Homestead spellings are both fetched (folded after the fetch by slug + Date)
    assert len(rb._dedup_candidates([rb.Candidate(u, 'supplemental') for u in urls])) == 6
    from corpus_keys import slug_date_key
    hs = [u for u in urls if 'miami-homestead' in u]
    assert len(hs) == 2 and slug_date_key(hs[0], '2026-05-21') == slug_date_key(hs[1], '2026-05-21')
    ex = rb.load_exclusions(repo / 'rubio_coverage_exclusions.csv')
    assert len(ex.url_keys) == 6 and len(ex.slug_dates) == 2
    assert rb._live_key(f'{WWW}/remarks-secretary-rubio/') in ex.url_keys


# ---------------------------------------------------------------------------
# End to end: run() with every source, sidecar and state file (C-03, F-01, D-04)
# ---------------------------------------------------------------------------

def _e2e_routes(tmp_path):
    body = ['We will keep working with our partners on the matters that count most.'] * 3
    listing = _listing([('remarks-new', '2026/10'), ('already-have', '2026/09'),
                        ('media-note-about-him', '2026/10'),
                        ('secretary-rubios-meeting-with-icelandic-prime-minister-frostadottir', '2026/10')], 1)
    # The Cuban message: old (2026/05) and listed, not in the corpus -> fetched (D-04).
    listing = listing.replace('</ul>', f'<li class="collection-result"><a href="{CUBA}/">Cuba</a>'
                                       f'<span>Media Note</span><span>May 20, 2026</span></li></ul>')
    who = f'{SPOKES}/2026/10/termination-who-joint-statement-by-secretary-of-state-rubio-and-kennedy/'
    sm_index = ('<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                f'<sitemap><loc>{WWW}/state_press_release-sitemap1.xml</loc><lastmod>2026-10-18</lastmod></sitemap>'
                '</sitemapindex>')
    sm = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
          f'<url><loc>{who}</loc></url>'
          f'<url><loc>{SPOKES}/2026/10/visa-restrictions-on-example-officials/</loc></url>'
          f'<url><loc>{SPOKES}/2026/10/a-page-that-503s/</loc></url></urlset>')
    return {
        IDX: listing,
        f'{WWW}/sitemap_index.xml': sm_index,
        f'{WWW}/state_press_release-sitemap1.xml': sm,
        f'{SPOKES}/2026/10/remarks-new/': page_html(
            'Remarks to the Press', 'Remarks', [RUBIO], ['Joint Base Andrews'], 'October 14, 2026',
            ['SECRETARY RUBIO: Good afternoon. A few words before we take off today.',
             'QUESTION: Mr. Secretary, will you meet the minister?',
             'SECRETARY RUBIO: Yes, tomorrow morning, and we will talk about trade.'],
            published='2026-10-14T10:00:00-04:00'),
        f'{SPOKES}/2026/10/media-note-about-him/': page_html(
            'Secretary Rubio Appoints an Envoy', 'Media Note', ['Office of the Spokesperson'], [],
            'October 13, 2026', ['Today, Secretary of State Marco Rubio appointed an envoy.'] + body,
            published='2026-10-13T10:00:00-04:00'),
        f'{CUBA}/': raw_to_html(fx('cuban_17483_raw.txt'), '2026-05-20T10:00:00-04:00'),
        who: page_html(
            'Termination of U.S. Membership in the WHO: Joint Statement by Secretary of State Rubio and '
            'Secretary of Health and Human Services Kennedy', 'Press Statement',
            [RUBIO, 'Robert F. Kennedy, Secretary of Health and Human Services'], [], 'January 22, 2026',
            body, published='2026-01-22T09:00:00-05:00'),
        f'{SPOKES}/2026/10/visa-restrictions-on-example-officials/': page_html(
            'Visa Restrictions', 'Press Statement', ['Tammy Bruce, Department Spokesperson'], [],
            'October 10, 2026', ['The Department of State is taking steps today.'] + body,
            published='2026-10-10T10:00:00-04:00'),
        f'{SPOKES}/2026/10/a-page-that-503s/': FakeResp('x', 503, 'busy'),
        f'{SPOKES}/2026/09/pending-from-last-week/': page_html(
            'Op-Ed', 'Op-Ed', [RUBIO], [], 'September 30, 2026', body,
            published='2026-09-30T10:00:00-04:00'),
    }


def _e2e_setup(tmp_path):
    corpus = tmp_path / 'corpus.csv'
    with open(corpus, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['Row Id', 'Secretary', 'Date', 'URL'])
        w.writeheader()
        w.writerow({'Row Id': 1, 'Secretary': 'Blinken', 'Date': '2024-12-01', 'URL': f'{WWW}/blinken-x'})
        w.writerow({'Row Id': 2, 'Secretary': 'Rubio', 'Date': '2026-09-10',
                    'URL': f'{WWW}/releases/office-of-the-spokesperson/2026/09/already-have'})
    out = tmp_path / 'out'
    out.mkdir()
    with open(out / 'rubio_pending.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=rb._PENDING_COLUMNS)
        w.writeheader()
        w.writerow({'url': f'{SPOKES}/2026/09/pending-from-last-week/', 'first_seen': '2026-10-05',
                    'attempts': 1, 'last_error': 'fetch_error: ReadTimeout'})
    excl = tmp_path / 'excl.csv'
    excl.write_text('url_key,slug_date_key,reason,decided_by,date\n'
                    'state.gov/releases/office-of-the-spokesman/2026/10/secretary-rubios-meeting-with-icelandic-'
                    'prime-minister-frostadottir,,readout,scope-2026-10-21,2026-10-21\n', encoding='utf-8')
    supp = tmp_path / 'supp.txt'
    supp.write_text('# already in the corpus under the old spelling: never fetched\n'
                    f'{SPOKES}/2026/09/already-have/\n', encoding='utf-8')
    return corpus, out, excl, supp


def _run_main(monkeypatch, session, corpus, out, excl, supp, gh_out):
    monkeypatch.setattr(rb.requests, 'Session', lambda: session)
    monkeypatch.setenv('GITHUB_OUTPUT', str(gh_out))
    monkeypatch.setattr(sys, 'argv', [
        'rubio_backfill.py', '--enriched', str(corpus), '--output', str(out / 'rubio_new.csv'),
        '--exclusions', str(excl), '--urls', str(supp), '--rate-limit', '0'])
    with redirect_stderr(io.StringIO()):
        rb.main()


def _rows(p):
    with open(p, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def test_run_end_to_end_offline(tmp_path, monkeypatch):
    corpus, out, excl, supp = _e2e_setup(tmp_path)
    routes = _e2e_routes(tmp_path)
    s = FakeSession(routes)
    gh = tmp_path / 'gh_out'
    _run_main(monkeypatch, s, corpus, out, excl, supp, gh)

    rows = {r['url'].rstrip('/').rsplit('/', 1)[-1]: r for r in _rows(out / 'rubio_new.csv')}
    assert set(rows) == {'remarks-new', 'secretary-rubios-message-to-the-cuban-people-on-their-independence-day',
                         'termination-who-joint-statement-by-secretary-of-state-rubio-and-kennedy',
                         'pending-from-last-week'}
    assert rows['remarks-new']['modality'] == 'spoken'
    assert rows['remarks-new']['discovery_source'] == 'archive'
    assert rows['remarks-new']['Location'] == 'Joint Base Andrews'
    cuba = rows['secretary-rubios-message-to-the-cuban-people-on-their-independence-day']
    assert cuba['modality'] == 'video' and cuba['speech_type'] == 'remarks'
    assert cuba['speech_text'].startswith('On a day like today')
    assert cuba['listing_date'] == '2026-05-20' and cuba['Date'] == '2026-05-20'
    assert cuba['extraction_mode'].endswith('+after_transcript_marker')
    who = rows['termination-who-joint-statement-by-secretary-of-state-rubio-and-kennedy']
    assert who['modality'] == 'joint' and who['discovery_source'] == 'sitemap_release'
    assert rows['pending-from-last-week']['discovery_source'] == 'pending'
    assert list(_rows(out / 'rubio_new.csv')[0]) == rb._FIELDNAMES

    # The corpus row (old spelling) and the excluded readout are never fetched.
    assert not any('already-have' in c for c in s.calls)
    assert not any('frostadottir' in c for c in s.calls)

    skipped = {(r['url'].rstrip('/').rsplit('/', 1)[-1], r['reason'], r['stage'])
               for r in _rows(out / 'rubio_skipped.csv')}
    assert ('media-note-about-him', 'spokesperson_byline', 'classify') in skipped
    assert ('visa-restrictions-on-example-officials', 'spokesperson_byline', 'classify') in skipped
    assert ('secretary-rubios-meeting-with-icelandic-prime-minister-frostadottir',
            'excluded_by_decision', 'prefetch') in skipped
    assert ('a-page-that-503s', 'fetch_error', 'fetch') in skipped
    mn = [r for r in _rows(out / 'rubio_skipped.csv') if r['reason'] == 'spokesperson_byline'][0]
    assert mn['block_type'] and mn['byline'] and mn['first_400'] and mn['run_id']

    snap = _rows(out / 'rubio_listing_snapshot.csv')
    assert len(snap) == 5 and snap[0]['total_pages'] == '1'

    pending = _rows(out / 'rubio_pending.csv')
    assert [(p['url'].rsplit('/', 2)[-2], p['attempts']) for p in pending] == [('a-page-that-503s', '1')]

    state = json.loads((out / 'rubio_discovery_state.json').read_text())
    assert state['last_complete_floor'] == '2026-07-12'          # 2026-09-10 - 60d
    assert state['last_run']['complete'] is True
    assert any('visa-restrictions' in k for k in state['sitemap_classified'])

    summ = json.loads((out / 'rubio_backfill_summary.json').read_text())
    assert summ['written'] == 4 and summ['failed'] == 1 and summ['listed'] == 5
    assert summ['skipped_by_reason']['spokesperson_byline'] == 2
    kv = dict(line.split('=', 1) for line in gh.read_text().split())
    assert kv['rows_written'] == '4' and kv['pending'] == '1' and kv['stuck'] == '0'
    assert not (out / 'rubio_stuck.csv').exists()

    # Second and third runs: the Spokesperson release is not re-fetched, the 503 page
    # keeps failing, and after its third failed run it is "stuck".
    for n in (2, 3):
        s2 = FakeSession(routes)
        _run_main(monkeypatch, s2, corpus, out, excl, supp, gh)
        assert not any('visa-restrictions' in c for c in s2.calls)
        assert _rows(out / 'rubio_pending.csv')[0]['attempts'] == str(n)
    stuck = _rows(out / 'rubio_stuck.csv')
    assert [r['url'].rsplit('/', 2)[-2] for r in stuck] == ['a-page-that-503s']


def test_run_discovery_error_writes_sidecars_and_keeps_state(tmp_path, monkeypatch):
    corpus, out, excl, supp = _e2e_setup(tmp_path)
    (out / 'rubio_discovery_state.json').write_text('{"last_complete_floor": "2026-06-01"}')
    s = FakeSession({IDX: '<html><body><p>maintenance</p></body></html>'})
    with pytest.raises(SystemExit) as ei:
        _run_main(monkeypatch, s, corpus, out, excl, supp, tmp_path / 'gh')
    assert 'discovery failed' in str(ei.value.code)
    assert (out / 'rubio_skipped.csv').exists() and (out / 'rubio_listing_snapshot.csv').exists()
    assert json.loads((out / 'rubio_discovery_state.json').read_text()) == {'last_complete_floor': '2026-06-01'}
    assert _rows(out / 'rubio_pending.csv')[0]['attempts'] == '1'      # untouched
    assert not (out / 'rubio_new.csv').exists()


def test_run_dry_run_leaves_state_alone(tmp_path, monkeypatch):
    corpus, out, excl, supp = _e2e_setup(tmp_path)
    s = FakeSession(_e2e_routes(tmp_path))
    monkeypatch.setattr(rb.requests, 'Session', lambda: s)
    monkeypatch.setattr(sys, 'argv', [
        'rubio_backfill.py', '--enriched', str(corpus), '--output', str(out / 'rubio_new.csv'),
        '--exclusions', str(excl), '--rate-limit', '0', '--dry-run'])
    with redirect_stderr(io.StringIO()):
        rb.main()
    assert not (out / 'rubio_discovery_state.json').exists()
    assert _rows(out / 'rubio_pending.csv')[0]['attempts'] == '1'
    assert (out / 'rubio_listing_snapshot.csv').exists()
    # nothing but listing/sitemap requests
    assert all(c == IDX or c.endswith('.xml') for c in s.calls)


def test_sitemap_fetch_cap_logs_what_it_drops(tmp_path, monkeypatch):
    corpus, out, excl, supp = _e2e_setup(tmp_path)
    s = FakeSession(_e2e_routes(tmp_path))
    monkeypatch.setattr(rb.requests, 'Session', lambda: s)
    monkeypatch.setattr(sys, 'argv', [
        'rubio_backfill.py', '--enriched', str(corpus), '--output', str(out / 'rubio_new.csv'),
        '--exclusions', str(excl), '--rate-limit', '0', '--sitemap-fetch-cap', '1'])
    with redirect_stderr(io.StringIO()):
        rb.main()
    capped = [r for r in _rows(out / 'rubio_skipped.csv') if r['reason'] == 'sitemap_fetch_cap']
    assert len(capped) == 2
    # 'rubio' slugs first: the WHO joint statement is the one fetched
    assert not any('rubio-and-kennedy' in r['url'] for r in capped)


# ---------------------------------------------------------------------------
# Review fixes
# ---------------------------------------------------------------------------

def test_missing_explicit_input_files_fail_loudly(tmp_path, monkeypatch):
    corpus, out, excl, supp = _e2e_setup(tmp_path)
    s = FakeSession(_e2e_routes(tmp_path))
    with pytest.raises(SystemExit) as ei:
        _run_main(monkeypatch, s, corpus, out, excl, tmp_path / 'no_such_urls.txt', tmp_path / 'gh')
    assert 'not found' in str(ei.value.code) and 'no_such_urls.txt' in str(ei.value.code)
    with pytest.raises(SystemExit) as ei:
        _run_main(monkeypatch, s, corpus, out, tmp_path / 'no_excl.csv', supp, tmp_path / 'gh')
    assert 'no_excl.csv' in str(ei.value.code)
    assert s.calls == []                       # failed before any request


def test_resolve_location_has_no_washington_default():
    assert rb._resolve_location({}, {}) == ''
    assert rb._resolve_location({}, {'block_location': ''}) == ''
    assert rb._resolve_location({'jsonld_location_name': 'Doha, Qatar'}, {}) == 'Doha, Qatar'


@pytest.mark.parametrize('intro', [
    "Today, Secretary of State Marco Rubio delivered a video message to the conference. "
    "Below is the transcript of the Secretary's message.",
    'Today, Secretary of State Marco Rubio delivered a video message. The text of his remarks follows.',
    'Today, Secretary of State Marco Rubio delivered a video message. The full text is below.',
    'Secretary Rubio spoke to the conference by video. The transcript follows.',
])
def test_third_person_intro_with_transcript_wording_is_kept(intro):
    body = ' '.join(['We stand with our friends and will keep working together for freedom.'] * 8)
    block = intro + '\n' + body
    keep, reason, after = rb._active_speaker_decision('https://www.state.gov/x/', block, block)
    assert keep and reason.startswith('third_person_intro+marker:'), reason
    assert after.startswith('We stand with our friends')


def test_third_person_readout_still_dropped():
    t = 'Secretary of State Marco Rubio met with Foreign Minister X today. They discussed trade.'
    assert rb._active_speaker_decision('https://www.state.gov/x/', t, t)[0] is False
