"""Tests for the 2026-10 catch-up patch (corpus_patch_catchup.py) and the verifier's
Check Q handling of an audited patch that changes Date/Location and removes rows."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / 'scripts'
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import corpus_patch_catchup as cpc  # noqa: E402
from test_corpus_title import MEETING, MEETING_SPEECH, _modern_raw, _row, _verify, _write  # noqa: E402


def _rows(n=3):
    return [_row(i, f'https://2021-2025.state.gov/item-{i}/', MEETING,
                 _modern_raw(MEETING, f'SECRETARY BLINKEN: {MEETING_SPEECH} {i}'),
                 sos=f'{MEETING_SPEECH} {i}') for i in range(n)]


def _fix(rid, url, field, value='', reason='r'):
    return {'Row Id': str(rid), 'URL': url, 'field': field, 'value': value, 'reason': reason}


def test_apply_fixes_checks_url_and_audits():
    rows = _rows()
    audit = cpc.apply_fixes(rows, [_fix(1, rows[1]['URL'], 'Location', 'Jerusalem, Israel'),
                                   _fix(2, rows[2]['URL'], 'Speakers Present', 'Rubio')],
                            cpc.HASHED_FIXES)
    assert rows[1]['Location'] == 'Jerusalem, Israel' and 'Speakers Present' not in rows[2]
    assert [(a['Row Id'], a['field'], a['old_value']) for a in audit] == [
        ('1', 'Location', 'Washington, D.C.')]
    with pytest.raises(SystemExit):
        cpc.apply_fixes(rows, [_fix(1, rows[2]['URL'], 'Date', '2025-01-01')], cpc.HASHED_FIXES)


def test_remove_rows_renumbers_and_remaps_audit():
    rows = _rows(4)
    fixes = [_fix(1, rows[1]['URL'], cpc.REMOVED)]
    audit = [{'Row Id': '1', 'field': 'Title'}, {'Row Id': '3', 'field': 'Location'}]
    kept, out = cpc.remove_rows(rows, fixes, audit)
    assert [r['Row Id'] for r in kept] == ['0', '1', '2']
    assert kept[2]['URL'].endswith('item-3/')
    assert ('2', 'Location') in {(a['Row Id'], a['field']) for a in out}
    assert ('1', 'Title') not in {(a['Row Id'], a['field']) for a in out}    # removed row's change
    assert [a['Row Id'] for a in out if a['field'] == cpc.REMOVED] == ['1']   # baseline id


def test_apply_reextract_accepts_scored_or_approved_rows():
    rows = _rows(3)
    diff = {str(i): {'changed': '1', 'accepted': acc, 'old_rubio_recall': '0.9',
                     'new_rubio_recall': '1.0', 'old_foreign_leak_words': '5',
                     'new_foreign_leak_words': '0'}
            for i, acc in enumerate(('1', '0', '0'))}
    texts = {str(i): {'SOS Text': f'new {i}', 'Natural Text': rows[i]['Natural Text']}
             for i in range(3)}
    audit = cpc.apply_reextract(rows, diff, texts, approve={'1'})
    assert [r['SOS Text'] for r in rows] == ['new 0', 'new 1', rows[2]['SOS Text']]
    assert {a['Row Id'] for a in audit} == {'0', '1'}
    assert all(a['field'] == 'SOS Text' for a in audit)


def test_verifier_check_q_accepts_audited_removal_and_location(tmp_path):
    base = _rows(3)
    base_csv = _write(tmp_path / 'base.csv', base)
    cur = [dict(base[0]), dict(base[2], **{'Row Id': '1', 'Location': 'Jerusalem, Israel'})]
    cur_csv = _write(tmp_path / 'cur.csv', cur)
    audit = tmp_path / 'audit.csv'
    with audit.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['Row Id', 'field'])
        w.writeheader()
        w.writerows([{'Row Id': '1', 'field': '(row removed)'}, {'Row Id': '1', 'field': 'Location'}])
    rc, out = _verify('--input', cur_csv, '--baseline', base_csv, '--allow-changed-rows', audit)
    assert rc == 0 and '1 row(s) removed by the audited patch' in out, out
    # without the audit: the Location change and the missing row both fail
    rc, out = _verify('--input', cur_csv, '--baseline', base_csv)
    assert rc == 1 and 'FAIL  Check Q' in out, out
    # removal listed but the Location change not: still fails
    with audit.open('w', newline='') as f:
        f.write('Row Id,field\n1,(row removed)\n')
    rc, out = _verify('--input', cur_csv, '--baseline', base_csv, '--allow-changed-rows', audit)
    assert rc == 1 and 'base columns diverge' in out, out
