"""Offline tests for the Rubio coverage harness (plan step 8): rubio_coverage_reconcile.py
and the verifier checks AC-AI in corpus_verify_research_grade.py.

Everything is synthetic -- small strings shaped like state.gov pages -- so nothing
touches the network or the 482MB corpus.

Run: python -m pytest tests/test_rubio_coverage_reconcile.py -q
"""
import csv
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent / 'scripts'
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import pytest  # noqa: E402

import rubio_coverage_reconcile as rcr  # noqa: E402

VERIFIER = _SCRIPTS / 'corpus_verify_research_grade.py'
RECONCILER = _SCRIPTS / 'rubio_coverage_reconcile.py'

# ---------------------------------------------------------------------------
# Synthetic pages
# ---------------------------------------------------------------------------
_NAV = ('Skip to content\nAn official website of the United States Government\n'
        'Topics\nCountries and Areas\nShare this page on:\nClose\nhide\n')
_FOOTER = ('\nBack to Top\nContact Us\nPrivacy Policy\nAccessibility Statement\nFOIA\n')

R1 = ('Well, thank you all for being here today. We had a long series of meetings with our '
      'partners on regional security, on trade, and on the migration challenges that we '
      'share, and I think we made real progress on every one of those questions.')
MOD = ('Thank you, Mr. Secretary. Our next question comes from the back of the room, '
       'from a reporter who has been waiting patiently for quite some time now.')
R2 = ('Look, the President has been very clear about this. We are going to protect our '
      'interests in this hemisphere, and we are going to work with every country that '
      'wants to work with us on stopping the cartels and the flow of fentanyl.')


def _raw(block_body: str, byline: str = 'Marco Rubio, Secretary of State',
         stype: str = 'Remarks') -> str:
    head = f'Secretary Rubio Remarks to Press\n{stype}\n{byline}\nWashington, D.C.\nMarch 3, 2026\n'
    return _NAV + head + block_body + '\nTags\nOffice of the Spokesperson' + _FOOTER


TRANSCRIPT = _raw(f'SECRETARY RUBIO:\n{R1}\nMODERATOR:\n{MOD}\nSECRETARY RUBIO:\n{R2}')
STATEMENT_BODY = ('The United States congratulates the people of Ruritania on their national day. '
                  'We look forward to deepening the partnership between our two nations in the '
                  'year ahead, on trade, on security, and on the values we share.')


def _row(rid, url, sos, raw=TRANSCRIPT, d='2026-03-03', sec='Rubio', title='Remarks to Press'):
    return {'Row Id': str(rid), 'Secretary': sec, 'Date': d, 'Title': title,
            'Speech Type': 'Remarks', 'Location': 'Washington, D.C.', 'URL': url,
            'SOS Text': sos, 'Natural Text': sos, 'Raw Text': raw}


U = 'https://www.state.gov/releases/office-of-the-spokesman/2026/03/'


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def test_item_key_folds_spokesperson_on_www_only():
    a = rcr.item_key(U.replace('spokesman', 'spokesperson') + 'remarks-to-press-7/?utm_source=x#top')
    b = rcr.item_key(U + 'remarks-to-press-7')
    assert a == b == 'state.gov/releases/office-of-the-spokesman/2026/03/remarks-to-press-7'
    arch = 'https://2021-2025.state.gov/releases/office-of-the-spokesperson/2024/03/x/'
    assert 'office-of-the-spokesperson' in rcr.item_key(arch)
    # exclusions store keys without a scheme
    assert rcr.item_key('state.gov/releases/office-of-the-spokesperson/2026/03/remarks-to-press-7') == b


def test_norm_slug_date_accepts_pipe_form():
    assert rcr.norm_slug_date('abc|2026-03-03') == 'abc\x1f2026-03-03'


# ---------------------------------------------------------------------------
# Item level
# ---------------------------------------------------------------------------

