"""corpus_rubio_update: plan steps 6a (merge side), 6b and 6d.

Offline: a synthetic 320-row corpus (enough distinct cities for
build_gazetteer's sanity floor) and a hand-written backfill CSV. The real
verifier runs as a subprocess, exactly as in CI; NER is skipped (--skip-ner)
so no spaCy model is needed.
"""
import csv
import os
import subprocess
import sys
from pathlib import Path

import pytest

import corpus_rubio_update as cru
from corpus_enrich_v1 import OUTPUT_COLUMNS, backfill_canonical_urls, enrich_row

SCRIPTS = Path(__file__).resolve().parent.parent / 'scripts'
VERIFIER = SCRIPTS / 'corpus_verify_research_grade.py'

csv.field_size_limit(10 ** 8)


# ── 6d: date fallback ────────────────────────────────────────────────────────

@pytest.mark.parametrize('rec, expected', [
    ({'Date': '2026-09-04', 'url': 'https://www.state.gov/x/'}, ('2026-09-04', 'scraped')),
    ({'Date': '', 'listing_date': 'Sept. 4, 2026', 'url': 'https://www.state.gov/x/'},
     ('2026-09-04', 'listing_date')),
    ({'Date': '', 'listing_date': 'September 4, 2026', 'url': ''}, ('2026-09-04', 'listing_date')),
    ({'Date': '', 'listing_date': '2026-09-04', 'url': ''}, ('2026-09-04', 'listing_date')),
    ({'Date': '', 'url': 'https://www.state.gov/releases/2026/05/foo/'}, ('2026-05-01', 'url_month')),
    ({'Date': 'garbage', 'listing_date': 'nope', 'url': 'https://www.state.gov/foo/'}, ('', 'undated')),
    ({'url': 'https://www.state.gov/releases/2026/13/foo/'}, ('', 'undated')),
])
def test_resolve_date(rec, expected):
    assert cru._resolve_date(rec) == expected


# ── 6b: held list bookkeeping ────────────────────────────────────────────────

def test_update_held_carries_first_held_and_attempts():
    prev = {
        cru._canonical_url_key('https://www.state.gov/a/'):
            {'url': 'https://www.state.gov/a/', 'check': 'Check M', 'detail': 'x',
             'first_held': '2026-10-01', 'attempts': '2'},
        cru._canonical_url_key('https://www.state.gov/b/'):
            {'url': 'https://www.state.gov/b/', 'check': 'undated', 'detail': 'y',
             'first_held': '2026-10-08', 'attempts': '1'},
        cru._canonical_url_key('https://www.state.gov/c/'):
            {'url': 'https://www.state.gov/c/', 'check': 'undated', 'detail': 'z',
             'first_held': '2026-10-08', 'attempts': '1'},
    }
    held_now = [{'url': 'https://www.state.gov/a/', 'check': 'Check M', 'detail': 'x2'},
                {'url': 'https://www.state.gov/d/', 'check': 'undated', 'detail': 'w'}]
    # b was seen this run and not held again (merged); c was not seen at all.
    seen = {cru._canonical_url_key(u) for u in
            ('https://www.state.gov/a/', 'https://www.state.gov/b/', 'https://www.state.gov/d/')}
    out = {r['url']: r for r in cru._update_held(prev, held_now, seen, '2026-10-21')}
    assert set(out) == {'https://www.state.gov/a/', 'https://www.state.gov/c/',
                        'https://www.state.gov/d/'}
    assert out['https://www.state.gov/a/']['first_held'] == '2026-10-01'
    assert out['https://www.state.gov/a/']['attempts'] == '3'
    assert out['https://www.state.gov/a/']['detail'] == 'x2'
    assert out['https://www.state.gov/c/']['attempts'] == '1'      # untouched
    assert out['https://www.state.gov/d/'] == {
        'url': 'https://www.state.gov/d/', 'check': 'undated', 'detail': 'w',
        'first_held': '2026-10-21', 'attempts': '1'}


def test_check_name():
    assert cru._check_name('Check M: x') == 'Check M'
    assert cru._check_name('Check AB: "ã——" separator') == 'Check AB'
    assert cru._check_name('Empty Date: 1 rows, sample [0]') == 'Empty Date'


