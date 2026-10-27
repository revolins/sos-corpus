"""Tests for the 2026-10-27 whole-corpus audit repairs (corpus_audit_repairs.py) and
the Speakers Present label fixes in corpus_enrich_v1."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / 'scripts'
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import corpus_audit_repairs as ar  # noqa: E402
from corpus_enrich_v1 import _titlecase, speakers_present  # noqa: E402


def test_remove_other_turns_inline_and_split_labels():
    """Kerry 8144 / Powell 2062: another speaker's turn left inside SOS Text,
    on the same line or with the label's colon on the next line."""
    nat = ('SECRETARY KERRY: Thank you very much.\nPRESIDENT PERES: We share their pain.\n'
           'SECRETARY KERRY: Indeed we do.\nPRESIDENT JIANG\n: Mr. Tang is younger than you.\n'
           'SECRETARY KERRY: He is.')
    sos = ('Thank you very much. PRESIDENT PERES: We share their pain.\n\nIndeed we do.\n'
           'PRESIDENT JIANG\n: Mr. Tang is younger than you.\n\nHe is.')
    out = ar.remove_other_turns(sos, nat, 'Kerry')
    assert 'PERES' not in out and 'share their pain' not in out
    assert 'JIANG' not in out and 'younger' not in out
    assert out.startswith('Thank you very much.') and 'Indeed we do.' in out and out.endswith('He is.')


def test_remove_other_turns_leaves_clean_text_untouched():
    sos = 'Thank you.  Good  to be here.'
    nat = 'SECRETARY RICE: Thank you.  Good  to be here.\nQUESTION: Welcome.'
    assert ar.remove_other_turns(sos, nat, 'Rice') == sos


def test_strip_combined_stage_directions():
    t = 'Thank you. (Applause and cheers.) And now (Laughter, applause.) we go. (crosstalk) Yes.'
    assert ar.strip_combined_stage(t) == 'Thank you. And now we go. Yes.'
    assert ar.strip_combined_stage('No stage here.') == 'No stage here.'


def test_restore_cut_start_completes_only_the_cut_word():
    raw = 'Remarks\nSECRETARY POMPEO: Good morning, everyone. It is great to be with you.\n'
    assert ar.restore_cut_start('d morning, everyone. It is great to be with you.', raw) == \
        'Good morning, everyone. It is great to be with you.'
    # a start at a word boundary is left alone: what precedes it may be another speaker's
    raw2 = 'FOREIGN MINISTER X: Secretary Albright has mentioned an international conference.\n'
    assert ar.restore_cut_start('has mentioned an international conference.', raw2) is None


def test_extend_natural_over_the_sos_span():
    raw = ('Header\nSECRETARY ALBRIGHT: one two three four five six seven eight nine ten.\n'
           'QUESTION: alpha beta gamma delta epsilon zeta eta theta?\n'
           'SECRETARY ALBRIGHT: eleven twelve thirteen fourteen fifteen sixteen seventeen '
           'eighteen nineteen twenty.\nFooter')
    nat = 'SECRETARY ALBRIGHT: one two three four five six seven eight nine ten.'
    sos = ('one two three four five six seven eight nine ten. eleven twelve thirteen fourteen '
           'fifteen sixteen seventeen eighteen nineteen twenty.')
    out = ar.extend_natural(nat, sos, raw)
    assert out.startswith('SECRETARY ALBRIGHT: one') and out.endswith('twenty.')
    assert 'QUESTION: alpha' in out and 'Header' not in out and 'Footer' not in out


def test_fix_replacement_chars_known_and_trailing():
    raw = 'the poetry of Petï¿½fi and more. Thanks, appreciate it.ï¿½ So'
    t = "the poetry of Pet�'fi and more. Thanks, appreciate it.��� So"
    assert ar.fix_replacement_chars(t, raw) == 'the poetry of Petőfi and more. Thanks, appreciate it. So'


def test_strip_chrome_head_and_footer():
    t = ('Michael R. Pompeo, Secretary of State June 10, 2020 Share this page on: I am proud. '
         'The Office of Website Management, Bureau of Public Affairs, manages this site as a portal.')
    assert ar.strip_chrome(t) == 'I am proud.'


def test_dedupe_paragraph_only_when_the_page_has_it_once():
    p = 'What a great welcome. Thank you all. How are you? Everybody good? That is a lousy answer.'
    text = f'{p}\nMiddle.\n{p}'
    assert ar.dedupe_paragraphs(text, f'page {p} Middle.') == f'{p}\nMiddle.'
    assert ar.dedupe_paragraphs(text, f'{p} Middle. {p}') == text


