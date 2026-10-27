"""Offline tests for scripts/rubio_ground_truth.py (plan step 7, audit §3).

Everything runs against tests/fixtures/gt_site -- a tiny synthetic state.gov
whose markup is modelled on corpus Raw Text and rubio_backfill.py's parsers --
or against in-memory fake sessions. Nothing here touches the network.

Run:  python -m pytest tests/test_rubio_ground_truth.py
"""
import csv
import json
from datetime import date
from pathlib import Path

import pytest

import rubio_ground_truth as gt

FIX = Path(__file__).resolve().parent / 'fixtures' / 'gt_site'
W = 'https://www.state.gov'
SPOKES = W + '/releases/office-of-the-spokesman'


def _read(name):
    return (FIX / name).read_text(encoding='utf-8')


def _raw(title, typ, bylines, day, paras):
    """Flattened page text in the corpus "Raw Text" shape."""
    return ('Skip to content\nHome\n' + title + '\nhide\n' + title + '\n' + typ + '\n'
            + '\n'.join(bylines) + '\n' + day + '\n' + '\n'.join(paras)
            + '\nTags\nOffice of the Spokesperson\nBack to Top\n')


RUBIO = 'Marco Rubio, Secretary of State'


# ── E1 / E2 sitemap parsing ────────────────────────────────────────────────

def test_parse_sitemap_index_namespaced():
    kids = gt.parse_sitemap_index(_read('sitemap_index.xml'))
    locs = [k['loc'] for k in kids]
    assert f'{W}/state_press_release-sitemap1.xml' in locs
    assert len(kids) == 4
    assert all(k['lastmod'].startswith('2026-10-18') for k in kids)
    assert gt._family(locs[0]) == 'state_press_release'


def test_parse_urlset_and_family():
    urls = gt.parse_urlset(_read('state_press_release-sitemap1.xml'))
    assert len(urls) == 10
    assert all(u['loc'].startswith(SPOKES) for u in urls)
    assert gt._family(f'{W}/page-sitemap12.xml') == 'page'
    assert gt._family(f'{W}/state_ext_content-sitemap.xml') == 'state_ext_content'


def test_parse_urlset_without_namespace():
    xml = '<urlset><url><loc>https://x/a/</loc></url></urlset>'
    assert gt.parse_urlset(xml) == [{'loc': 'https://x/a/', 'lastmod': ''}]


@pytest.mark.parametrize('loc,lastmod,keep', [
    (f'{SPOKES}/2025/01/a/', '2024-01-01', True),       # URL month decides
    (f'{SPOKES}/2024/12/a/', '2026-05-01', False),      # edited later, still pre-era
    (f'{W}/flat-slug/', '2025-01-15T00:00:00+00:00', True),
    (f'{W}/flat-slug/', '2025-01-14', False),
    (f'{W}/flat-slug/', '', True),                      # no lastmod -> keep
    (f'{W}/flat-slug/', 'garbage', True),
])
def test_e2_window(loc, lastmod, keep):
    assert gt.e2_in_window(loc, lastmod) is keep


# ── E3 / E4 listing parsing ─────────────────────────────────────────────────

def test_parse_listing_page_items_and_pagination():
    p = gt.parse_listing_page(_read('remarks_p1.html'), W + '/remarks-secretary-rubio/')
    assert [i['type'] for i in p['items']] == ['Remarks to the Press', 'Press Statement']
    assert p['items'][0]['list_date'] == 'May 21, 2026'
    assert p['items'][0]['title'].startswith('Secretary of State Marco Rubio Remarks')
    assert p['total_pages'] == '2'
    assert p['items_header'].startswith('4 Items')
    assert p['max_link_page'] == 2


def test_parse_listing_page_relative_and_template_hrefs():
    html = ('<ul><li class="collection-result"><a href="/releases/x/2026/05/a/">A</a>'
            '<span>Remarks</span><span>May 1, 2026</span></li>'
            '<li class="collection-result"><a href="{{ item.link }}">T</a></li></ul>')
    p = gt.parse_listing_page(html, W + '/remarks-secretary-rubio/page/3/')
    assert [i['href'] for i in p['items']] == [W + '/releases/x/2026/05/a/']
    assert p['total_pages'] == '' and p['max_link_page'] == 1