# ── 6b: gate with a fake verifier ────────────────────────────────────────────

def test_gate_batch_pass_runs_once():
    calls = []

    def runner(verifier, path, extra):
        calls.append(path.name)
        return 0, 'PASSED'
    assert cru.gate_new_rows([{'URL': 'a'}, {'URL': 'b'}], VERIFIER, runner=runner) == {}
    assert calls == ['batch.csv']


def test_gate_isolates_failing_row_and_ignores_cross_row():
    def runner(verifier, path, extra):
        with open(path, newline='', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        assert [r['Row Id'] for r in rows] == [str(i) for i in range(len(rows))]
        assert '--max-gap-days' in extra
        if len(rows) > 1:
            return 1, '  FAIL  Check Z: cross-row\n'
        if rows[0]['URL'] == 'bad':
            return 1, '  ok    fine\n  FAIL  Check M: byline\n  FAIL  Empty Date: 1 rows\n'
        return 0, 'PASSED'
    rows = [{'URL': 'good', 'Row Id': '900'}, {'URL': 'bad', 'Row Id': '901'}]
    out = cru.gate_new_rows(rows, VERIFIER, runner=runner)
    assert out == {1: ['Check M: byline', 'Empty Date: 1 rows']}
    assert rows[0]['Row Id'] == '900'       # caller's rows not mutated

    # Batch fails but no single row does -> nothing held (left to the hard gate).
    def cross_only(verifier, path, extra):
        with open(path, newline='', encoding='utf-8') as f:
            n = sum(1 for _ in csv.DictReader(f))
        return (1, '  FAIL  Check Z: dup\n') if n > 1 else (0, '')
    assert cru.gate_new_rows([{'URL': 'x'}, {'URL': 'y'}], VERIFIER, runner=cross_only) == {}


# ── end to end: real verifier, synthetic corpus ──────────────────────────────

_WORDS = ('alliance trade security partnership region dialogue cooperation '
          'stability prosperity commitment').split()


def _text(i: int) -> str:
    body = ' '.join(_WORDS[(i + k) % len(_WORDS)] for k in range(40))
    return f'Thank you all for being here on day number {i}. {body}.'


def _synthetic_corpus(path: Path, n: int = 320) -> None:
    from datetime import date, timedelta
    start = date(2025, 2, 1)
    rows = []
    for i in range(n):
        t = _text(i)
        rows.append({
            'Row Id': str(i), 'Secretary': 'Rubio',
            'Date': (start + timedelta(days=i)).isoformat(),
            'Title': f'Remarks number {i}', 'Speech Type': 'Remarks',
            'Location': f'Townville{i}, Freedonia' if i % 2 else f'Townville{i}, Sylvania',
            'URL': f'https://www.state.gov/synthetic-remarks-{i}/',
            'SOS Text': t, 'Natural Text': t, 'Raw Text': t,
        })
    rows[0]['Location'] = 'Jerusalem, Israel'
    rows[1]['Location'] = 'Washington, D.C.'
    for r in rows:
        enrich_row(r, incremental=False)
        for c in OUTPUT_COLUMNS:
            r.setdefault(c, '')
    backfill_canonical_urls(rows)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


_BF_COLS = ['url', 'Date', 'listing_date', 'Title', 'speech_type', 'Location',
            'speech_text', 'speaker_text', 'raw_text']


def _bf(url, date_, title, loc, i, *, raw=None, listing=''):
    t = _text(1000 + i)
    return {'url': url, 'Date': date_, 'listing_date': listing, 'Title': title,
            'speech_type': 'remarks', 'Location': loc, 'speech_text': t,
            'speaker_text': t, 'raw_text': raw if raw is not None else t}


def _run(tmp_path, backfill_rows, extra=()):
    corpus = tmp_path / 'corpus.csv'
    if not corpus.exists():
        _synthetic_corpus(corpus)
    bf = tmp_path / 'rubio_new.csv'
    with open(bf, 'w', newline='', encoding='utf-8') as f:
        extra_cols = [k for r in backfill_rows for k in r if k not in _BF_COLS]
        w = csv.DictWriter(f, fieldnames=_BF_COLS + list(dict.fromkeys(extra_cols)))
        w.writeheader()
        w.writerows(backfill_rows)
    out = tmp_path / 'corpus_updated.csv'
    if out.exists():
        out.unlink()
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / 'corpus_rubio_update.py'),
         '--corpus', str(corpus), '--backfill', str(bf), '--output', str(out),
         '--max-gap-days', '100000', '--skip-ner', *extra],
        capture_output=True, text=True,
        env={k: v for k, v in os.environ.items() if k != 'GITHUB_OUTPUT'},
    )
    return proc, out


