"""Tests for scripts/rubio_turns.py (plan step P1c; audit X-03..X-06, X-10, M-05).

Offline only.  Fixtures under tests/fixtures/rubio/ are the content block (title line
through the "Tags" footer) of Raw Text from corpus_v2026.10.19.csv, byte-for-byte
(NBSP, curly quotes and split labels preserved).

Runs under pytest, or without it:  python tests/test_rubio_turns.py
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / 'scripts'
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import rubio_turns as rt  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'rubio'


def _fixture(row_id: int) -> str:
    return (FIXTURES / f'row_{row_id}.txt').read_text(encoding='utf-8')


def _flat(s: str) -> str:
    """Whitespace/quote-insensitive form for phrase containment checks."""
    return ' '.join(s.replace('\xa0', ' ').replace('\u2019', "'").split())


def _labels(text):
    return [(t.label, t.is_rubio, t.text) for t in rt.segment(text) if t.label is not None]


# ---------------------------------------------------------------------------
# Label resolution
# ---------------------------------------------------------------------------
def test_rubio_label_variants_resolve():
    for lbl in ('SECRETARY RUBIO', 'SECRETARY RUBIO:', 'SECRETARY-DESIGNATE RUBIO',
                'SECRETARY DESIGNATE RUBIO', 'SECRETARY OF STATE RUBIO',
                'SECRETARY OF STATE MARCO RUBIO', 'SECRETARY MARCO RUBIO',
                'THE SECRETARY RUBIO', 'SECRETARY RUBI', 'SECREATRY RUBIO',
                'SEC RETARY RUBIO', 'SECRETARY\xa0RUBIO', 'Secretary Rubio', 'RUBIO'):
        assert rt.is_rubio_label(lbl), lbl
        assert rt.normalize_label(lbl) == 'SECRETARY RUBIO', lbl


def test_excluded_rubio_labels():
    # audit D9 / X-06: Mrs. Rubio and the 2022 SENATOR RUBIO clip are not the Secretary
    for lbl in ('MRS RUBIO', 'MRS. RUBIO', 'SENATOR RUBIO', 'MR RUBIO', 'MS RUBIO'):
        assert not rt.is_rubio_label(lbl), lbl
    assert rt.normalize_label('MRS. RUBIO') == 'MRS RUBIO'
    assert rt.normalize_label('SENATOR RUBIO') == 'SENATOR RUBIO'


def test_other_labels_not_rubio():
    for lbl in ('QUESTION', 'Q', 'MODERATOR', 'SECRETARY HEGSETH', 'SECRETARY BESSENT',
                'DEPUTY SECRETARY LANDAU', 'PRESIDENT TRUMP', 'RUBEN', 'SECRETARY',
                'FOREIGN MINISTER RAMÍREZ', 'COMMISSIONER ŠEFČOVIČ'):
        assert not rt.is_rubio_label(lbl), lbl


def test_normalize_label_canonical_forms():
    # M-05: one spelling per speaker for Speakers Present
    assert rt.normalize_label('MS. BRUCE:') == 'MS BRUCE'
    assert rt.normalize_label('CHAIRMAN\xa0RISCH') == 'CHAIRMAN RISCH'
    assert rt.normalize_label('FOREIGN MINISTER SA\u2019AR') == "FOREIGN MINISTER SA'AR"
    assert rt.normalize_label('FOREIGN MINISTER AL-ZAYANI') == \
        rt.normalize_label('FOREIGN MINISTER AL ZAYANI')
    assert rt.normalize_label('COMMISSIONER ŠEFČOVIČ') == 'COMMISSIONER ŠEFČOVIČ'


# ---------------------------------------------------------------------------
# Segmentation: synthetic cases
# ---------------------------------------------------------------------------
def test_own_line_and_inline_labels():
    text = ('SECRETARY RUBIO:\nGood morning.\nQUESTION: What about Iran?\n'
            'SECRETARY RUBIO: We are watching closely.\nQ: And China?\n'
            'SECRETARY RUBIO:\nSame answer.\n')
    body, st = rt.rubio_text(text)
    assert body == 'Good morning.\n\nWe are watching closely.\n\nSame answer.'
    assert st['n_rubio_turns'] == 3 and st['n_other_turns'] == 2
    assert 'Iran' not in body and 'China' not in body


def test_vocative_is_not_a_label():
    # X-03: "Secretary Rubio, thank you..." inside another speaker's turn
    text = ('SECRETARY RUBIO:\nThank you all.\nSECRETARY HEGSETH:\n'
            'Secretary Rubio, thank you for hosting us.\nSecretary Rubio: a mixed-case line.\n'
            'We are building submarines together.\n')
    body, st = rt.rubio_text(text)
    assert body == 'Thank you all.'
    assert st['n_rubio_turns'] == 1
    assert st['unresolved_labels'] == []      # SECRETARY HEGSETH is resolved, not logged


def test_nbsp_and_curly_quote_labels_are_boundaries():
    # X-04
    text = ('SECRETARY\xa0RUBIO:\nWelcome, minister.\nFOREIGN MINISTER SA\u2019AR:\n'
            'Thank you, Marco.\nCHAIRMAN\xa0RISCH:\nThe committee will come to order.\n'
            'SECRETARY RUBIO:\nThank you, Chairman.\n')
    body, st = rt.rubio_text(text)
    assert body == 'Welcome, minister.\n\nThank you, Chairman.'
    labels = [l for l, _, _ in _labels(text)]
    assert labels == ['SECRETARY RUBIO', "FOREIGN MINISTER SA'AR", 'CHAIRMAN RISCH',
                      'SECRETARY RUBIO']


def test_unicode_uppercase_labels():
    # X-04: RAMÍREZ, GUNNARSDÓTTIR, inline form
    text = ('SECRETARY RUBIO:\nWe visited Paraguay just a couple of months ago \u2014\n'
            'FOREIGN MINISTER RAMÍREZ: Yes, exactly.\nSECRETARY RUBIO:\n\u2014 and we talked.\n'
            'FOREIGN MINISTER GUNNARSDÓTTIR:\nThank you, dear Marco.\n')
    body, _ = rt.rubio_text(text)
    assert 'Yes, exactly' not in body and 'dear Marco' not in body
    assert 'and we talked' in body


def test_split_colon_label():
    # X-05: get_text('\n') puts the colon on its own line
    text = ('SECRETARY RUBIO\n:  Thank you all for being here today.\n'
            'FOREIGN MINISTER LEZCANO\n:  Thank you, dear Marco.\n'
            'QUESTION\n:\nWhat about the agreement?\nSECRETARY RUBIO\n:\nIt is historic.\n')
    body, st = rt.rubio_text(text)
    assert body == 'Thank you all for being here today.\n\nIt is historic.'
    assert st['n_other_turns'] == 2
    assert [l for l, _, _ in _labels(text)] == [
        'SECRETARY RUBIO', 'FOREIGN MINISTER LEZCANO', 'QUESTION', 'SECRETARY RUBIO']


def test_split_word_label_sec_retary():
    # X-06 (17153): "SEC\nRETARY RUBIO:"
    text = ('QUESTION:\nSecretary Rubio, thank you so much for sitting down with me.\n'
            'SEC\nRETARY RUBIO:\nThank you.\n')
    body, st = rt.rubio_text(text)
    assert body == 'Thank you.'
    turns = rt.segment(text)
    assert turns[0].label == 'QUESTION' and 'SEC' not in turns[0].text.split()


def test_three_line_split_label():
    # X-04/X-05 (17451): "COMMISSIONER\nŠEFČOVIČ\n:"
    text = ('SECRETARY RUBIO:\nWelcome to the State Department.\nCOMMISSIONER\nŠEFČOVIČ\n:\n'
            'Thank you very much, Mr. Secretary of State.\n')
    body, _ = rt.rubio_text(text)
    assert body == 'Welcome to the State Department.'
    assert [l for l, _, _ in _labels(text)] == ['SECRETARY RUBIO', 'COMMISSIONER ŠEFČOVIČ']


def test_heading_line_not_joined_into_label():
    text = 'AS PREPARED FOR DELIVERY\nSECRETARY RUBIO:\nGood afternoon.\n'
    turns = rt.segment(text)
    assert turns[-1].label == 'SECRETARY RUBIO' and turns[-1].text == 'Good afternoon.'
    assert turns[0].label is None and turns[0].text == 'AS PREPARED FOR DELIVERY'


def test_typo_labels_captured():
    # X-06: 17658 "SECRETARY RUBI:", 17538 "SECREATRY RUBIO:"
    text = ('QUESTION:\nAnd why not in Barranquilla?\nSECRETARY RUBI:\nThat needs to be studied.\n'
            'QUESTION:\nIran and China?\nSECREATRY RUBIO:\nOkay.\n')
    body, st = rt.rubio_text(text)
    assert body == 'That needs to be studied.\n\nOkay.'
    assert st['n_rubio_turns'] == 2 and st['unresolved_labels'] == []


def test_mrs_and_senator_rubio_excluded():
    text = ('MRS RUBIO:\nWe are so proud of him.\nSENATOR RUBIO:\nIn 2022 I said this.\n'
            'SECRETARY RUBIO:\nThank you.\n')
    body, st = rt.rubio_text(text)
    assert body == 'Thank you.'
    assert st['n_other_turns'] == 2
    assert st['unresolved_labels'] == []


def test_unresolved_labels_logged():
    text = 'SECRETARY:\nHello.\nSECRETRY RUBBIO JR:\nHi.\nSECRETARY NOEM:\nHey.\n'
    _, st = rt.rubio_text(text)
    assert 'SECRETARY' in st['unresolved_labels']
    assert 'SECRETRY RUBBIO JR' in st['unresolved_labels']
    assert 'SECRETARY NOEM' not in st['unresolved_labels']


def test_question_and_moderator_are_non_rubio():
    text = ('MODERATOR:\nFirst question.\nQUESTION\n:\nSecretary Rubio, Cuba?\n'
            'SECRETARY RUBIO:\nCuba is controlled by GAESA.\nPARTICIPANT: Thank you.\n')
    body, st = rt.rubio_text(text)
    assert body == 'Cuba is controlled by GAESA.'
    assert st['n_other_turns'] == 3


def test_no_minimum_turn_length():
    text = 'QUESTION:\nAre you ready?\nSECRETARY RUBIO:\nYes.\nQUESTION:\nGood.\n'
    body, st = rt.rubio_text(text)
    assert body == 'Yes.' and st['n_rubio_turns'] == 1


def test_stage_directions_removed():
    text = ('SECRETARY RUBIO:\nThank you. (Laughter.) We are glad (inaudible) to be here.\n'
            '(Applause.)\n(Via translation) Briefly in Spanish.\n(The agreement was signed.)\n')
    body, _ = rt.rubio_text(text)
    assert body == 'Thank you. We are glad to be here. Briefly in Spanish.'


def test_untranscribed_spanish_counted():
    # X-10: standalone placeholders in Rubio turns count; translation markers followed by
    # text (17658) and placeholders in other speakers' turns do not.
    text = ('SECRETARY RUBIO:\nForeign aid must serve our interest.\n(In Spanish.)\n'
            'All right.  Questions?\nQUESTION:\n(In Spanish.)\nSECRETARY RUBIO:\n(In Spanish.)\n'
            'QUESTION:\n(In Spanish.) Will tariffs be lifted?\nSECRETARY RUBIO:\n'
            '(In Spanish.) We are working on that.\nSECRETARY RUBIO:\n(Speaking in Icelandic.)\n')
    body, st = rt.rubio_text(text)
    assert st['untranscribed_segments'] == 3
    assert '(In Spanish' not in body and 'Icelandic' not in body
    assert 'We are working on that.' in body
    assert st['n_rubio_turns'] == 4


def test_untranscribed_inline_gap_and_english_switch():
    # 17045 / 17189: mid-line gap counts; 17357: "(In English)" switch-back does not
    text = ('SECRETARY RUBIO:\nIt is also an honor to be (in Spanish).  But more importantly, '
            'I am glad.\n(Via translation)\nWe talk to everyone.\n(In English)\n'
            'The question was about Colombia.\n')
    body, st = rt.rubio_text(text)
    assert st['untranscribed_segments'] == 1
    assert body == ('It is also an honor to be. But more importantly, I am glad. '
                    'We talk to everyone. The question was about Colombia.')


def test_footer_stops_last_turn():
    # X-01 safety: the final Rubio turn ends at the state.gov footer, not the page end
    text = 'SECRETARY RUBIO:\nThank you.\nTags\nAUKUS\nAustralia\nBack to Top\nPrivacy Policy\n'
    body, _ = rt.rubio_text(text)
    assert body == 'Thank you.'
    body_nf, _ = rt.rubio_text(text, stop_at_footer=False)
    assert 'Privacy Policy' in body_nf


def test_cuban_message_intro_has_no_labels():
    # C-01 page (row 17483 in rubio_backfill_2026-06-22.csv): third-person intro then a
    # label-less translated transcript.  The segmenter must find no Rubio label; the
    # transcript-marker handling belongs to the classifier (plan step 3).
    text = ('Media Note\nMarco Rubio, Secretary of State\nMay 20, 2026\n'
            'Today, Secretary of State Marco Rubio\xa0delivered\xa0a video message to the\xa0'
            'people of Cuba.\nA courtesy translation of the Secretary\u2019s video message is below.\n'
            'Translated Transcript\nOn a day like today, in 1902, the Cuban flag flew for the first '
            'time over an independent country.\n')
    body, st = rt.rubio_text(text)
    assert body == '' and st['n_rubio_turns'] == 0 and st['n_other_turns'] == 0
    turns = rt.segment(text)
    assert len(turns) == 1 and turns[0].label is None


def test_offsets_index_original_text():
    text = 'QUESTION:\nHi\xa0there.\nSECRETARY\xa0RUBIO:\nHello\u2019s\xa0world.\n'
    for t in rt.segment(text):
        if t.label == 'SECRETARY RUBIO':
            assert text[t.start:t.end].strip() == 'Hello\u2019s\xa0world.'
            assert t.text == 'Hello\u2019s world.'   # original apostrophe kept, NBSP folded


def test_empty_input():
    assert rt.segment('') == []
    body, st = rt.rubio_text(None)
    assert body == '' and st['n_rubio_turns'] == 0


# ---------------------------------------------------------------------------
# Real corpus rows (v2026.10.19 Raw Text).  Zero leak: phrases spoken by others,
# which the published SOS Text contains today, must be absent; Rubio phrases present.
# ---------------------------------------------------------------------------
_LEAK_CASES = {
    # row: (other-speaker phrases that must be absent, Rubio phrases that must be present)
    17342: (['Deputy Prime Minister Marles, we\'ve seen each other often',       # Hegseth
             'AUSMIN was established',                                          # Wong
             'on behalf of Penny and I'],                                       # Marles
            ['I want to welcome our colleagues from Australia', 'All right.']),
    17021: (['Secretary Rubio, thanks so much for joining the show',
             'Secretary Rubio, it\'s a really good object lesson',
             'Secretary Rubio, obviously this is a',
             'Secretary Rubio, I think that the controversy'],
            []),
    16919: (['Good morning, everyone.  And good morning particularly Secretary of State Rubio',
             'A safe country is not a topic that exists',
             'it is very difficult to decide in advance where we will be able to locate returnees',
             'how ready is Guatemala'],
            []),
    17189: (['Yes, exactly.',
             'You are a very close friend of our government',
             'Today this agreement represent our deep engagement',
             'very focused on establishing safe third country agreements'],
            ['Briefly in Spanish', 'Okay.  Well, let\'s sign.',
             'some conversation about security guarantees']),
    17451: (['Thank you very much, Mr. Secretary of State.  It is a pleasure to be here with you'],
            []),
    17704: (['I will not continue in Icelandic, my dear Marco',
             'Marco explained very eloquently during our meeting',
             'Thank you, dear Marco.  And actually you saw',
             'The first question from RUV'],
            []),
    17347: (['Your commitment with our relation is quite important',
             'it will strength our relation not only on security'],
            ['Thank you all for being here today',
             'I would like to thank Minister Lezcano for being with us today']),
    16996: (['Good afternoon, Prime Minister Holness and Secretary Rubio',
             'our last question will come from Andrea Chisholm'],
            []),
    17237: (['your visit to our eternal and undivided capital',
             'did Israel update the U.S. before the strike in Doha',
             'Did you ask Israel not to advance with its military maneuver',
             'You can ask anything, but there\'s order',
             'Israel\'s decision to act against the Hamas terrorist leadership'],    # PRME typo
            ['Hamas can surrender tonight']),
}


def test_fixture_rows_zero_leak():
    for row_id, (absent, present) in _LEAK_CASES.items():
        raw = _fixture(row_id)
        body, st = rt.rubio_text(raw)
        flat = _flat(body)
        assert st['n_rubio_turns'] >= 1, row_id
        for phrase in absent:
            assert _flat(phrase) in _flat(raw), (row_id, 'phrase missing from fixture', phrase)
            assert _flat(phrase) not in flat, (row_id, 'leak', phrase)
        for phrase in present:
            assert _flat(phrase) in flat, (row_id, 'lost', phrase)
        # no label text ever lands inside Rubio text
        for lbl in st['labels']:
            assert lbl + ':' not in flat.upper().replace('.', ''), (row_id, lbl)


def test_fixture_17237_netanyahu_split_label():
    # X-05: "PRIME MINISTER NETANYAHU\n:  Go ahead." must be his turn, not Rubio's
    turns = rt.segment(_fixture(17237))
    ga = [t for t in turns if t.text == 'Go ahead.']
    assert ga and all(t.label == 'PRIME MINISTER NETANYAHU' for t in ga)


def test_fixture_17451_sefcovic_three_line_label():
    turns = rt.segment(_fixture(17451))
    labels = [t.label for t in turns if t.label]
    assert 'COMMISSIONER ŠEFČOVIČ' in labels
    assert 'ŠEFČOVIČ' not in labels


def test_fixture_17658_secretary_rubi_turn_captured():
    # X-06: 203 words lost today
    body, st = rt.rubio_text(_fixture(17658))
    flat = _flat(body)
    assert 'Well, I was asked about the military base, so as I said, that needs to be studied' in flat
    assert 'has to come through a permit from the Colombian government' in flat
    assert 'And why not in Barranquilla?' not in flat
    assert 'will the tariffs on Colombia be lifted by you' not in flat   # vocative question
    assert st['unresolved_labels'] == []
    assert st['untranscribed_segments'] == 0      # every marker here precedes a translation


def test_fixture_17153_split_sec_retary():
    raw = _fixture(17153)
    assert 'SEC\nRETARY RUBIO:' in raw
    turns = rt.segment(raw)
    i = next(n for n, t in enumerate(turns) if t.label == 'QUESTION')
    assert turns[i].text == 'Secretary Rubio, thank you so much for sitting down with me.'
    assert turns[i + 1].is_rubio and turns[i + 1].text.startswith('Thank you.')
    assert set(t.label for t in turns if t.label) == {'QUESTION', 'SECRETARY RUBIO'}


def test_fixture_17538_secreatry_okay():
    turns = rt.segment(_fixture(17538))
    i = next(n for n, t in enumerate(turns)
             if t.label == 'QUESTION' and 'Iran-China relations' in t.text)
    assert turns[i + 1].is_rubio and turns[i + 1].text == 'Okay.'
    body, st = rt.rubio_text(_fixture(17538))
    assert st['unresolved_labels'] == []
    assert 'Cara Castronuova from LindellTV' not in body


def test_fixture_footer_never_in_rubio_text():
    for row_id in list(_LEAK_CASES) + [17658, 17153, 17538]:
        body, _ = rt.rubio_text(_fixture(row_id))
        lines = body.split('\n')
        assert 'Tags' not in lines and 'Back to Top' not in body, row_id


def test_label_with_parenthetical_is_a_turn_boundary():
    # "(via interpreter)" before the colon used to fail the all-caps shape test,
    # so the foreign minister's words leaked into the open Rubio turn.
    text, info = rt.rubio_text('SECRETARY RUBIO: Thank you all for coming.\n'
                               'FOREIGN MINISTER WANG (via interpreter): We disagree entirely.\n'
                               'SECRETARY RUBIO: Okay.')
    assert 'disagree' not in text and 'WANG' not in text
    assert info['n_rubio_turns'] == 2 and info['n_other_turns'] == 1


def test_rubio_label_with_parenthetical_is_kept():
    text, info = rt.rubio_text('SECRETARY RUBIO (via interpreter): Gracias a todos.')
    assert text == 'Gracias a todos.' and info['n_rubio_turns'] == 1


if __name__ == '__main__':
    # Plain runner for environments without pytest.
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith('test_') and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f'PASS {name}')
        except Exception as exc:              # noqa: BLE001
            failed += 1
            print(f'FAIL {name}: {exc!r}')
    print(f'{len(tests) - failed} passed, {failed} failed')
    sys.exit(1 if failed else 0)