def test_repair_row_reports_what_fired():
    row = {'Secretary': 'Kerry', 'Raw Text': 'x',
           'SOS Text': 'Thank you. (Applause and cheers.)',
           'Natural Text': 'SECRETARY KERRY: Thank you. (Applause and cheers.)'}
    out, fired = ar.repair_row(row)
    assert out == {'SOS Text': 'Thank you.'} and fired == ['stage_directions']


def test_speaker_label_fixes():
    assert _titlecase("FOREIGN MINISTER SA'AR") == "Foreign Minister Sa'ar"
    assert _titlecase("PRIME MINISTER D'ALEMA") == "Prime Minister D'Alema"
    assert _titlecase("MR. O'BRIEN") == "Mr. O'Brien"
    nat = ('SECRETARY POWELL: I think Secretary Rumsfeld would tell you. MR. DONALDSON: '
           'Thank you.\nI.  INTRODUCTION: The report covers this.\nSECRETARY POWELL: Yes.')
    sp = speakers_present(nat, 'Powell')
    assert 'Rumsfeld Would Tell You' not in sp and 'Introduction' not in sp
    assert 'Donaldson' in sp and 'Powell' in sp


def test_remove_inline_other_turns_mid_line_and_stage_notes():
    """Rice 3771 / Pompeo 11661: the other speaker's label is mid-line in Natural Text
    too, and Natural Text keeps "(Via interpreter)" that SOS Text dropped."""
    nat = ('SECRETARY RICE: Something the President has told him. PRESIDENT TOLEDO: '
           "(Via interpreter) That's right. SECRETARY RICE: Mr. President, thank you.\n"
           'PRESIDENT ABBAS\n(in Arabic): We are not telling a secret. SECRETARY RICE: Indeed.')
    sos = ("Something the President has told him. PRESIDENT TOLEDO: That's right.\n\n"
           'Mr. President, thank you. PRESIDENT ABBAS : We are not telling a secret.\n\nIndeed.')
    out = ar.remove_inline_other_turns(sos, nat, 'Rice')
    assert 'TOLEDO' not in out and "That's right" not in out
    assert 'ABBAS' not in out and 'secret' not in out
    assert 'Mr. President, thank you.' in out and out.endswith('Indeed.')


def test_remove_inline_other_turns_longest_opening():
    """Clinton 5957: the old extraction mangled the last words of the moderator's turn;
    its 20+ word opening is still removed."""
    turn = ' '.join(f'w{i}' for i in range(30))
    nat = f'SECRETARY CLINTON: Hello. MR. TRAN: {turn} end words here. SECRETARY CLINTON: Good.'
    sos = f'Hello. MR. TRAN: {turn} Assistant Good.'
    out = ar.remove_inline_other_turns(sos, nat, 'Clinton')
    assert 'TRAN' not in out and 'w29' not in out and out.startswith('Hello.')


def test_known_garbles():
    row = {'Secretary': 'Rice', 'Raw Text': '',
           'SOS Text': 'toward theQUESTION: uartet and alQUESTION: aeda.',
           'Natural Text': 'toward theQUESTION: uartet and alQUESTION: aeda.'}
    out, fired = ar.repair_row(row)
    assert out['SOS Text'] == 'toward the Quartet and al-Qaeda.' and 'known_garbles' in fired


def test_not_labels_are_left_alone():
    """Rubio 17657 "... we face in the U.S.: the same networks", Clinton 7053 "the
    OSCE: lasting peace", Clinton 5976 "BEGIN TEXT:" -- none is another speaker."""
    nat = ('SECRETARY RUBIO: the same things that we face in the U.S.: the same networks.\n'
           'QUESTION: Thanks.\nSECRETARY RUBIO: at the heart of the OSCE: lasting peace.')
    sos = 'the same things that we face in the U.S.: the same networks.\n\nat the heart of the OSCE: lasting peace.'
    assert ar.remove_inline_other_turns(sos, nat, 'Rubio') == sos
    stmt = 'Joint statement on the study:\nBEGIN TEXT:\nThe study was clearly unethical. We are outraged.'
    assert ar.remove_other_turns(stmt, stmt + '\nSECRETARY CLINTON: x', 'Clinton') == stmt


def test_remove_foreign_paragraphs_vocative_question():
    """The old extraction took "Secretary Kerry" in a question for his label, so the
    rest of the question became SOS Text."""
    nat = ('SECRETARY KERRY: Thank you all.\nQUESTION:\nSecretary Kerry, is there any more news '
           'on Syria today?\nSECRETARY KERRY:\nThe basic ceasefire is holding, but fragile.')
    sos = 'Thank you all.\n\n, is there any more news on Syria today?\n\nThe basic ceasefire is holding, but fragile.'
    out = ar.remove_foreign_paragraphs(sos, nat, 'Kerry')
    assert out == 'Thank you all.\n\nThe basic ceasefire is holding, but fragile.'
    # the Secretary's label after a dash mid-line is still his
    nat2 = 'MR. RUSSERT: So we would meet you - SECRETARY ALBRIGHT: We really have not determined that.'
    sos2 = 'We really have not determined that.'
    assert ar.remove_foreign_paragraphs(sos2, nat2, 'Albright') == sos2