def _read(path):
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def test_end_to_end_provisional_held_and_undated(tmp_path):
    rows = [
        # merged, strict Location
        _bf('https://www.state.gov/new-good/', '2026-10-01', 'Good remarks',
            'Prime Minister’s Office, Jerusalem', 1),
        # merged with a provisional Location (used to be quarantined = dropped)
        _bf('https://www.state.gov/new-byline/', '2026-10-02', 'Joint statement',
            'Robert F. Kennedy, Secretary of Health and Human Services', 2),
        # no Date, but a listing date -> merged
        _bf('https://www.state.gov/new-listing-dated/', '', 'Listing dated',
            'Washington, D.C.', 3, listing='Sept. 30, 2026'),
        # no date anywhere -> held 'undated'
        _bf('https://www.state.gov/new-undated/', '', 'Undated', 'Washington, D.C.', 4),
        # Raw Text byline names a Deputy Secretary -> verifier Check M fails
        # on this row alone -> held, the rest still released
        _bf('https://www.state.gov/new-wrong-byline/', '2026-10-03', 'Wrong byline',
            'Washington, D.C.', 5,
            raw='Share Remarks Paul Jones Deputy Secretary of State ' + _text(5)),
    ]
    proc, out = _run(tmp_path, rows)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    merged = _read(out)
    assert len(merged) == 320 + 3
    new = {r['URL']: r for r in merged[320:]}
    assert new['https://www.state.gov/new-good/']['Location'] == 'Jerusalem, Israel'
    assert new['https://www.state.gov/new-byline/']['Location'] == 'Unknown, Unknown'
    assert new['https://www.state.gov/new-listing-dated/']['Date'] == '2026-09-30'
    # Published schema unchanged (provenance is sidecar-only).
    assert list(merged[0].keys()) == OUTPUT_COLUMNS

    prov = {r['URL']: r for r in _read(tmp_path / 'rubio_merge_provenance.csv')}
    assert prov['https://www.state.gov/new-byline/']['Location Provisional'] == '1'
    assert prov['https://www.state.gov/new-byline/']['Location Method'] == 'provisional_unknown'
    assert prov['https://www.state.gov/new-good/']['Location Provisional'] == '0'
    assert prov['https://www.state.gov/new-listing-dated/']['Date Method'] == 'listing_date'
    assert prov['https://www.state.gov/new-good/']['Row Id'] == new['https://www.state.gov/new-good/']['Row Id']

    q = _read(tmp_path / 'rubio_unresolved_location.csv')
    assert [r['disposition'] for r in q] == ['merged_provisional']

    held = {r['url']: r for r in _read(tmp_path / 'rubio_held.csv')}
    assert set(held) == {'https://www.state.gov/new-undated/',
                         'https://www.state.gov/new-wrong-byline/'}
    assert held['https://www.state.gov/new-undated/']['check'] == 'undated'
    assert 'Check M' not in held['https://www.state.gov/new-undated/']['check']
    assert held['https://www.state.gov/new-wrong-byline/']['check'].startswith('Raw Text byline')
    assert all(r['attempts'] == '1' for r in held.values())
    assert (tmp_path / 'rubio_held.flag').read_text().strip() == '2'

    # Next run: the held URLs are fed back; the undated one now has a date.
    rows2 = [_bf('https://www.state.gov/new-undated/', '2026-10-04', 'Undated', 'Washington, D.C.', 4),
             rows[4]]
    held_first = held['https://www.state.gov/new-wrong-byline/']['first_held']
    proc, out = _run(tmp_path, rows2)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    held2 = {r['url']: r for r in _read(tmp_path / 'rubio_held.csv')}
    assert set(held2) == {'https://www.state.gov/new-wrong-byline/'}
    assert held2['https://www.state.gov/new-wrong-byline/']['attempts'] == '2'
    assert held2['https://www.state.gov/new-wrong-byline/']['first_held'] == held_first