def test_parse_listing_unknown_markup_is_empty_not_error():
    p = gt.parse_listing_page(_read('secretary_unknown_markup.html'), W + '/releases/secretary-of-state/')
    assert p['items'] == []


# ── Classification ──────────────────────────────────────────────────────────

def test_written_byline_rubio():
    raw = _raw('Independence Day', 'Press Statement', [RUBIO], 'May 14, 2026',
               ['On behalf of the United States of America, I congratulate the people.'])
    out = gt.classify_page(raw, url=f'{SPOKES}/2026/05/independence-day/')
    assert out['gt_class'] == 'rubio_written_byline'
    assert out['modality'] == 'written' and out['in_scope'] == 1
    assert out['byline'] == RUBIO and out['event_date'] == '2026-05-14'


def test_spokesperson_byline_is_out_of_scope():
    raw = _raw('Visa Restrictions', 'Press Statement', ['Office of the Spokesperson'],
               'May 12, 2026', ['The Department of State is taking steps.', 'Secretary Rubio has made clear.'])
    out = gt.classify_page(raw, url=f'{SPOKES}/2026/05/visa/')
    assert out['gt_class'] == 'spokesperson_third_person'
    assert out['in_scope'] == 0


def test_cuban_video_message_is_video_transcript():
    """C-01: 'Today, Secretary of State Marco Rubio delivered' + Translated Transcript."""
    raw = _raw('Secretary Rubio\u2019s Message to the Cuban People', 'Media Note', [RUBIO], 'May 20, 2026', [
        'Today, Secretary of State Marco Rubio delivered a video message to the people of Cuba.',
        'A courtesy translation of the Secretary\u2019s video message is below.',
        'Translated Transcript', 'On a day like today, in 1902, the Cuban flag flew.'])
    out = gt.classify_page(raw, url=f'{SPOKES}/2026/05/cuba/')
    assert out['gt_class'] == 'rubio_video_transcript'
    assert out['modality'] == 'video' and out['in_scope'] == 1
    assert out['third_person_opener'] == 1 and out['has_translated_transcript'] == 1


def test_rubio_byline_third_person_media_note_without_transcript():
    raw = _raw('Secretary Rubio Authorizes', 'Media Note', [RUBIO], 'May 20, 2026',
               ['Secretary Rubio met with the delegation today.'])
    assert gt.classify_page(raw)['gt_class'] == 'spokesperson_third_person'


def test_readout_attributable_to():
    raw = _raw('Secretary Rubio\u2019s Call', 'Readout', ['Office of the Spokesperson'], 'May 11, 2026',
               ['The below is attributable to Spokesperson Tammy Bruce:',
                'Secretary of State Marco Rubio spoke today with Foreign Minister Example.'])
    out = gt.classify_page(raw)
    assert out['gt_class'] == 'readout' and out['in_scope'] == 0
    # The opener alone decides even when the type line says something else.
    raw2 = raw.replace('\nReadout\n', '\nPress Statement\n')
    assert gt.classify_page(raw2)['gt_class'] == 'readout'


def test_joint_statement_co_bylined():
    raw = _raw('Joint Statement', 'Press Statement',
               [RUBIO, 'Robert F. Kennedy, Secretary of Health and Human Services'],
               'August 12, 2026', ['As the Secretariat returns to the United States, we express appreciation.'])
    out = gt.classify_page(raw)
    assert out['gt_class'] == 'rubio_joint' and out['modality'] == 'joint'
    assert 'Kennedy' in out['byline']


def test_spoken_labels_and_dateline_split():
    raw = _raw('Remarks to Press', 'Remarks to the Press',
               [RUBIO, 'Homestead Air Reserve Base', 'Homestead, Florida'], 'May 21, 2026',
               ['SECRETARY RUBIO:', 'Good morning.', 'QUESTION:', 'Secretary Rubio, one more?',
                'SECRETARY RUBIO:', 'Sure.'])
    out = gt.classify_page(raw)
    assert out['gt_class'] == 'rubio_spoken'
    assert out['rubio_label_count'] == 2          # the vocative is not a label (X-03)
    assert out['dateline'] == 'Homestead Air Reserve Base | Homestead, Florida'
    assert out['byline'] == RUBIO


