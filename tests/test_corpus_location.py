"""corpus_location: plan step 6a (rubio coverage audit M-01/M-02).

Offline. The gazetteer is built from tests/fixtures/corpus_locations.csv --
every distinct Location of release v2026.10.19 with its row count, plus what
the PRE-change normalize_location returned for it (expected_norm /
expected_method), so the invariant "no existing value changes" is a test.
"""
import csv
from pathlib import Path

import pytest

from corpus_location import (
    UNKNOWN,
    assert_schema_safe,
    build_gazetteer,
    normalize_location,
    provisional_location,
)

FIX = Path(__file__).resolve().parent / 'fixtures'


def _read(name):
    with open(FIX / name, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope='module')
def loc_table():
    return _read('corpus_locations.csv')


@pytest.fixture(scope='module')
def gaz(loc_table):
    rows = [{'Location': r['Location']} for r in loc_table for _ in range(int(r['count']))]
    return build_gazetteer(rows)


def test_existing_locations_unchanged(loc_table, gaz):
    """Invariant: every Location already in the corpus normalizes exactly as
    before the change (all 555 values / 17,617 rows, every secretary)."""
    changed = [(r['Location'], r['expected_norm'], normalize_location(r['Location'], gaz)[0])
               for r in loc_table
               if normalize_location(r['Location'], gaz) != (r['expected_norm'], r['expected_method'])]
    assert changed == []


# The 8 Rubio rows the audit's replay would have quarantined (dropped).
REPLAY_EXPECTED = {
    '16986': ('Unknown, Unknown', True),            # 'NSA Waltz' -- a person, no place
    '17237': ('Jerusalem, Israel', False),          # Prime Minister’s Office, Jerusalem
    '17238': ('Tel Aviv, Israel', False),           # Ben Gurion International Airport
    '17293': ('Jerusalem, Israel', False),          # Prime Minister's Office, Jerusalem
    '17296': ('Southern Israel, Israel', True),     # CMCC, Southern Israel
    '17409': ('Basseterre, Saint Kitts and Nevis', False),
    '17473': ('Manama, Bahrain', False),            # The Ritz-Carlton Bahrain
    '17609': ('Unknown, Unknown', True),            # RFK Jr. byline
}


def _merge_location(raw, gaz):
    """What corpus_rubio_update does: strict first, provisional on failure."""
    value, method = normalize_location(raw, gaz)
    if value and assert_schema_safe(value):
        return value, method, False
    value, method = provisional_location(raw, gaz)
    return value, method, True


def test_replay_rows_never_dropped(gaz):
    rows = _read('rubio_replay_locations.csv')
    assert len(rows) == 8
    for r in rows:
        value, method, provisional = _merge_location(r['location_raw'], gaz)
        assert value and assert_schema_safe(value), r
        assert (value, provisional) == REPLAY_EXPECTED[r['Row Id']], (r, method)


@pytest.mark.parametrize('raw, expected', [
    ('Harry S. Truman Building, Washington, D.C.', 'Washington, D.C.'),
    ('The White House', 'Washington, D.C.'),
    ('Oval Office, The White House', 'Washington, D.C.'),
    ('U.S. Capitol', 'Washington, D.C.'),
    ('Ben Gurion Airport', 'Tel Aviv, Israel'),
    ("Ambassador's Residence, Paris, France", 'Paris, France'),
    ('Prime Minister’s Office, Jerusalem', 'Jerusalem, Israel'),
    ('Basseterre, St. Kitts and Nevis', 'Basseterre, Saint Kitts and Nevis'),
])
def test_strict_new_resolutions(gaz, raw, expected):
    assert normalize_location(raw, gaz)[0] == expected


@pytest.mark.parametrize('raw', [
    'Robert F. Kennedy, Secretary of Health and Human Services',
    'NSA Waltz',
    'Secretary of State Marco Rubio',
])
def test_person_strings_still_rejected_strictly(gaz, raw):
    assert normalize_location(raw, gaz) == ('', 'person_rejected')


def test_person_part_stripped_not_whole_value(gaz):
    # A person part next to a known city: the city is kept.
    assert normalize_location('Ambassador Jane Doe, Jerusalem', gaz)[0] == 'Jerusalem, Israel'


@pytest.mark.parametrize('raw, expected, method', [
    ('', f'{UNKNOWN}, {UNKNOWN}', 'provisional_unknown'),
    ('NSA Waltz', f'{UNKNOWN}, {UNKNOWN}', 'provisional_unknown'),
    ('Ambassador Smith, France', f'{UNKNOWN}, France', 'provisional_country'),
    ('Civil-Military Coordination Center, Southern Israel', 'Southern Israel, Israel',
     'provisional_country'),
    ('Some Unlisted Hotel', f'Some Unlisted Hotel, {UNKNOWN}', 'provisional_venue_only'),
    ('Somewhere Hall, Washington, D.C.', 'Washington, D.C.', 'provisional_dateline_dc'),
])
def test_provisional_cascade(gaz, raw, expected, method):
    value, got_method = provisional_location(raw, gaz)
    assert (value, got_method) == (expected, method)
    assert assert_schema_safe(value)


def test_provisional_never_fabricates_washington(gaz):
    # No dateline says Washington -> no Washington.
    for raw in ('', 'NSA Waltz', 'Some Unlisted Hotel', 'En Route'):
        assert provisional_location(raw, gaz)[0] != 'Washington, D.C.'


def test_unknown_placeholder_kept_out_of_gazetteer(loc_table):
    rows = [{'Location': r['Location']} for r in loc_table]
    rows += [{'Location': f'{UNKNOWN}, {UNKNOWN}'}] * 50 + [{'Location': f'Some Hotel, {UNKNOWN}'}] * 50
    g = build_gazetteer(rows)
    assert UNKNOWN not in g.rhs_vocab
    assert 'unknown' not in g.city