def test_restore_statement_start_from_release_line():
    raw = ('Secretary of State Madeleine K. Albright Statement on death of Israeli school children '
           'Washington, D.C., March 13, 1997 As released by the Office of the Spokesman U.S. Department '
           'of State I would like to add my own feelings of shock. Any violent act of this magnitude '
           'inspires outrage and sorrow. Released on March 13')
    text = 'act of this magnitude inspires outrage and sorrow.'
    out = ar.restore_statement_start(text, raw)
    assert out == 'I would like to add my own feelings of shock. Any violent act of this magnitude inspires outrage and sorrow.'
    assert ar.restore_statement_start('(In progress) words', raw) is None


def test_restore_turn_head_and_foreign_fragment():
    nat = 'SECRETARY POMPEO:\nGood morning, everyone. Welcome to the State Department today.'
    assert ar.restore_turn_head('morning, everyone. Welcome to the State Department today.', nat, 'Pompeo') == \
        'Good morning, everyone. Welcome to the State Department today.'
    nat2 = ('MINISTER PARK: We discussed many issues today. Secretary Albright and I had an extensive '
            'discussion on other issues of mutual concern.\nSECRETARY ALBRIGHT: Thank you very much. I am delighted.')
    sos2 = 'and I had an extensive discussion on other issues of mutual concern.\n\nThank you very much. I am delighted.'
    assert ar.drop_leading_foreign_fragment(sos2, nat2, 'Albright') == 'Thank you very much. I am delighted.'


def test_sos_from_natural_when_sos_is_another_page():
    raw = 'Header\nRussia held an election in an environment of repression and fear across the country. Footer'
    nat = 'Russia held an election in an environment of repression and fear across the country.'
    sos = ('Thank you very much President Yoon for hosting this third Summit for Democracy and for the '
           'extraordinary spirit we just saw a few moments ago in this hall today.')
    assert ar.sos_from_natural(sos, nat, raw, 'Blinken') == nat
    assert ar.sos_from_natural(nat, nat, raw, 'Blinken') is None


def test_not_a_person_filter():
    from corpus_enrich_v1 import filter_people, is_not_a_person
    assert filter_people('Obama; Quartet; Sierra Leone; al-Sisi; these Abraham Accords; Ally of; Lloyd Austin') == \
        'Obama; al-Sisi; Lloyd Austin'
    assert not is_not_a_person('bin Laden') and not is_not_a_person('de Klerk')


def test_distribution_audit_smoke(tmp_path):
    import corpus_distribution_audit as da
    rows = [{'Row Id': str(i), 'Secretary': 'Kerry', 'Title': f't{i}', 'Speech Type': 'Remarks',
             'Location': 'Washington, D.C.', 'Date': '2014-01-01',
             'SOS Text': f'word{i} ' + 'the quick brown fox jumps over the lazy dog again ' * 3,
             'Natural Text': f'word{i} ' + 'the quick brown fox jumps over the lazy dog again ' * 3,
             'Raw Text': f'word{i} ' + 'the quick brown fox jumps over the lazy dog again ' * 3,
             'Speakers Present': 'Kerry', 'People Mentioned (Natural Text)': '',
             'People Mentioned (SOS Text)': '', 'Countries Mentioned': ''} for i in range(30)]
    summary = da.audit(rows, tmp_path, boiler_min_rows=25)
    assert 'boilerplate 8-grams in >= 25 rows: ' in summary
    assert (tmp_path / 'rows.csv').exists() and (tmp_path / 'near_duplicates.csv').exists()


def test_secretary_label_typos_and_one_word_labels():
    """821 "SECRETARY ABRIGHT", Rubio 17658 "SECRETARY RUBI", Powell 1739 "SECRETARY :"."""
    assert ar.is_secretary_label('SECRETARY ABRIGHT', 'Albright')
    assert ar.is_secretary_label('SECRETARY RUBI', 'Rubio')
    assert ar.is_secretary_label('SECREATRY RUBIO', 'Rubio')
    assert ar.is_secretary_label('SECRETARY', 'Powell')
    assert not ar.is_secretary_label('MRS RUBIO', 'Rubio')
    assert not ar.is_secretary_label('SECRETARY RUBIN', 'Albright')
    assert not ar.is_secretary_label('PRESIDENT RICE', 'Rice')
    nat = ('QUESTION: Mr. Secretary, can you persuade them to talk first? SECRETARY : We are going to have '
           'good conversations today, and I look forward to it.')
    sos = 'We are going to have good conversations today, and I look forward to it.'
    assert ar.remove_foreign_paragraphs(sos, nat, 'Powell') == sos


