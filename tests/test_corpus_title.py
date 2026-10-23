"""Title column: headline recovery, bad-title detection, the patch and Check AJ.

Fixtures are the shapes found in v2026.10.19 (docs/title_repair.md).
"""

from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / 'scripts'
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import corpus_title as ct  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'backfill'
VERIFIER = _SCRIPTS / 'corpus_verify_research_grade.py'

SUFFIX = ' - United States Department of State'
CHROME = 'Skip to content\nAn official website of the United States Government\n'


def _modern_raw(headline: str, body: str, stype: str = 'Remarks', title: str | None = None) -> str:
    """2021+ page: <title>, site chrome, then the hide block."""
    return (f'{title or headline}{SUFFIX}\n{CHROME}Newsroom\nhide\n{headline}\n{stype}\n'
            f'Antony J. Blinken, Secretary of State\nJuly 4, 2023\n{body}\nTags\n')


MEETING = 'Secretary Antony J. Blinken Meet and Greet with Mission Kuwait'
MEETING_URL = 'https://2021-2025.state.gov/secretary-antony-j-blinken-meet-and-greet-with-mission-kuwait/'
MEETING_SPEECH = 'Well, thank you all very much. It is good to be here.'
MEETING_BODY = f'SECRETARY BLINKEN: {MEETING_SPEECH}'

KAZ = 'Kazakhstan’s Republic Day'
KAZ_URL = 'https://2021-2025.state.gov/kazakhstans-republic-day'
KAZ_BODY = ('On behalf of the United States of America, I congratulate the people of Kazakhstan '
            'on your Republic Day. We value our partnership.')


def _row(rid, url, title, raw, sos='', nat='', sec='Blinken'):
    return {'Row Id': str(rid), 'Secretary': sec, 'Date': '2023-07-04', 'Title': title,
            'Speech Type': 'Remarks', 'Location': 'Washington, D.C.', 'URL': url,
            'SOS Text': sos, 'Natural Text': nat or sos, 'Raw Text': raw}


# ---------------------------------------------------------------------------
# page_title
# ---------------------------------------------------------------------------

def test_page_title_hide_block_and_line1():
    assert ct.page_title(_modern_raw(MEETING, MEETING_BODY)) == (MEETING, 'hide_block')
    old = ('Interview With Juan Williams of National Public Radio\nU.S. Department of State\n'
           'Other State Department Archive Sites\nSECRETARY RICE: Glad to be with you.')
    assert ct.page_title(old) == ('Interview With Juan Williams of National Public Radio', 'line1')


def test_page_title_prefers_the_fuller_of_title_and_headline():
    # <title> extends the on-page headline -> the <title>
    raw = _modern_raw('Remarks to the Press', MEETING_BODY,
                      title='Secretary Antony J. Blinken Remarks to the Press')
    assert ct.page_title(raw)[0] == 'Secretary Antony J. Blinken Remarks to the Press'
    # <title> is a shortened headline -> the headline
    raw = _modern_raw('Secretary of State Marco Rubio and Italian Deputy Prime Minister and Foreign '
                      'Minister Antonio Tajani at a Signing Ceremony', MEETING_BODY,
                      title='Secretary Rubio and Italian Deputy PM and FM Antonio Tajani at a Signing Ceremony')
    assert ct.page_title(raw)[0].startswith('Secretary of State Marco Rubio and Italian Deputy Prime')


def test_page_title_flattened_raw_and_date_prefix():
    flat = 'El Salvador Presidential Elections Skip to Main Content Jump to In This Section Press'
    assert ct.page_title(flat) == ('El Salvador Presidential Elections', 'flat_prefix')
    flat2 = f'Interview With Rich Edson of Fox News{SUFFIX} Skip to content An official website'
    assert ct.page_title(flat2)[0] == 'Interview With Rich Edson of Fox News'
    albright = '8/10/98 Albright on CBS Evening News\nOther State Department Archive Sites\n'
    assert ct.page_title(albright)[0] == 'Albright on CBS Evening News'


def test_page_title_rejects_bare_type_and_transcript_start():
    assert ct.page_title('Remarks\nSkip to content\n') == ('', '')
    assert ct.page_title('SECRETARY GENERAL STOLTENBERG: So Secretary Blinken, welcome.') == ('', '')
    # hide block holding only the type falls through to the <title>
    raw = f'Real Headline Here{SUFFIX}\n{CHROME}hide\nRemarks\nRemarks\nJuly 4, 2023\nBody.\n'
    assert ct.page_title(raw)[0] == 'Real Headline Here'


def test_clean_title_strips_suffix_and_odd_spaces():
    assert ct.clean_title('Hungary\xa0National Day' + SUFFIX) == 'Hungary National Day'
    assert ct.clean_title('On National Disability Employment Awareness Month ') == \
        'On National Disability Employment Awareness Month'


# ---------------------------------------------------------------------------
# bad_title_reasons / repair_title
# ---------------------------------------------------------------------------