def test_end_to_end_all_held_still_writes_retry_list(tmp_path):
    proc, out = _run(tmp_path, [_bf('https://www.state.gov/only-undated/', '', 'U', '', 9)],
                     extra=('--held-out', str(tmp_path / 'h.csv'),
                            '--held-flag', str(tmp_path / 'h.flag')))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not out.exists()
    assert [r['url'] for r in _read(tmp_path / 'h.csv')] == ['https://www.state.gov/only-undated/']
    assert (tmp_path / 'h.flag').exists()


def test_end_to_end_clean_run_removes_flag(tmp_path):
    flag = tmp_path / 'rubio_held.flag'
    flag.write_text('3\n')
    proc, out = _run(tmp_path, [_bf('https://www.state.gov/fine/', '2026-10-05', 'Fine',
                                    'Ben Gurion International Airport', 7)])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not flag.exists()
    assert _read(out)[-1]['Location'] == 'Tel Aviv, Israel'
    assert _read(tmp_path / 'rubio_held.csv') == []


# ── Review fixes: Media Note, Modality/Untranscribed sidecar, X-09, held provisional ──

def test_map_backfill_row_media_note_and_unmapped_fallback():
    assert cru._map_backfill_row({'speech_type': 'media note'}, 1)['Speech Type'] == 'Remarks'  # no 'Media Note' type; unmapped -> Remarks
    assert cru._map_backfill_row({'speech_type': 'press statement'}, 1)['Speech Type'] == 'Press Statement'
    assert cru._map_backfill_row({'speech_type': 'mystery'}, 1)['Speech Type'] == 'Remarks'
    # The shared map is not touched (other secretaries' path unchanged).
    assert 'media note' not in cru.SPEECH_TYPE_TITLECASE


def test_clean_new_row_strips_via_translation_like_reextract():
    import rubio_reextract as rr
    t = 'Hello everyone. (Via translation) We are here today for the people of Cuba.'
    row = cru._clean_new_row({'Title': 'T', 'SOS Text': t, 'Natural Text': t, 'Raw Text': t})
    assert 'Via translation' not in row['SOS Text']
    assert row['SOS Text'] == rr.clean_sos_text(t, 'T')


def test_end_to_end_media_note_modality_and_held_provisional(tmp_path):
    good = dict(_bf('https://www.state.gov/new-media-note/', '2026-10-01', 'Message to Cuba',
                    'Washington, D.C.', 11),
                speech_type='media note', modality='video', untranscribed_rubio_segments='2')
    # Unresolvable Location (-> provisional) AND a wrong-byline Raw Text (-> held).
    bad = dict(_bf('https://www.state.gov/new-prov-held/', '2026-10-02', 'Prov held',
                   'Robert F. Kennedy, Secretary of Health and Human Services', 12,
                   raw='Share Remarks Paul Jones Deputy Secretary of State ' + _text(12)),
               modality='spoken', untranscribed_rubio_segments='0')
    proc, out = _run(tmp_path, [good, bad])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    merged = _read(out)
    assert len(merged) == 321 and list(merged[0].keys()) == OUTPUT_COLUMNS
    assert merged[-1]['Speech Type'] == 'Remarks'
    prov = _read(tmp_path / 'rubio_merge_provenance.csv')
    assert len(prov) == 1
    assert prov[0]['Modality'] == 'video' and prov[0]['Untranscribed Rubio Segments'] == '2'
    assert prov[0]['Speech Type'] == 'Remarks'
    # The provisional row was held, so the sidecar must not say it was merged.
    q = _read(tmp_path / 'rubio_unresolved_location.csv')
    assert [(r['url'], r['disposition']) for r in q] == [
        ('https://www.state.gov/new-prov-held/', 'held')]
    assert sum(r['Location Provisional'] == '1' for r in prov) == 0