def test_briefing_by_other_principal():
    raw = _raw('Department Press Briefing', 'Department Press Briefing',
               ['Tammy Bruce, Department Spokesperson'], 'May 19, 2026',
               ['MS BRUCE:', 'Good afternoon.', 'QUESTION:', 'Secretary Rubio, said what?'])
    out = gt.classify_page(raw)
    assert out['gt_class'] == 'other_principal' and out['in_scope'] == 0


def test_pre_tenure_is_out_of_scope():
    raw = _raw('Opening Statement', 'Testimony', [RUBIO], 'January 15, 2025',
               ['SECRETARY-DESIGNATE RUBIO:', 'Thank you, Mr. Chairman.'])
    out = gt.classify_page(raw)
    assert out['gt_class'] == 'rubio_spoken' and out['in_scope'] == 0
    assert 'pre-tenure' in out['reason']


def test_external_redirect_and_fetch_error():
    out = gt.classify_page('anything', url=W + '/external-content/x/',
                           final_url='https://www.whitehouse.gov/x/', status=200)
    assert out['gt_class'] == 'external_redirect' and out['in_scope'] == 0
    # Host decides even when the redirect target refused us (F-02's 403).
    out = gt.classify_page('', url=W + '/external-content/x/',
                           final_url='https://www.whitehouse.gov/x/', status=403)
    assert out['gt_class'] == 'external_redirect'
    out = gt.classify_page('', url=W + '/a/', status=503, error='HTTP 503')
    assert out['gt_class'] == 'fetch_error' and out['in_scope'] == ''


def test_local_label_regex_rejects_vocative_mrs_senator():
    text = ('SECRETARY RUBIO:\nHi.\nMRS RUBIO:\nHello.\nSENATOR RUBIO:\nOld clip.\n'
            'Secretary Rubio: thanks.\nSECRETARY\u00a0RUBIO\n:\nSplit colon.\nSECRETARY RUBI:\nTypo.\n')
    assert len(gt._RUBIO_LABEL_RE.findall(text)) == 2
    _, fuzzy, _ = gt.count_rubio_labels(text)
    assert 'SECRETARY RUBI' in fuzzy and 'MRS RUBIO' in fuzzy


def test_label_count_falls_back_without_rubio_turns(monkeypatch):
    monkeypatch.setattr(gt, 'rubio_turns', None)
    n, _, method = gt.count_rubio_labels('SECRETARY RUBIO:\nHi.\nQUESTION:\nQ?\n'
                                         'SECRETARY-DESIGNATE RUBIO:\nYes.\n')
    assert (n, method) == (2, 'local_regex')


def test_jsonld_graph_and_author():
    soup = gt._make_soup(_read('cuba_video.html'))
    ld = gt.parse_jsonld(soup)
    assert ld['date_published'].startswith('2026-05-20')
    assert ld['date_modified'].startswith('2026-05-20T11')


def test_probe_page_through_fixture_session():
    sess = gt.FixtureSession(FIX)
    f = gt.PoliteFetcher(sess, max_requests=5, sleep=lambda s: None)
    row = gt.probe_page(f, f'{SPOKES}/2026/05/secretary-rubios-message-to-the-cuban-people-on-their-independence-day/',
                        gt.DEFAULT_SCOPE_FLOOR)
    assert row['gt_class'] == 'rubio_video_transcript'
    assert row['block_type'] == 'Media Note' and row['byline'] == RUBIO
    assert row['slug_date_key'].endswith('2026-05-20')
    assert row['final_host'] == 'www.state.gov' and row['status'] == 200


# ── Keys ────────────────────────────────────────────────────────────────────

def test_url_key_folds_spokesperson_on_www_only():
    a = gt.url_key(W + '/releases/office-of-the-spokesperson/2026/05/x/?utm=1#frag')
    assert a == 'state.gov/releases/office-of-the-spokesman/2026/05/x'
    b = gt.url_key('https://2021-2025.state.gov/releases/office-of-the-spokesperson/2024/05/x/')
    assert 'office-of-the-spokesperson' in b


# ── Politeness: spacing, backoff, cap ───────────────────────────────────────

class _Resp:
    def __init__(self, status, headers=None, text='', url=''):
        self.status_code = status
        self.headers = headers or {}
        self.text = text
        self.url = url
        self.history = []


class _ScriptedSession:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        r = self.script.pop(0) if self.script else _Resp(200)
        if isinstance(r, Exception):
            raise r
        r.url = r.url or url
        return r


