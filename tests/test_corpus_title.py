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
    audit, text_changed = cpt.patch_rows(
        rows, {headerless['URL']: 'The U.S. in Asia: Economic Engagement for Good'})
    assert [a['Row Id'] for a in audit] == ['0', '1'] and text_changed == []
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


# ---------------------------------------------------------------------------
# Restoring the sentence strip_leading_title removed
# ---------------------------------------------------------------------------

def test_restore_written_statement_with_ordinal_seam():
    sentence = ('On behalf of the United States of America, I congratulate the people of Latvia '
                'as you celebrate the 104')
    body = 'th\nanniversary of your independence.\nWe value our partnership.'
    raw = (f'Latvia National Day{SUFFIX}\n{CHROME}hide\nLatvia National Day\nPress Statement\n'
           f'Antony J. Blinken, Secretary of State\nNovember 18, 2022\n{sentence}\n{body}\nTags\n')
    row = _row(1, 'https://2021-2025.state.gov/latvia-national-day', 'Latvia National Day', raw,
               sos=body, nat=body)
    out, status = ct.restore_row(row, sentence)
    assert status == {'Natural Text': 'restored', 'SOS Text': 'restored'}
    assert out['SOS Text'] == out['Natural Text']
    assert out['SOS Text'].startswith(sentence + 'th anniversary of your independence.\n')


def test_restore_two_ordinals_does_not_duplicate_the_clause():
    """Row 14570: SOS differs from Natural Text, so its head is searched in the
    restored Natural Text, whose first seam is already rejoined ("66th"). The search
    must still land on the 66th, not the later "11\\nth\\nanniversary"."""
    sentence = ('On behalf of the Government of the United States of America, I would like to '
                'congratulate the Tunisian people on the 66')
    body = ("th\nanniversary of their nation's independence and the 11\nth\n"
            'anniversary of the Tunisian revolution.\nThe bonds between our two countries endure.')
    raw = (f'Tunisia National Day{SUFFIX}\n{CHROME}hide\nTunisia National Day\nPress Statement\n'
           f'Antony J. Blinken, Secretary of State\nMarch 20, 2022\n{sentence}\n{body}\nTags\n')
    row = _row(1, 'https://2021-2025.state.gov/tunisia-national-day', 'Tunisia National Day', raw,
               sos=body.replace('endure.', 'endure. '), nat=body)
    out, status = ct.restore_row(row, sentence)
    assert status == {'Natural Text': 'restored', 'SOS Text': 'restored'}
    want = (sentence + "th anniversary of their nation's independence and the 11\nth\n"
            'anniversary of the Tunisian revolution.')
    assert out['SOS Text'].startswith(want), out['SOS Text'][:200]
    assert out['Natural Text'].startswith(want)


def test_restore_interview_keeps_only_the_secretary_turn():
    raw = ('Interview With Zain Verjee of CNN\nU.S. Department of State\nSecretary Condoleezza Rice\n'
           'Washington, DC\nMay 1, 2008\nQUESTION:\nThanks for joining us.\nSECRETARY RICE:\n'
           'Nice to be with you.\nQUESTION:\nLet us start with Lebanon.\nSECRETARY RICE:\n'
           'Well, first of all, Lebanon matters.\n')
    nat = raw[raw.index('QUESTION:'):]
    row = _row(2, 'https://2001-2009.state.gov/secretary/rm/2008/05/1.htm',
               'Interview With Zain Verjee of CNN', raw,
               sos='Well, first of all, Lebanon matters.', nat=nat, sec='Rice')
    out, status = ct.restore_row(row, 'Nice to be with you.')
    assert status['Natural Text'] == 'present'
    assert out == {'SOS Text': 'Nice to be with you.\n\nWell, first of all, Lebanon matters.'}


def test_restore_declines_other_speaker_and_header_titles():
    raw = ('Remarks at a School\nU.S. Department of State\nMay 1, 2008\nMR. HOST:\n'
           'Please welcome Secretary Condoleezza Rice. (Applause.)\nSECRETARY RICE:\nThank you all.\n')
    nat = raw[raw.index('MR. HOST:'):]
    row = _row(3, 'https://2001-2009.state.gov/secretary/rm/2008/05/2.htm', 'Remarks at a School',
               raw, sos='Thank you all.', nat=nat, sec='Rice')
    out, status = ct.restore_row(row, 'Please welcome Secretary Condoleezza Rice. (Applause.)')
    assert out == {} and status['SOS Text'] == 'not_secretary'
    # an old title from the page header is not speech: nothing to restore
    assert ct.restore_row(row, 'Remarks at a School') == ({}, {})


def test_strip_leading_title_keeps_speech_titles():
    from corpus_finalize_v4_30 import strip_leading_title
    text = 'Glad to be with you, as always.\nNow, on the question of trade.'
    assert strip_leading_title(text, 'Glad to be with you, as always.') == text
    hdr = 'Remarks at the Opening of the Ministerial\nThank you all for coming.'
    assert strip_leading_title(hdr, 'Remarks at the Opening of the Ministerial') == 'Thank you all for coming.'