def test_slug_title_with_cut_name_is_repaired():
    assert ct.title_from_slug(MEETING_URL) == 'And Greet With Mission Kuwait'
    raw = _modern_raw(MEETING, MEETING_BODY)
    row = _row(1, MEETING_URL, 'And Greet With Mission Kuwait', raw, sos=MEETING_SPEECH)
    new, reasons, source = ct.repair_title(row)
    assert new == MEETING and 'slug_derived' in reasons and 'fragment_start' in reasons
    assert source == 'hide_block'


def test_title_cased_slug_is_repaired_with_corpus_quote_style():
    raw = _modern_raw(KAZ, KAZ_BODY, stype='Press Statement')
    row = _row(2, KAZ_URL, 'Kazakhstans Republic Day', raw, sos=KAZ_BODY)
    new, reasons, _ = ct.repair_title(row)
    assert reasons == ['slug_derived']
    assert new == "Kazakhstan's Republic Day"          # ftfy straightens quotes, as finalize does


def test_speech_sentence_titles():
    # spoken: first answer of an interview
    raw = ('Interview With Juan Williams of National Public Radio\nU.S. Department of State\n'
           'Secretary Condoleezza Rice\nMay 26, 2005\nQUESTION:\nThank you for the time.\n'
           'SECRETARY RICE: Glad to be with you.\n')
    row = _row(3, 'https://2001-2009.state.gov/secretary/rm/2005/46857.htm', 'Glad to be with you.',
               raw, sos='Glad to be with you.', sec='Rice')
    assert ct.bad_title_reasons(row['Title'], row['URL'], (row['SOS Text'],),
                                ct.page_title(raw)[0], raw) == ['speech_sentence']
    assert ct.repair_title(row)[0] == 'Interview With Juan Williams of National Public Radio'
    # not utterance-shaped, still lifted from the speech ("Who wants first?")
    row2 = _row(4, row['URL'], 'Who wants first?', raw.replace('Glad to be with you.', 'Who wants first?'),
                sos='Who wants first? Go ahead.', sec='Powell')
    assert 'speech_sentence' in ct.repair_title(row2)[1]
    # written: the first sentence was MOVED out of the text into the title
    first = KAZ_BODY.split('. ')[0] + '.'
    raw3 = _modern_raw(KAZ, KAZ_BODY, stype='Press Statement')
    row3 = _row(5, KAZ_URL, first, raw3, sos='We value our partnership.')
    new, reasons, _ = ct.repair_title(row3)
    assert reasons == ['speech_sentence'] and new == "Kazakhstan's Republic Day"


def test_legacy_host_keeps_unflagged_heading():
    raw = ('8/10/98 Albright on CBS Evening News\nOther State Department Archive Sites\n'
           'Secretary of State Madeleine K. Albright\nInterview on CBS Evening News\n'
           'Washington, DC\nAugust 10, 1998\nSECRETARY ALBRIGHT: Good evening.\n')
    row = _row(6, 'https://1997-2001.state.gov/statements/1998/980810.html',
               'Interview on CBS Evening News', raw, sos='Good evening.', sec='Albright')
    assert ct.repair_title(row)[0] == ''                 # the fuller in-page heading stays
    assert ct.title_problems(row) == []


def test_split_heading_is_joined():
    raw = ('3/7/98 Albright & Italian ForMin Dini press remarks\nOther State Department Archive Sites\n'
           'Secretary of State Madeleine K. Albright\nAnd Italian Foreign Minister Lamberto Dini\n'
           'Press Remarks\nRome, Italy\nMarch 7, 1998\nSECRETARY ALBRIGHT: Thank you.\n')
    row = _row(7, 'https://1997-2001.state.gov/statements/1998/980307.html',
               'And Italian Foreign Minister Lamberto Dini', raw, sos='Thank you.', sec='Albright')
    assert ct.repair_title(row) == ('Secretary of State Madeleine K. Albright and Italian Foreign '
                                    'Minister Lamberto Dini', ['split_heading'], 'heading_join')
    raw2 = ('x\nSecretary of State Madeleine K. Albright,\nSecretary of Defense William Cohen,\n'
            'and National Security Advisor Sandy Berger\nRemarks\nSECRETARY ALBRIGHT: Hi.\n')
    row2 = _row(8, row['URL'], 'Secretary of Defense William Cohen,', raw2, sos='Hi.', sec='Albright')
    assert ct.repair_title(row2)[0] == ('Secretary of State Madeleine K. Albright, Secretary of Defense '
                                        'William Cohen, and National Security Advisor Sandy Berger')


def test_good_titles_are_left_alone():
    raw = _modern_raw(MEETING, MEETING_BODY)
    row = _row(9, MEETING_URL, MEETING, raw, sos=MEETING_SPEECH)
    assert ct.repair_title(row) == ('', [], 'hide_block')
    assert ct.title_problems(row) == []
    # quote style / NBSP differences are normalisation, not a problem
    row2 = _row(10, KAZ_URL, "Kazakhstan's Republic Day", _modern_raw(KAZ, KAZ_BODY), sos=KAZ_BODY)
    assert ct.title_problems(row2) == []