class _Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def test_spacing_is_at_least_min_interval_with_deterministic_jitter():
    c = _Clock()
    f = gt.PoliteFetcher(_ScriptedSession([]), max_requests=10, min_interval=2.0, jitter=1.0,
                         sleep=c.sleep, clock=c.clock)
    f.get('https://a/1')
    f.get('https://a/2')
    f.get('https://a/3')
    assert len(c.sleeps) == 2                        # no wait before the first request
    assert all(2.0 <= s < 3.0 for s in c.sleeps)
    assert gt._jitter('https://a/2', 1.0) == gt._jitter('https://a/2', 1.0)


def test_backoff_honours_retry_after_and_counts_against_cap():
    c = _Clock()
    sess = _ScriptedSession([_Resp(429, {'Retry-After': '7'}), _Resp(503), _Resp(200, text='ok')])
    f = gt.PoliteFetcher(sess, max_requests=10, min_interval=0, jitter=0,
                         backoff_base=5.0, sleep=c.sleep, clock=c.clock)
    res = f.get('https://a/x')
    assert res.ok and res.attempts == 3
    assert c.sleeps == [7.0, 10.0]                   # Retry-After, then base*2**1
    assert f.used == 3 and f.retries == 2


def test_connection_error_is_retried_then_reported():
    c = _Clock()
    sess = _ScriptedSession([ConnectionError('boom')] * 4)
    f = gt.PoliteFetcher(sess, max_requests=10, min_interval=0, jitter=0, max_attempts=4,
                         backoff_base=1.0, sleep=c.sleep, clock=c.clock)
    res = f.get('https://a/x')
    assert res.resp is None and 'boom' in res.error and f.used == 4


def test_404_is_not_retried():
    f = gt.PoliteFetcher(_ScriptedSession([_Resp(404)]), max_requests=10, min_interval=0,
                         sleep=lambda s: None)
    res = f.get('https://a/x')
    assert res.status == 404 and f.used == 1 and not res.ok


def test_hard_cap_raises_before_exceeding():
    sess = _ScriptedSession([_Resp(503)] * 10)
    f = gt.PoliteFetcher(sess, max_requests=2, min_interval=0, jitter=0, sleep=lambda s: None)
    with pytest.raises(gt.BudgetExhausted):
        f.get('https://a/x')
    assert len(sess.calls) == 2 and f.used == 2


def test_retry_after_http_date():
    assert gt._retry_after_seconds('Thu, 01 Jan 1970 00:01:40 GMT', now=0.0) == 100.0
    assert gt._retry_after_seconds('') is None


# ── End-to-end offline run, partial run + resume ────────────────────────────

def _run(tmp, *extra):
    args = ['--offline-fixtures', str(FIX), '--corpus', str(FIX / 'corpus_small.csv'),
            '--classified', str(FIX / 'classified.csv'), '--run-id', 't', *extra]
    return gt.main(args)