# ---------------------------------------------------------------------------
# Second-audit findings
# ---------------------------------------------------------------------------

def test_department_of_state_inside_a_title_is_kept():
    raw = ('Secretary of State Michael R. Pompeo at the U.S. Department of State 230th Anniversary '
           f'Celebration{SUFFIX}\nSkip to content\n')
    assert ct.page_title(raw)[0].endswith('U.S. Department of State 230th Anniversary Celebration')
    flat = ('Russia Move into Georgia U.S. Department of State Other State Department Archive Sites '
            'Russia Move into Georgia Statement by Secretary Condoleezza Rice')
    assert ct.page_title(flat) == ('Russia Move into Georgia', 'flat_prefix')
    chrome = ("U.S. Department of State: Secretary Powell Interview on Egypt's Nile Television "
              'U.S. Department of State Other State Department Archive Sites You are in:')
    assert ct.page_title(chrome)[0] == "Secretary Powell Interview on Egypt's Nile Television"


def test_lone_a_circumflex_is_removed():
    assert ct.fix_text('Indian Independence DayÂ') == 'Indian Independence Day'
    assert ct.fix_text('Pakistan Independence Day Â') == 'Pakistan Independence Day'


def test_truncated_heading_is_flagged_and_continued():
    raw = ('4/4/99 Albright op-ed on crisis in Kosovo\nOther State Department Archive Sites\n'
           'Secretary of State Madeleine K. Albright\nOp-Ed on the Crisis in Kosovo in\n'
           'The Washington Post\nApril 4, 1999\nThe crisis in Kosovo is grave.\n')
    row = _row(1, 'https://1997-2001.state.gov/statements/1999/990404a.html',
               'Op-Ed on the Crisis in Kosovo in', raw, sos='The crisis in Kosovo is grave.',
               sec='Albright')
    assert ct.repair_title(row) == ('Op-Ed on the Crisis in Kosovo in The Washington Post',
                                    ['truncated'], 'heading_join')
    raw2 = ("Statement on International Women's Day\nU.S. Department of State\nInternational Women's Day\n"
            'Statement by Secretary Condoleezza Rice\nMarch 7, 2007\nToday, we pay tribute.\n')
    row2 = _row(2, 'https://2001-2009.state.gov/secretary/rm/2007/mar/81497.htm', 'Statement by', raw2,
                sos='Today, we pay tribute.', sec='Rice')
    assert ct.repair_title(row2)[:2] == ("Statement on International Women's Day", ['truncated'])


def test_body_first_line_is_not_a_headline():
    raw = ('The Democratic People’s Republic of Korea (DPRK) continues its unprecedented pace, scale, '
           'and scope of ballistic missile launches this year.\nThis year alone, it has launched more.')
    assert ct.page_title(raw) == ('', '')


def test_transcript_line_titles_are_speech():
    raw = 'SECRETARY BLINKEN: Marc, good morning.\nFOREIGN MINISTER GARNEAU: Good morning, Tony.\n'
    assert ct.raw_header(raw) == ''
    row = _row(3, 'https://2021-2025.state.gov/x-before-their-meeting-2', 'Marc, good morning.', raw,
               sos='Marc, good morning.')
    assert 'speech_sentence' in ct.title_problems(row)
    deep = ('The InterAction 2004 Annual Forum Luncheon\nU.S. Department of State\nMay 1, 2004\n'
            + 'Nav line\n' * 3000 + 'SECRETARY POWELL:\nDid you see that? An NGO.\nQUESTION:\nThanks.\n')
    row2 = _row(4, 'https://2001-2009.state.gov/secretary/former/powell/remarks/32580.htm',
                'Did you see that? An NGO.', deep, sos='Thank you all.', sec='Powell')
    assert 'speech_sentence' in ct.title_problems(row2)
    assert ct.repair_title(row2)[0] == 'The InterAction 2004 Annual Forum Luncheon'


def test_stage_direction_chrome_and_mojibake_titles():
    assert ct.page_title('(Via interpreter.)\nQUESTION : Good evening.\n') == ('', '')
    raw = ("U.S. Department of State: Secretary Powell Interview on Egypt's Nile Television "
           'U.S. Department of State Other State Department Archive Sites You are in: x')
    row = _row(1, 'https://2001-2009.state.gov/secretary/former/powell/remarks/2003/20660.htm',
               "U.S. Department of State: Secretary Powell Interview on Egypt's Nile Television "
               'U.S. Department of State', raw, sos='Thank you.', sec='Powell')
    assert ct.repair_title(row)[:2] == ("Secretary Powell Interview on Egypt's Nile Television",
                                        ['site_chrome'])
    assert 'mojibake' in ct.bad_title_reasons('Terrorist Attack in Bnei BrakÂ', 'https://x/y')