def test_modern_host_mismatch_is_a_problem():
    raw = _modern_raw('Iran Continues To Sponsor Terrorism', 'The Department certified today.')
    row = _row(11, 'https://2017-2021.state.gov/iran-continues-to-sponsor-terrorism',
               'Trump Administration Undergoing Interagency Review of Iran Deal', raw,
               sos='The Department certified today.', sec='Tillerson')
    assert ct.title_problems(row) == ['headline_mismatch']
    assert ct.repair_title(row)[0] == 'Iran Continues To Sponsor Terrorism'


# ---------------------------------------------------------------------------
# Weekly path and patch
# ---------------------------------------------------------------------------

def test_backfill_page_headline():
    import rubio_backfill as rb
    raw = (FIXTURES / 'cuban_17483_raw.txt').read_text(encoding='utf-8')
    assert rb.page_headline({'raw_text': raw, 'title': 'ignored'}) == \
        'Secretary Rubio’s Message to the Cuban People on Their Independence Day'
    # no headline in Raw Text -> JSON-LD headline, then og:title, then <title>; never a type
    assert rb.page_headline({'raw_text': 'QUESTION: Hi.', 'jsonld_headline': '',
                             'og_title': 'From OG', 'title': 'Remarks'}) == 'From OG'
    assert rb.page_headline({'raw_text': 'QUESTION: Hi.', 'title': 'Remarks' + SUFFIX}) == ''


def test_patch_rows_uses_overrides_and_recomputes_hash():
    import corpus_patch_titles as cpt
    from corpus_enrich_v1 import row_hash
    good = _row(0, MEETING_URL, 'And Greet With Mission Kuwait', _modern_raw(MEETING, MEETING_BODY),
                sos=MEETING_SPEECH)
    headerless = _row(1, 'https://2021-2025.state.gov/the-us-in-asia-economic-engagement-for-good',
                      'The Us In Asia Economic Engagement For Good', 'QUESTION: Hi.', sos='Hi.')
    rows = [good, headerless]
    audit = cpt.patch_rows(rows, {headerless['URL']: 'The U.S. in Asia: Economic Engagement for Good'})
    assert [a['Row Id'] for a in audit] == ['0', '1']
    assert good['Title'] == MEETING and good['Row Hash'] == row_hash(good)
    assert headerless['Title'] == 'The U.S. in Asia: Economic Engagement for Good'
    assert audit[1]['source'] == 'title_overrides.csv' and audit[1]['field'] == 'Title'


# ---------------------------------------------------------------------------
# Verifier Check AJ
# ---------------------------------------------------------------------------

_COLS = ['Row Id', 'Secretary', 'Date', 'Title', 'Speech Type', 'Location', 'URL',
         'SOS Text', 'Natural Text', 'Raw Text']


def _write(path, rows):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=_COLS, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    return path


def _verify(*args):
    r = subprocess.run([sys.executable, str(VERIFIER), '--max-gap-days', '100000', *map(str, args)],
                       capture_output=True, text=True, timeout=300)
    return r.returncode, r.stdout + r.stderr


def test_verifier_check_aj(tmp_path):
    bad = _row(0, MEETING_URL, 'And Greet With Mission Kuwait', _modern_raw(MEETING, MEETING_BODY),
               sos=MEETING_SPEECH)
    fixed = dict(bad, Title=MEETING)
    base = _write(tmp_path / 'bad.csv', [bad])
    good = _write(tmp_path / 'good.csv', [fixed])
    none = tmp_path / 'no_overrides.csv'
    rc, out = _verify('--input', base)
    assert 'skip  Check AJ' in out and 'FAIL  Check AJ' not in out, out
    rc, out = _verify('--input', base, '--titles', '--title-overrides', none)
    assert rc == 1 and 'FAIL  Check AJ' in out, out                  # no baseline: all new
    rc, out = _verify('--input', base, '--titles', '--baseline', base, '--title-overrides', none)
    assert rc == 0 and 'warn  Check AJ' in out, out                  # unchanged legacy title
    rc, out = _verify('--input', base, '--strict-titles', '--baseline', base, '--title-overrides', none)
    assert rc == 1 and 'FAIL  Check AJ' in out, out
    rc, out = _verify('--input', good, '--strict-titles', '--title-overrides', none)
    assert rc == 0 and 'ok    Check AJ' in out, out
    ov = tmp_path / 'ov.csv'
    ov.write_text(f'Row Id,URL,old_title,title,source,reasons,status\n0,{MEETING_URL},x,,,,unresolved\n')
    rc, out = _verify('--input', base, '--strict-titles', '--title-overrides', ov)
    assert rc == 0 and 'listed unresolved' in out, out