def _csv(path):
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def test_full_offline_run(tmp_path):
    out = tmp_path / 'full'
    assert _run(tmp_path, '--out-dir', str(out)) == 0
    for name in ('sitemap_inventory.csv', 'listing_snapshot.csv', 'candidates.csv', 'probe.csv',
                 'ground_truth.csv', 'gt_diff.csv', 'drift_report.csv', 'run_meta.json',
                 'gt_state.json'):
        assert (out / name).exists(), name
    g = {r['url_key']: r for r in _csv(out / 'ground_truth.csv')}
    k = lambda u: gt.url_key(u)  # noqa: E731
    assert set(_csv(out / 'ground_truth.csv')[0]) >= {
        'url_key', 'slug_date_key', 'canonical_url', 'gt_class', 'modality', 'event_date',
        'source_set', 'in_scope', 'first_seen'}
    cuba = g[k(f'{SPOKES}/2026/05/secretary-rubios-message-to-the-cuban-people-on-their-independence-day/')]
    assert cuba['gt_class'] == 'rubio_video_transcript' and cuba['in_scope'] == '1'
    iop = g[k(f'{SPOKES}/2025/12/secretary-of-state-marco-rubio-remarks-to-press-at-donald-j-trump-institute-of-peace/')]
    assert iop['gt_class'] == 'rubio_spoken' and int(iop['source_set']) == gt.SRC_E2 | gt.SRC_E3
    home = g[k(W + '/secretary-of-state-marco-rubio-remarks-to-press-at-the-miami-homestead-airport/')]
    assert home['gt_class'] == 'rubio_spoken'
    assert g[k(f'{SPOKES}/2026/01/joint-statement-on-termination-of-who-membership/')]['gt_class'] == 'rubio_joint'
    assert g[k(f'{SPOKES}/2026/05/secretary-rubios-call-with-foreign-minister-example/')]['in_scope'] == '0'
    assert g[k(W + '/external-content/statement-from-the-white-house-example/')]['gt_class'] == 'external_redirect'
    # Corpus rows: url_key match through the spokesperson->spokesman fold,
    # slug+month match across a path change, pre-tenure row out of scope.
    exist = g[k(f'{SPOKES}/2026/09/existing-corpus-statement/')]
    assert exist['in_corpus'] == '1' and int(exist['source_set']) & gt.SRC_CORPUS
    assert int(exist['source_set']) & gt.SRC_E3
    renamed = g[k(W + '/releases/secretary-of-state/2026/09/renamed-in-place/')]
    assert int(renamed['source_set']) & gt.SRC_E2          # merged via slug_ym
    assert g[k(W + '/secretary-rubio-confirmation-opening-statement')]['in_scope'] == '0'
    # Classified-only (never listed) item carried into the ground truth.
    assert g[k(f'{SPOKES}/2025/07/joint-statement-rfk-ihr/')]['source_set'] == str(gt.SRC_CLASSIFIED)

    diff = {r['url_key'] for r in _csv(out / 'gt_diff.csv')}
    assert cuba['url_key'] in diff and iop['url_key'] in diff and home['url_key'] in diff
    assert exist['url_key'] not in diff

    cands = {r['url_key']: r for r in _csv(out / 'candidates.csv')}
    assert cands[k(f'{SPOKES}/2026/05/independence-day-of-example/')]['probe_status'] == 'skipped_classified'
    assert cands[k(f'{SPOKES}/2024/12/old-one/')]['probe_status'] == 'skipped_window'
    assert cands[k(f'{SPOKES}/2026/09/renamed-in-place/')]['corpus_match'] == 'slug_ym'
    # No 'rubio' filter on E2: the briefing and visa release are candidates.
    assert k(W + '/briefings/department-press-briefing-may-19-2026/') in cands

    sess_calls = json.loads((out / 'run_meta.json').read_text())
    probed = {r['url_key'] for r in _csv(out / 'probe.csv')}
    assert exist['url_key'] not in probed                      # corpus keys never probed
    assert k(f'{SPOKES}/2026/05/independence-day-of-example/') not in probed
    assert sess_calls['stage_status']['E4'] == 'done'
    # The 503 page is retryable, so E6 (and the run) is not complete.
    assert sess_calls['complete'] is False and sess_calls['stage_status']['E6'] == 'partial'

    drift = _csv(out / 'drift_report.csv')
    assert any(r['kind'] == 'dup_slot_p1' for r in drift)
    hdr = [r for r in drift if r['kind'] == 'summary_p1' and r['detail'] == 'header_minus_unique']
    assert hdr[0]['value'] == '1'

    snap = _csv(out / 'listing_snapshot.csv')
    p2 = [r for r in snap if r['stage'] == 'E3' and r['pass'] == '2']
    assert all('?gt=t' in r['request_url'] for r in p2)
    assert {r['x_cache'] for r in snap if r['stage'] == 'E3'} >= {'Hit from cloudfront', 'Miss from cloudfront'}
    spk_pages = {r['page'] for r in snap if r['listing'] == 'spokesman'}
    assert spk_pages == {'1', '2', '3'}                       # stopped by date, not total_pages=10
    meta = json.loads((out / 'run_meta.json').read_text())
    assert any('collection-result' in e['error'] for e in meta['errors'])  # secretary listing logged