def _gt(url, cls='rubio_spoken', in_scope='1', ev='2026-03-03', first_seen='2026-01-01', sd=''):
    return {'url_key': rcr.item_key(url), 'slug_date_key': sd, 'canonical_url': url,
            'gt_class': cls, 'in_scope': in_scope, 'modality': 'spoken', 'event_date': ev,
            'first_seen': first_seen}


@pytest.fixture
def index():
    rows = [
        _row(1, U.replace('spokesman', 'spokesperson') + 'remarks-to-press-7/', 'x'),
        _row(2, 'https://www.state.gov/secretary-rubio-on-ruritania-national-day/', 'x',
             d='2026-03-10'),
        _row(3, 'https://www.state.gov/annual-national-day/', 'x', d='2025-07-04'),
    ]
    return rcr.CorpusIndex(rows)


def test_reconcile_statuses(index):
    excl = ({rcr.item_key('state.gov/excluded-readout'): 'readout'}, {})
    gt = [
        _gt(U + 'remarks-to-press-7/'),                                  # url key + fold
        _gt(U + 'secretary-rubio-on-ruritania-national-day/',            # moved path:
            sd='secretary-rubio-on-ruritania-national-day|2026-03-10'),  # slug+date
        _gt(U + 'annual-national-day/',                                  # same slug, other
            sd='annual-national-day|2026-07-04'),                        # date -> NOT present
        _gt('https://www.state.gov/excluded-readout/'),
        _gt(U + 'readout-x/', cls='readout', in_scope='0'),
        _gt(U + 'old/', ev='2025-01-15'),
        _gt(U + 'probe-failed/', cls='fetch_error', in_scope=''),
    ]
    items = rcr.reconcile_items(gt, index, excl, as_of='2026-03-20')
    st = [i['status'] for i in items]
    assert st == ['present', 'present', 'missing', 'excluded', 'out_of_scope',
                  'out_of_scope', 'unknown']
    assert items[0]['matched_by'] == 'url_key' and items[0]['corpus_row_id'] == '1'
    assert items[1]['matched_by'] == 'slug_date'
    assert [i['canonical_url'] for i in rcr.item_failures(items)] == [U + 'annual-national-day/']


def test_missing_grace_first_seen_pending_and_allowlist(index, tmp_path):
    gt = [_gt(U + 'new-item/', first_seen='2026-03-15'),
          _gt(U + 'queued-once/'), _gt(U + 'queued-thrice/'), _gt(U + 'allowed/')]
    pending = {rcr.item_key(U + 'queued-once/'): {'since': '2026-03-01', 'attempts': 1, 'url': ''},
               rcr.item_key(U + 'queued-thrice/'): {'since': '2026-01-01', 'attempts': 3, 'url': ''}}
    al = tmp_path / 'allow.csv'
    al.write_text(f'row_id,url,reason,expires\n,{U}allowed/,awaiting decision,2026-12-31\n')
    allow = rcr.Allowlist(al, today=date(2026, 10, 21))
    items = rcr.reconcile_items(gt, index, ({}, {}), pending=pending, allow=allow,
                                as_of='2026-03-20', grace_days=7)
    by = {i['canonical_url'].rsplit('/', 2)[-2]: i for i in items}
    assert by['new-item']['grace'] == 1
    assert by['queued-once']['grace'] == 1
    assert by['queued-thrice']['grace'] == 0
    assert by['allowed']['allowlisted']
    assert [i['canonical_url'] for i in rcr.item_failures(items)] == [U + 'queued-thrice/']


def test_allowlist_expiry(tmp_path):
    al = tmp_path / 'allow.csv'
    al.write_text('row_id,url,reason,expires\n17095,,repair pending,2026-10-01\n'
                  '17342,,repair pending,2026-11-30\n')
    a = rcr.Allowlist(al, today=date(2026, 10, 21))
    assert a.get('17095') is None and len(a.expired) == 1
    assert a.get('17342') is not None


