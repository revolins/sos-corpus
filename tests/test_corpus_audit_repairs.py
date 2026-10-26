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