def test_cap_then_resume_matches_single_run(tmp_path):
    full = tmp_path / 'full'
    _run(tmp_path, '--out-dir', str(full))
    total = json.loads((full / 'run_meta.json').read_text())['requests_this_run']

    part = tmp_path / 'part'
    assert _run(tmp_path, '--out-dir', str(part), '--max-requests', '6') == 0
    m1 = json.loads((part / 'run_meta.json').read_text())
    assert m1['budget_exhausted'] and m1['requests_this_run'] == 6 and not m1['complete']

    seen = 6
    state = part / 'gt_state.json'
    for i in range(10):
        nxt = tmp_path / f'resume{i}'
        assert _run(tmp_path, '--out-dir', str(nxt), '--state-in', str(state),
                    '--max-requests', '8') == 0
        m = json.loads((nxt / 'run_meta.json').read_text())
        seen += m['requests_this_run']
        state = nxt / 'gt_state.json'
        if not m['budget_exhausted']:
            break
    assert not m['budget_exhausted'] and i > 0
    assert m['requests_total'] == seen
    # Nothing done twice: resumed total equals one uncapped run, except that the
    # still-failing 503 page is retried once per resumed run (4 attempts each).
    assert seen >= total
    a = sorted((r['url_key'], r['gt_class'], r['in_scope']) for r in _csv(full / 'ground_truth.csv'))
    b = sorted((r['url_key'], r['gt_class'], r['in_scope']) for r in _csv(nxt / 'ground_truth.csv'))
    assert a == b


def test_persistent_fetch_error_given_up_after_max_runs(tmp_path):
    state = None
    for i in range(gt.MAX_PROBE_RUNS + 1):
        out = tmp_path / f'r{i}'
        extra = ['--state-in', str(state)] if state else []
        _run(tmp_path, '--out-dir', str(out), *extra)
        state = out / 'gt_state.json'
        m = json.loads((out / 'run_meta.json').read_text())
        if i < gt.MAX_PROBE_RUNS - 1:
            assert not m['complete']
    assert m['complete'] is True and m['requests_this_run'] == 0
    row = [r for r in _csv(out / 'probe.csv') if 'a-page-that-503s' in r['url']][0]
    assert row['gt_class'] == 'fetch_error' and row['probe_runs'] == str(gt.MAX_PROBE_RUNS)


def test_resume_skips_finished_work(tmp_path):
    out1 = tmp_path / 'one'
    _run(tmp_path, '--out-dir', str(out1), '--stages', 'E1,E2,E3,E4')
    out2 = tmp_path / 'two'
    _run(tmp_path, '--out-dir', str(out2), '--state-in', str(out1 / 'gt_state.json'),
         '--stages', 'E1,E2,E3,E4')
    assert json.loads((out2 / 'run_meta.json').read_text())['requests_this_run'] == 0


def test_e6_requires_corpus(tmp_path):
    assert gt.main(['--offline-fixtures', str(FIX), '--stages', 'E6',
                    '--out-dir', str(tmp_path / 'x')]) == 2


def test_unknown_stage_rejected(tmp_path):
    assert gt.main(['--stages', 'E9', '--out-dir', str(tmp_path / 'x')]) == 2


def test_fixtures_are_small():
    for p in FIX.iterdir():
        assert p.stat().st_size < 200_000, p


# ---------------------------------------------------------------------------
# T1: translations are in scope only without an English original (2026-10-22)
# ---------------------------------------------------------------------------

class _TResp:
    def __init__(self, text, status=200, url=''):
        self.text, self.status_code, self.url, self.history = text, status, url, []


class _FakeFetcher:
    def __init__(self, pages):
        self.pages, self.used, self.max_requests = pages, 0, 10 ** 6

    @property
    def remaining(self):
        return self.max_requests - self.used

    def get(self, url):
        self.used += 1
        if url not in self.pages:
            return gt.FetchResult(url, _TResp('', 404, url), '', 1)
        return gt.FetchResult(url, _TResp(self.pages[url], 200, url), '', 1)


_ES = 'https://www.state.gov/translations/spanish/comparecencia-del-secretario-rubio/'
_ZH = 'https://www.state.gov/translations/chinese/%e5%8d%a2%e6%af%94%e5%a5%a5/'
_UNIQUE = 'https://www.state.gov/translations/spanish/mensaje-solo-en-espanol/'
_ORIG = ('https://www.state.gov/releases/office-of-the-spokesperson/2026/06/'
         'secretary-of-state-marco-rubio-before-the-house-foreign-affairs-committee/')