def test_recall_tables_and_family_gaps(index):
    items = rcr.reconcile_items([_gt(U + 'remarks-to-press-7/'), _gt(U + 'gone/', ev='2026-04-02')],
                                index, ({}, {}), as_of='2026-06-01')
    months = {r['month']: r['recall'] for r in rcr.recall_table(items, 'month')}
    assert months == {'2026-03': 1.0, '2026-04': 0.0}
    gaps = rcr.slug_family_gaps([U + 'remarks-to-press/', U + 'remarks-to-press-2/',
                                 U + 'remarks-to-press-4/', U + 'mexico-national-day/'])
    assert gaps == [{'family': 'remarks-to-press', 'have': [1, 2, 4], 'gaps': [3]}]


# ---------------------------------------------------------------------------
# Turn level
# ---------------------------------------------------------------------------

def test_inline_labels_unicode_and_false_positives():
    t = ('We agree. FOREIGN MINISTER RAMÍREZ: Yes, exactly. And NATO: an alliance. '
         'At 10:30 we met. QUESTION: why? COMMISSIONER ŠEFČOVIČ: thanks')
    assert rcr.inline_labels(t) == ['FOREIGN MINISTER RAMÍREZ', 'QUESTION', 'COMMISSIONER ŠEFČOVIČ']
    assert rcr.inline_labels('Let me be clear: Iran should not target U.S. interests.') == []


def test_mid_word_start_and_nav_tail():
    raw = 'Tonight, Israel took unilateral action. We are told that they believe this was needed.'
    assert rcr.mid_word_start('hat they believe this was needed.', raw)
    assert not rcr.mid_word_start('they believe this was needed.', raw)   # word boundary
    assert not rcr.mid_word_start('Tonight, Israel took', raw)
    assert rcr.nav_tail('... personnel." Related WHAT THEY ARE SAYING: x Releases June 22, 2025 Notifications')
    assert not rcr.nav_tail('We discussed related issues and notifications of travel.')


def test_row_coverage_full_dropped_and_leak():
    good = rcr.row_coverage(_row(1, U + 'a/', f'{R1}\n\n{R2}'))
    assert good['scored'] == 'turns' and good['n_rubio_turns'] == 2 and good['n_other_turns'] == 1
    assert good['rubio_recall'] == 1.0 and good['foreign_leak_words'] == 0
    assert rcr.turn_failures(good) == [] and good['non_rubio_byline'] == 0

    dropped = rcr.row_coverage(_row(2, U + 'b/', R1))          # X-01: final turn lost
    assert dropped['rubio_recall'] < rcr.RECALL_MIN
    assert any('recall' in r for r in rcr.turn_failures(dropped))

    leaked = rcr.row_coverage(_row(3, U + 'c/', f'{R1}\n\n{MOD}\n\n{R2}'))   # X-03
    # recall dips only by the turn-junction shingles (rubio_reextract.reference joins turns)
    assert leaked['rubio_recall'] > 0.9 and leaked['foreign_leak_words'] >= 20
    assert any('leak' in r for r in rcr.turn_failures(leaked))