def test_foreign_removal_never_empties_a_row():
    nat = 'QUESTION: Secretary Kerry, is there any more news on Syria today and tomorrow?'
    sos = ', is there any more news on Syria today and tomorrow?'
    assert ar.remove_foreign_paragraphs(sos, nat, 'Kerry') == sos


def test_verifier_audit_checks(tmp_path):
    """Checks AK (text a repair would still change) and AL (non-person People Mentioned):
    a new row FAILs, the same row unchanged since --baseline is skipped, and
    --strict-audit-checks checks it anyway."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_corpus_title import _verify, _write, _COLS
    import csv as _csv
    cols = _COLS + ['People Mentioned (Natural Text)', 'People Mentioned (SOS Text)']
    raw = ('Remarks\nQUESTION: Secretary Kerry, is there any more news on Syria today?\n'
           'SECRETARY KERRY: The basic ceasefire is holding, but it is fragile.\n')
    row = {'Row Id': '0', 'Secretary': 'Kerry', 'Date': '2016-01-01', 'Title': 'Remarks',
           'Speech Type': 'Remarks', 'Location': 'Washington, D.C.', 'URL': 'https://x/1',
           'SOS Text': ', is there any more news on Syria today?\n\nThe basic ceasefire is holding, but it is fragile.',
           'Natural Text': raw.split('\n', 1)[1].strip(), 'Raw Text': raw,
           'People Mentioned (Natural Text)': 'Quartet', 'People Mentioned (SOS Text)': ''}

    def write(path, rows):
        with open(path, 'w', newline='', encoding='utf-8') as f:
            w = _csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        return path
    bad = write(tmp_path / 'bad.csv', [row])
    rc, out = _verify('--input', bad, '--audit-checks')
    assert rc == 1 and 'FAIL  Check AK' in out and 'FAIL  Check AL' in out, out
    rc, out = _verify('--input', bad, '--audit-checks', '--baseline', bad)
    assert 'ok    Check AK' in out and 'skipped' in out, out
    rc, out = _verify('--input', bad, '--strict-audit-checks', '--baseline', bad)
    assert 'FAIL  Check AK' in out, out
    good = write(tmp_path / 'good.csv', [dict(row, **{
        'SOS Text': 'The basic ceasefire is holding, but it is fragile.',
        'People Mentioned (Natural Text)': ''})])
    rc, out = _verify('--input', good, '--audit-checks')
    assert 'ok    Check AK' in out and 'ok    Check AL' in out, out


def test_statement_start_never_restores_a_transcript():
    """7579: Natural Text with mid-line labels is a transcript; the page text before the
    cut ("AUDIENCE: Good evening. SECRETARY CLINTON: ...") must not be pasted in."""
    raw = ('Remarks\nJune 1, 2012\nGood evening. AUDIENCE: Good evening. SECRETARY CLINTON: And Minister, '
           'thank you. (Applause.) Today HIV, and new infections are on the rise again in the region.')
    assert ar.restore_statement_start('and new infections are on the rise again in the region.', raw) is None
    assert ar._has_inline_label('Good evening. AUDIENCE: Good evening. SECRETARY CLINTON: And')


LONG = ('We are grateful for everything this embassy community does, in good times and in hard ones, '
        'and I want every one of you to know how much the whole department values the work you do here.')


def test_inline_label_after_swallowed_word_and_foreign_detector():
    """8109: "... Model UN. SECRETARY KERRY: Oh, no." -- the match swallows "UN."; the
    label is still found, so Kerry's words are his, not Ms. Norton's."""
    nat = ('AMBASSADOR MCFAUL: Please welcome the Secretary of State today and thank you all.\n'
           'SECRETARY KERRY: Thank you very much. Thanks for the job you are doing here in Moscow. '
           'MS. NORTON: Model UN. SECRETARY KERRY: Oh, no. He is at the model UN conference in '
           'Helsinki this week with the rest of the delegation from the embassy school. ' + LONG)
    sos = ('Thank you very much. Thanks for the job you are doing here in Moscow.\n\nOh, no. He is at the '
           'model UN conference in Helsinki this week with the rest of the delegation from the embassy school. ' + LONG)
    assert ar.foreign_leak_words(sos, nat, 'Kerry') == 0
    assert ar.remove_foreign_sentences(sos, nat, 'Kerry') == sos
    leaked = sos + '\n\nPlease welcome the Secretary of State today and thank you all.'
    assert ar.foreign_leak_words(leaked, nat, 'Kerry') >= 10
    assert ar.remove_foreign_sentences(leaked, nat, 'Kerry') == sos