def _tpage(title, body, original=None, prefix='Para ver el texto original, ir a:'):
    link = (f'<p>{prefix} <a href="{original}">{original}</a></p>' if original else '')
    return (f'<html><body><nav><a href="https://www.state.gov/translations/spanish/">Español</a></nav>'
            f'<main><span>hide</span><h1>{title}</h1><p>{body}</p>{link}'
            f'<p><a href="https://www.state.gov/releases/office-of-the-spokesman/2026/06/other/">'
            f'Más</a></p></main></body></html>')


def test_translation_lang():
    assert gt.translation_lang(_ES) == 'spanish'
    assert gt.translation_lang(_ZH) == 'chinese'
    assert gt.translation_lang(
        'https://www.state.gov/translations/%d1%80%d1%83%d1%81%d1%81%d0%ba%d0%b8%d0%b9/x/') == 'русский'
    assert gt.translation_lang('https://www.state.gov/translations/spanish/') == ''   # index page
    assert gt.translation_lang(_ORIG) == ''


def test_find_original_url_needs_url_anchor_text():
    soup = gt._make_soup(_tpage('T', 'Gracias.', _ORIG))
    assert gt.find_original_url(soup) == _ORIG
    # A state.gov link whose anchor text is not a URL (navigation, "Más") is not it.
    assert gt.find_original_url(gt._make_soup(_tpage('T', 'Gracias.'))) == ''
    # Persian/Urdu layout: the anchor text is the English title, not the URL.
    html = ('<html><body><main><h1>T</h1><p>\u067e\u06cc\u0648\u0646\u062f: <a href="' + _ORIG + '">'
            'Secretary of State Marco Rubio Before the House Foreign Affairs Committee</a></p>'
            '<p><a href="https://www.state.gov/bureau-of-near-eastern-affairs/">Near Eastern Affairs</a></p>'
            '</main></body></html>')
    assert gt.find_original_url(gt._make_soup(html)) == _ORIG
    # An English-titled navigation link (bureau page) is not an original.
    html = ('<html><body><main><h1>T</h1><p><a href="https://www.state.gov/bureaus-offices/'
            'under-secretary-for-management/bureau-of-budget-and-planning/">Bureau of Budget and '
            'Planning Under Secretary for Management</a></p></main></body></html>')
    assert gt.find_original_url(gt._make_soup(html)) == ''
    assert gt._is_item_path('/secretary-of-state-marco-rubio-at-a-tour-of-aeroman/')
    assert not gt._is_item_path('/bureau-of-western-hemisphere-affairs-and-more/')
    # A byline linking to the biography is never the original, even when it
    # comes first and reads as five ASCII words.
    html = ('<html><body><main><h1>T</h1><p><a href="https://www.state.gov/biographies/marco-rubio/">'
            'MARCO RUBIO, SECRETARIO DE ESTADO</a></p><p>Para ver el texto original, ir a: '
            '<a href="' + _ORIG + '">' + _ORIG + '</a></p></main></body></html>')
    assert gt.find_original_url(gt._make_soup(html)) == _ORIG


def test_t1_classifies_and_resumes():
    pages = {
        _ES: _tpage('Comparecencia', 'SECRETARIO RUBIO: Gracias.', _ORIG),
        _ZH: _tpage('卢比奥国务卿', '卢比奥国务卿：谢谢。', _ORIG, prefix='欲查看原稿内容:'),
        _UNIQUE: _tpage('Mensaje', 'SECRETARIO RUBIO: Solo en español.'),
    }
    state = gt.new_state()
    state['e2_urls'] = {u: {} for u in (_ES, _ZH, _UNIQUE, 'https://www.state.gov/translations/spanish/',
                                        _ORIG)}
    known = {gt.url_key(_ORIG)}
    f = _FakeFetcher(pages)
    n, left = gt.run_t1(f, state, known)
    assert (n, left) == (3, 0) and f.used == 3          # index page and English URL not probed
    by = {r['url']: r for r in state['translations'].values()}
    assert by[_ES]['t_class'] == 'translation_of_english' and by[_ES]['original_known'] == 1
    assert by[_ZH]['lang'] == 'chinese' and by[_ZH]['t_class'] == 'translation_of_english'
    assert by[_UNIQUE]['t_class'] == 'translation_unique' and by[_UNIQUE]['needs_review'] == 1
    # Resume: nothing left to fetch.
    f2 = _FakeFetcher(pages)
    assert gt.run_t1(f2, state, known) == (0, 0) and f2.used == 0