def test_row_coverage_label_less_and_spokesperson_byline():
    raw = _raw(STATEMENT_BODY, stype='Press Statement')
    m = rcr.row_coverage(_row(1, U + 'd/', STATEMENT_BODY, raw=raw))
    assert m['scored'] == 'block' and m['n_rubio_turns'] == 0
    assert rcr.turn_failures(m) == []                     # label-less rows are not gated
    note = _raw(STATEMENT_BODY, byline='Office of the Spokesperson', stype='Media Note')
    m2 = rcr.row_coverage(_row(2, U + 'e/', STATEMENT_BODY, raw=note))
    assert m2['non_rubio_byline'] == 1 and m2['byline_reason'] == 'spokesperson_byline'
    assert any('non_rubio_byline' in f for f in rcr.turn_flags(m2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_COLS = ['Row Id', 'Secretary', 'Date', 'Title', 'Speech Type', 'Location', 'URL',
         'SOS Text', 'Natural Text', 'Raw Text']


def _write_csv(path, rows, cols=_COLS):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    return path


def _run(script, *args):
    r = subprocess.run([sys.executable, str(script), *map(str, args)],
                       capture_output=True, text=True, timeout=300)
    return r.returncode, r.stdout + r.stderr


def test_cli_exit_codes_and_outputs(tmp_path):
    corpus = _write_csv(tmp_path / 'c.csv', [
        _row(0, U + 'good/', f'{R1}\n\n{R2}'),
        _row(1, U + 'dropped/', R1),
        _row(2, 'https://www.state.gov/blinken-thing/', 'Unrelated.', sec='Blinken'),
    ])
    gt = _write_csv(tmp_path / 'gt.csv', [_gt(U + 'good/'), _gt(U + 'missing-one/')],
                    cols=['url_key', 'slug_date_key', 'canonical_url', 'gt_class', 'in_scope',
                          'modality', 'event_date', 'first_seen'])
    excl = tmp_path / 'excl.csv'
    excl.write_text('url_key,slug_date_key,reason,decided_by,date\n')
    out = tmp_path / 'out'
    rc, log = _run(RECONCILER, '--corpus', corpus, '--exclusions', excl, '--out', out)
    assert rc == 1, log                                  # recall failure on row 1
    rc, log = _run(RECONCILER, '--corpus', corpus, '--exclusions', excl, '--legacy-warn-only',
                   '--out', out)
    assert rc == 0, log
    rc, log = _run(RECONCILER, '--corpus', corpus, '--exclusions', excl, '--legacy-warn-only',
                   '--ground-truth', gt, '--missing-urls-out', tmp_path / 'miss.txt', '--out', out)
    assert rc == 1, log                                  # missing in-scope item still fails
    s = json.loads((out / 'coverage_summary.json').read_text())
    assert s['items']['missing_failing'] == [U + 'missing-one/']
    assert s['items']['item_recall'] == 0.5
    assert s['rubio_rows'] == 2 and s['turns']['rows_recall_below_min_ids'] == ['1']
    assert (tmp_path / 'miss.txt').read_text().splitlines()[-1] == U + 'missing-one/'
    with open(out / 'coverage_turns.csv', newline='', encoding='utf-8') as f:
        turns = {r['Row Id']: r for r in csv.DictReader(f)}
    assert turns['0']['status'] == 'ok' and turns['1']['status'] == 'warn'
    assert (out / 'coverage_report.md').read_text().startswith('## Rubio coverage')


# ---------------------------------------------------------------------------
# Verifier checks AC-AI
# ---------------------------------------------------------------------------

def _verify(*args):
    return _run(VERIFIER, '--max-gap-days', '100000', *args)


@pytest.fixture
def corpora(tmp_path):
    good = _row(0, U + 'good/', f'{R1}\n\n{R2}')
    bad = _row(1, U + 'leaky/', f'{R1}\n\n{MOD}\n\n{R2}', d='2026-03-04')
    base = _write_csv(tmp_path / 'base.csv', [good, bad])
    only_good = _write_csv(tmp_path / 'good.csv', [good])
    return tmp_path, base, only_good


def test_verifier_new_checks_skip_by_default(corpora):
    _, base, _ = corpora
    rc, out = _verify('--input', base)
    assert rc == 0, out
    for c in ('Check AC', 'Checks AD-AG', 'Check AH', 'Check AI'):
        assert f'skip  {c}' in out
    assert 'Check AD:' not in out


def test_verifier_rubio_coverage_new_vs_legacy(corpora, tmp_path):
    _, base, only_good = corpora
    rc, out = _verify('--input', only_good, '--rubio-coverage')
    assert rc == 0, out
    assert 'ok    Check AD' in out and 'ok    Check AE' in out
    rc, out = _verify('--input', base, '--rubio-coverage')       # no baseline: all new
    assert rc == 1 and 'FAIL  Check AE' in out, out
    rc, out = _verify('--input', base, '--rubio-coverage', '--baseline', base)
    assert rc == 0 and 'warn  Check AE' in out, out               # unchanged legacy row
    rc, out = _verify('--input', base, '--strict-rubio-coverage', '--baseline', base)
    assert rc == 1 and 'FAIL  Check AE' in out, out
    al = tmp_path / 'allow.csv'
    al.write_text('row_id,url,reason,expires\n1,,repair in patch release,2099-01-01\n')
    rc, out = _verify('--input', base, '--strict-rubio-coverage', '--coverage-allowlist', al)
    assert rc == 0 and 'allowlisted' in out, out


def test_verifier_ac_ah_ai(corpora, tmp_path):
    _, _, only_good = corpora
    gt = _write_csv(tmp_path / 'gt.csv', [_gt(U + 'good/'), _gt(U + 'missing-one/')],
                    cols=['url_key', 'slug_date_key', 'canonical_url', 'gt_class', 'in_scope',
                          'modality', 'event_date', 'first_seen'])
    excl = tmp_path / 'excl.csv'
    excl.write_text('url_key,slug_date_key,reason,decided_by,date\n')
    rc, out = _verify('--input', only_good, '--ground-truth', gt, '--exclusions', excl)
    assert rc == 1 and 'FAIL  Check AC' in out and 'missing-one' in out, out

    pending = tmp_path / 'pending.csv'
    pending.write_text(f'url,first_seen,attempts,last_error\n{U}missing-one/,2026-01-01,1,fetch_error: 503\n')
    rc, out = _verify('--input', only_good, '--ground-truth', gt, '--exclusions', excl,
                      '--pending', pending, '--as-of', '2026-03-05')
    assert rc == 0 and 'warn  Check AC' in out, out
    assert 'warn  Check AI' in out                            # pending since 2026-01-01

    snap = tmp_path / 'snap.csv'
    snap.write_text('page,pos,href,url_key,type,title,list_date,total_pages,items_header,pass\n'
                    f'1,1,{U}good/,,Remarks,Good,2026-03-03,1,2,1\n'
                    f'1,2,{U}cuban-message/,,Remarks,Cuba,2026-03-02,1,2,1\n'
                    f'1,3,{U}old-item/,,Remarks,Old,2025-12-01,1,2,1\n')
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'last_run': {'since': '2026-02-01'}}))
    rc, out = _verify('--input', only_good, '--listing-snapshot', snap, '--discovery-state', state,
                      '--exclusions', excl)
    assert rc == 1 and 'FAIL  Check AH' in out and 'cuban-message' in out, out
    assert 'old-item' not in out                              # before the window
    excl.write_text('url_key,slug_date_key,reason,decided_by,date\n'
                    'state.gov/releases/office-of-the-spokesman/2026/03/cuban-message,,test,t,2026-10-21\n')
    rc, out = _verify('--input', only_good, '--listing-snapshot', snap, '--discovery-state', state,
                      '--exclusions', excl)
    assert rc == 0 and 'ok    Check AH: all 2 listed' in out, out

    # Snapshots written before list_date became ISO carry the listing's raw
    # 'Month D, YYYY' text: the window must still apply to them.
    raw_snap = tmp_path / 'snap_raw.csv'
    raw_snap.write_text('page,pos,href,url_key,type,title,list_date,total_pages,items_header,pass\n'
                        f'1,1,{U}good/,,Remarks,Good,"March 3, 2026",1,2,1\n'
                        f'1,2,{U}old-item/,,Remarks,Old,"December 1, 2025",1,2,1\n')
    rc, out = _verify('--input', only_good, '--listing-snapshot', raw_snap, '--discovery-state', state,
                      '--exclusions', excl)
    assert rc == 0 and 'ok    Check AH: all 1 listed' in out and 'old-item' not in out, out


def test_fail_lines_parse_for_per_row_gate():
    """corpus_rubio_update._check_name reads 'Check AD' etc. from FAIL lines."""
    import re
    msg = 'Check AD: Rubio-turn recall < 0.98: 1 new/changed row(s), sample [...]'
    assert re.match(r'(Check [A-Z]{1,2})\b', msg).group(1) == 'Check AD'
