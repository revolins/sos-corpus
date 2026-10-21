"""Offline tests for the Rubio extraction path (plan steps P1a, P1b, P1c wiring, P1d,
P1e) and the re-extraction replay tool. No network: every page is a fixture built
from corpus Raw Text (tests/fixtures/rubio/) or a synthetic string.

Run: python -m pytest tests/test_rubio_extract.py -q   (or python tests/test_rubio_extract.py)
"""
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (_HERE.parent / 'scripts', _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import rubio_backfill as rb                                   # noqa: E402
import rubio_reextract as rx                                  # noqa: E402
from corpus_finalize_v4_30 import strip_stage_directions      # noqa: E402
from corpus_fusion import _find_speech_content_start          # noqa: E402
from hybrid_corpus_recovery import extract_secretary_only_text  # noqa: E402
from speech_extract import make_speaker_text                  # noqa: E402
from test_rubio_turns import _LEAK_CASES                      # noqa: E402

FIXTURES = _HERE / 'fixtures' / 'rubio'

# Real state.gov chrome around the content block, so the footer is present exactly
# as it was when X-01 vetoed final turns.
_NAV = ('Skip to content\nAn official website of the United States Government\n'
        'Here\'s how you know\nTopics\nCountries and Areas\nBureaus and Offices\n'
        'About\nShare this page on:\nClose\nhide\n')
_FOOTER = ('\nBack to Top\nContact Us\nPrivacy Policy\nAccessibility Statement\n'
           'Copyright Information\nFOIA\nNo FEAR Act\n')


def _page(row_id: int) -> dict:
    block = (FIXTURES / f'row_{row_id}.txt').read_text(encoding='utf-8')
    return {'raw_text': _NAV + block + _FOOTER}


def _extract(row_id: int) -> dict:
    ex = rb.extract_row(f'https://www.state.gov/test/{row_id}/', _page(row_id))
    assert ex is not None, row_id
    return ex


def _norm(s: str) -> str:
    s = s.lower().replace('\u2019', "'").replace('\u2018', "'").replace('\xa0', ' ')
    return ' '.join(re.sub(r"[^\w']+", ' ', s).split())


# ---------------------------------------------------------------------------
# Full recall (X-01 final turn, X-02 boilerplate word, X-06 typo, X-07 legacy)
# ---------------------------------------------------------------------------

# Phrases from the Rubio text the published v2026.10.19 SOS Text lost.
_RECALL_CASES = {
    17409: ['the Caribbean basin is home for me',
            'my presence here today serves as a real world demonstration'],
    17379: ['I want to focus my comments this morning on what happens now'],
    17083: ['do you remember that movie The Day After'],
    17045: ['they always relied on him to carry some of the big bills'],
    17685: ['this is the 2028 U.S. passport'],
    17415: ['I have a couple updates for you',
            'so people can contact us and register'],
    17658: ['I was asked about the military base'],
    17435: ['why do we have billions and billions of dollars'],
    17183: ["it's going to be decided on the ground"],
    17706: ['these were very small sanctions'],
}

# Independent oracle for X-01: the body of the LAST strict "SECRETARY RUBIO:" turn
# in the fixture (up to the next own-line uppercase label or Tags).
_OWNLINE_LABEL = re.compile(r'(?m)^[^\W\d_a-z][^a-z:\n]{0,60}:[ \t]*$')


def _last_rubio_turn(block: str) -> str:
    starts = [m.end() for m in re.finditer(r'(?m)^SECRETARY RUBIO:[ \t]*$', block)]
    if not starts:
        return ''
    s = starts[-1]
    nxt = _OWNLINE_LABEL.search(block, s)
    end = nxt.start() if nxt else block.find('\nTags\n', s)
    return block[s:end]


def test_full_recall_rows_contain_lost_phrases():
    for row_id, phrases in _RECALL_CASES.items():
        ex = _extract(row_id)
        flat = _norm(ex['speech_text'])
        assert ex['extraction_mode'].startswith('no_checkpoint+rubio_turns'), row_id
        for p in phrases:
            assert _norm(p) in flat, (row_id, p)


def test_full_recall_rows_keep_final_rubio_turn_and_no_footer():
    for row_id in _RECALL_CASES:
        block = (FIXTURES / f'row_{row_id}.txt').read_text(encoding='utf-8')
        block, _ = rb._apply_footnote_errata(block)      # 17415 ends in a footnote list
        ex = _extract(row_id)
        last = _norm(re.sub(r'\([^()]{0,40}\)', ' ', _last_rubio_turn(block)))
        if last:
            assert last[-120:] in _norm(ex['speech_text']), row_id
        for junk in ('Contact Us', 'Privacy Policy', 'Back to Top'):
            assert junk not in ex['speech_text'], (row_id, junk)
        assert '\nTags' not in ex['speech_text'] and '\nTags' not in ex['speaker_text']


def test_17658_typo_label_turn_and_17415_contact_us_turn():
    assert 'has to come through a permit from the colombian government' in \
        _norm(_extract(17658)['speech_text'])
    # X-02: the 866-word opening that says "contact us" is kept whole
    t = _norm(_extract(17415)['speech_text'])
    assert t.startswith('all right i have a couple updates for you')


# ---------------------------------------------------------------------------
# Zero leak (X-03 vocatives, X-04 Unicode labels, X-05 split colons)
# ---------------------------------------------------------------------------

def test_zero_leak_rows_through_extract_row():
    for row_id, (absent, present) in _LEAK_CASES.items():
        ex = _extract(row_id)
        flat = _norm(ex['speech_text'])
        for p in absent:
            assert _norm(p) not in flat, (row_id, p)
        for p in present:
            assert _norm(p) in flat, (row_id, p)
        # No speaker label survives inside SOS Text
        assert not re.search(r'(?m)^[^\W\d_a-z][^a-z:\n]{2,60}:', ex['speech_text']), row_id


def test_zero_leak_rows_score_zero_with_replay_metric():
    for row_id in _LEAK_CASES:
        page = _page(row_id)
        ex = rb.extract_row('u', page)
        rub, foreign, n = rx.reference(page['raw_text'])
        ref = rx._shingles(rx._tokens(rub))
        fo = rx._shingles(rx._tokens(foreign)) - ref
        words, recall, leak = rx.score(ex['speech_text'], ref, fo)
        assert n >= 1 and leak == 0 and recall >= 0.98, (row_id, recall, leak)


# ---------------------------------------------------------------------------
# X-08: 17095 starts cleanly; half-text fallback is opt-out only
# ---------------------------------------------------------------------------

def _raw17095() -> str:
    return (FIXTURES / 'raw_17095.txt').read_text(encoding='utf-8')


def test_17095_clean_start():
    url = 'https://www.state.gov/external-content/statement-from-secretary-of-state-marco-rubio'
    ex = rb.extract_row(url, {'raw_text': _raw17095()})
    t = _norm(ex['speech_text'])
    assert t.startswith('tonight israel took unilateral action against iran'), t[:80]
    assert t.endswith('iran should not target u s interests or personnel')
    assert 'related' not in t and 'what they are saying' not in t
    assert ex['extraction_mode'] == 'no_block+dated_body_fallback'


def test_find_speech_content_start_half_fallback_param():
    raw = _raw17095()
    assert _find_speech_content_start(raw, 'rubio') == len(raw) // 2     # unchanged default
    assert _find_speech_content_start(raw, 'rubio', half_fallback=False) == -1
    assert make_speaker_text(raw, 'https://www.state.gov/x', 'rubio', half_fallback=False) == ''
    assert make_speaker_text(raw, 'https://www.state.gov/x', 'rubio') != ''


# ---------------------------------------------------------------------------
# P1b: boilerplate veto parameter
# ---------------------------------------------------------------------------

_VETO_TEXT = ('SECRETARY RUBIO: We have set up a hotline so people can contact us and '
              'register, and we will reach every American who needs help.\n'
              'QUESTION: Thank you.\n'
              'SECRETARY RUBIO: Thank you all very much for coming today.\n'
              'Contact Us\nPrivacy Policy\nFOIA\nSite Map\n')


def test_boilerplate_veto_default_unchanged():
    # Old behaviour: both anchored turns contain a cue, both are vetoed, and the
    # extractor falls back to the whole text, footer included.
    text, mode = extract_secretary_only_text(_VETO_TEXT, 'rubio')
    assert mode == 'fallback_full' and 'Privacy Policy' in text


def test_boilerplate_veto_off_trims_tail_keeps_turns():
    text, mode = extract_secretary_only_text(_VETO_TEXT, 'rubio', boilerplate_veto=False)
    assert 'so people can contact us and register' in text
    assert text.endswith('Thank you all very much for coming today.')
    assert 'Privacy' not in text and 'Contact Us' not in text


# ---------------------------------------------------------------------------
# P1e: per-block text, stage directions, footnote errata, untranscribed count
# ---------------------------------------------------------------------------

_HTML = """<html><head><title>T</title><script>var x = 'SECRETARY RUBIO:';</script></head>
<body><nav><ul><li>Topics</li><li>About</li></ul></nav>
<div><button>Share</button><span>hide</span></div>
<h1>Remarks to the Press</h1><p>Remarks</p><p>Marco Rubio, Secretary of State</p>
<p>Washington, D.C.</p><p>March 3, 2026</p>
<div class="entry-content">
<p><strong>SECRETARY RUBIO</strong>: Good <em>afternoon</em>.  We had an
extraordinarily<sup><a href="#fn1">[1]</a></sup> busy week.</p>
<p><strong>QUESTION</strong>: Mr. Secretary –</p>
<p><strong>SECRETARY RUBIO</strong>: Thank you.<br>Bye.</p>
<p><a id="fn1">[1]</a> extraordinary</p>
</div>
<p>Tags</p><p>Iran</p><footer><p>Contact Us</p></footer></body></html>"""


def test_block_text_keeps_labels_and_colons_together():
    soup = rb._make_soup(_HTML)
    bt = rb._block_text(soup)
    assert 'SECRETARY RUBIO: Good afternoon. We had an extraordinarily[1] busy week.' in bt
    assert 'var x' not in bt                       # script bodies are not text
    assert '\nhide\n' in '\n' + bt + '\n' and '\nTags\n' in bt
    # get_text('\n') splits the same label from its colon
    assert 'SECRETARY RUBIO\n:' in soup.get_text('\n', strip=True)


def test_content_block_prefers_per_block_only_when_same_characters():
    soup = rb._make_soup(_HTML)
    raw = soup.get_text('\n', strip=True)
    ct = rb._extract_modern_state_content(rb._block_text(soup))
    block, src = rb._rubio_content_block({'raw_text': raw, 'content_text': ct})
    assert src == 'per_block' and block == ct
    block, src = rb._rubio_content_block({'raw_text': raw, 'content_text': ct + ' extra'})
    assert src == 'raw'
    ex = rb.extract_row('u', {'raw_text': raw, 'content_text': ct})
    assert ex['speech_text'] == ('Good afternoon. We had an extraordinary busy week.'
                                 '\n\nThank you. Bye.')
    assert ex['extraction_mode'].endswith('+per_block+footnote_errata')


def test_backlink_footnote_uses_raw_layout():
    # 2025-12-04 Institute of Peace remarks: a bare-number <sup> marker with a
    # back-link footnote. Per-block text glues it ("starting1"); raw_text keeps it
    # on its own line, where the erratum ("starting" -> "ending") is applied.
    html = ('<html><head><title>T</title></head><body>'
            '<div><button>Share</button><span>hide</span></div>'
            '<h1>Remarks to Press</h1><p>Remarks</p><p>December 4, 2025</p>'
            '<div class="entry-content">'
            '<p><strong>SECRETARY RUBIO</strong>: We want a peace that extends beyond simply '
            'starting<sup><a href="#fn1">1</a></sup> a war in the Congo.</p>'
            '<p><strong>QUESTION</strong>: Thank you.</p>'
            '<p><strong>SECRETARY RUBIO</strong>: Thank you.</p>'
            '<p>________________</p><p>ending<a href="#r1">\u21a9\ufe0e</a></p>'
            '</div><p>Tags</p><p>Congo</p><footer><p>Contact Us</p></footer></body></html>')
    soup = rb._make_soup(html)
    raw = soup.get_text('\n', strip=True)
    ct = rb._extract_modern_state_content(rb._block_text(soup))
    assert 'starting1' in ct
    block, src = rb._rubio_content_block({'raw_text': raw, 'content_text': ct})
    assert src == 'raw'
    ex = rb.extract_row('u', {'raw_text': raw, 'content_text': ct})
    assert 'simply ending a war' in ex['speech_text']
    assert 'starting' not in ex['speech_text'] and '\u21a9' not in ex['speech_text']
    assert ex['speech_text'].rstrip().endswith('Thank you.')


def test_stage_direction_via_translation_rubio_only():
    s = '(Via translation) We are concerned about political violence in any country.'
    assert strip_stage_directions(s) == s                              # default unchanged
    assert strip_stage_directions(s, rubio_path=True).startswith('We are concerned')
    assert strip_stage_directions('( Via interpretation) Thank you all for coming here.',
                                  rubio_path=True).startswith('Thank you')


def test_footnote_errata_layouts():
    blk = ('He was here\n[1]\nthis week and it was extraordinarily\n[2]\nbusy.\n'
           'SECRETARY RUBIO:\nThanks.\n# # #\n[1] Washington D.C. Secretary Rubio met with '
           'him in Washington D.C. on Tuesday.\n[2]\nextraordinary')
    out, n = rb._apply_footnote_errata(blk)
    assert n == 2
    assert 'He was here this week and it was extraordinary busy.' in out    # note: marker only
    assert 'Washington D.C.' not in out and '[1]' not in out and '[2]' not in out
    # back-link layout with bare-number reference (17492)
    out, n = rb._apply_footnote_errata('We held the Rare Earths Ministerial\n1\nthat was big.\n'
                                       'Thank you.\nCritical Minerals Ministerial\n\u21a9\ufe0e')
    assert out == 'We held the Critical Minerals Ministerial that was big.\nThank you.' and n == 1
    # split linked marker (17415) and a correction that runs past the marker (17686)
    out, _ = rb._apply_footnote_errata('a massive short-term\n[\n1\n]* ballistic missile '
                                       'inventory.\nEnd.\n[\n1\n]\nshort-range ballistic missile')
    assert out == 'a massive short-range ballistic missile inventory.\nEnd.'
    head, tail = rb._apply_correction('with both the Kingdom of Norway',
                                      'Kingdom of Denmark but also Greenland',
                                      'but also Denmark that allows us')
    assert head + ' ' + tail.lstrip() == ('with both the Kingdom of Denmark but also '
                                          'Greenland that allows us')
    # no footnotes: untouched
    assert rb._apply_footnote_errata('Plain text [1] here.') == ('Plain text [1] here.', 0)
    # orphan definition under the separator that nothing references: part of the
    # footnote list, dropped with the separator
    out, _ = rb._apply_footnote_errata('every step of the way.\nSECRETARY RUBIO:\nThank you.\n'
                                       '________________\nending\n\u21a9\ufe0e')
    assert out == 'every step of the way.\nSECRETARY RUBIO:\nThank you.'


def test_footnote_fixture_rows():
    t = _norm(_extract(17255)['speech_text'])
    assert 'his patience is not indefinite' in t
    assert 'previous secretaries of state have done' in t
    assert 'patience secretaries' not in t                   # footnote list dropped
    t = _norm(_extract(17686)['speech_text'])
    assert 'kingdom of denmark but also greenland that allows us' in t
    t = _norm(_extract(17415)['speech_text'])
    assert 'massive short range ballistic missile inventory' in t
    assert not t.endswith('ballistic missile')


def test_untranscribed_segments_and_fieldnames():
    assert 'untranscribed_rubio_segments' in rb._FIELDNAMES
    assert _extract(17189)['untranscribed_rubio_segments'] >= 1
    assert _extract(17342)['untranscribed_rubio_segments'] == 0


def test_labelless_statement_is_the_block():
    body = ('Today, the United States designates a terrorist organization.\n'
            'Secretary Rubio has made clear that we will act.  For questions, contact us.\n'
            'We will continue to use every tool available.')
    raw = (_NAV + 'Designation of Something\nPress Statement\nMarco Rubio, Secretary of State\n'
           'June 1, 2026\n' + body + '\nTags\nTerrorism\n' + _FOOTER)
    ex = rb.extract_row('u', {'raw_text': raw})
    assert ex['speech_text'] == body and ex['speaker_text'] == body
    assert ex['extraction_mode'] == 'no_checkpoint+content_block'


def test_helpers_use_rubio_turns():
    t = 'QUESTION:\nSecretary Rubio, thank you.\nFOREIGN MINISTER RAMÍREZ:\nYes.\nSECRETARY RUBI:\nOkay.'
    assert rb._has_non_rubio_label(t)
    assert rb._collect_rubio_turns(t) == 'Okay.'
    assert not rb._has_non_rubio_label('SECRETARY RUBIO:\nHello.')


# ---------------------------------------------------------------------------
# rubio_reextract helpers
# ---------------------------------------------------------------------------

def test_reextract_scores():
    ref = rx._shingles(rx._tokens('one two three four five six seven eight'))
    foreign = rx._shingles(rx._tokens('alpha beta gamma delta epsilon zeta eta')) - ref
    w, r, lk = rx.score('one two three four five six seven eight', ref, foreign)
    assert (w, r, lk) == (8, 1.0, 0)
    w, r, lk = rx.score('one two three four five six alpha beta gamma delta epsilon zeta',
                        ref, foreign)
    assert lk == 6 and 0 < r < 1


def test_reextract_refuses_repo_data_dir():
    try:
        rx._check_out_dir(rx._REPO / 'data' / 'x')
    except SystemExit:
        return
    raise AssertionError('data/ output dir was accepted')


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f'PASS {fn.__name__}')
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f'FAIL {fn.__name__}: {e!r}')
    print(f'{len(fns) - failed} passed, {failed} failed')
    sys.exit(1 if failed else 0)


def test_reextract_hand_review_rows_are_not_auto_accepted():
    """A HAND_REVIEW row never counts as automatically accepted (plan step 9)."""
    import pandas as pd
    hr = sorted(rx.HAND_REVIEW)[0]
    base = {'URL': 'u', 'Speech Type': 'Remarks', 'extraction_mode': 'm', 'n_rubio_turns': 1,
            'old_words': 10, 'new_words': 10, 'old_rubio_recall': 1.0, 'new_rubio_recall': 1.0,
            'old_foreign_leak_words': 0, 'new_foreign_leak_words': 0, 'lost_words': 0,
            'gained_words': 0, 'untranscribed_rubio_segments': 0, 'changed': 0}
    d = pd.DataFrame([dict(base, **{'Row Id': '1', 'accepted': 1, 'hand_review': 0}),
                      dict(base, **{'Row Id': hr, 'accepted': 0, 'hand_review': 1})],
                     columns=rx._DIFF_COLUMNS)
    s = rx.summarize(d)
    assert 'Accepted automatically: 1   rejected: 0   pending hand review (never auto-accepted): 1' in s