def test_t1_original_known_ignores_spokesperson_fold():
    # A corpus key stored under the pre-rename path still counts as known.
    pages = {_ES: _tpage('T', 'x', _ORIG)}
    state = gt.new_state()
    state['e2_urls'] = {_ES: {}}
    from corpus_keys import canonical_url_key
    gt.run_t1(_FakeFetcher(pages), state, {canonical_url_key(_ORIG)})
    assert next(iter(state['translations'].values()))['original_known'] == 1


def test_e6_skips_translations_and_probes_linked_originals_first():
    orig = 'https://www.state.gov/responding-to-panama-earthquake/'
    other = 'https://www.state.gov/aaa-some-other-undated-page/'
    cands = {}
    for u in (other, orig, _ES):
        cands[gt.url_key(u)] = {'url': u, 'url_key': gt.url_key(u), 'probe_status': 'pending',
                                'probe_priority': 3}
    state = gt.new_state()
    state['translations'] = {'t': {'original_key': gt.url_key(orig)}}
    seen = []
    real = gt.probe_page
    gt.probe_page = lambda f, u, sf: (seen.append(u) or {'gt_class': 'readout'})
    try:
        gt.run_e6(_FakeFetcher({}), state, cands, max_probes=0, scope_floor=gt.DEFAULT_SCOPE_FLOOR)
    finally:
        gt.probe_page = real
    assert seen == [orig, other]          # linked original first; translation never probed
    assert cands[gt.url_key(_ES)]['probe_status'] == 'pending'


def test_translation_pages_are_never_in_scope_items():
    """A translation of an English item is the same item; a unique translation is
    recorded but not added (2026-10-22). Rubio labels on the page do not change that."""
    from rubio_ground_truth import apply_translation_scope
    rows = [{'url_key': 'state.gov/translations/spanish/entrevista', 'gt_class': 'rubio_spoken', 'in_scope': 1,
             'canonical_url': 'https://www.state.gov/translations/spanish/entrevista/'},
            {'url_key': 'state.gov/translations/french/op-ed', 'gt_class': 'rubio_written_byline', 'in_scope': 1,
             'canonical_url': 'https://www.state.gov/translations/french/op-ed/'},
            {'url_key': 'state.gov/releases/x', 'gt_class': 'rubio_spoken', 'in_scope': 1,
             'canonical_url': 'https://www.state.gov/releases/x/'}]
    tr = {'state.gov/translations/spanish/entrevista': {'t_class': 'translation_of_english'},
          'state.gov/translations/french/op-ed': {'t_class': 'translation_unique'}}
    assert apply_translation_scope(rows, tr) == 2
    assert [(r['gt_class'], r['in_scope']) for r in rows] == [
        ('translation_of_english', 0), ('translation_unique', 0), ('rubio_spoken', 1)]


def test_joint_statement_rule_reclassifies_stored_probes():
    """The weekly path's _JOINT_RELEASED_RE, applied to probes classified before it."""
    from rubio_ground_truth import apply_joint_rule, classify_page
    rows = [{'gt_class': 'spokesperson_third_person', 'in_scope': 0, 'event_date': '2025-10-16',
             'first_600': 'Joint Statement by Secretary of State Marco Rubio, United States Trade '
                          'Representative Jamieson Greer, and Minister of Foreign Affairs for Brazil', 'title': ''},
            {'gt_class': 'spokesperson_third_person', 'in_scope': 0, 'event_date': '2025-10-16',
             'first_600': 'The following statement was released by the Governments of the United States and Brazil.',
             'title': ''}]
    assert apply_joint_rule(rows) == 1
    assert (rows[0]['gt_class'], rows[0]['in_scope']) == ('rubio_joint', 1)
    assert rows[1]['gt_class'] == 'spokesperson_third_person'
    raw = ('U.S.-Brazil Meetings\nhide\nU.S.-Brazil Meetings\nMedia Note\nOffice of the Spokesperson\n'
           'October 16, 2025\nJoint Statement by Secretary of State Marco Rubio, United States Trade '
           'Representative Jamieson Greer, and Minister of Foreign Affairs for Brazil Mauro Vieira\n'
           'Today, the three met in Washington.\nTags\n')
    assert classify_page(raw, url='https://www.state.gov/releases/office-of-the-spokesman/2025/10/u-s-brazil-meetings/',
                         status=200)['gt_class'] == 'rubio_joint'
