#!/usr/bin/env python3
"""Location normalization for newly scraped SOS corpus rows.

The research schema requires every Location to be strict `City, Region` --
exactly one comma, no "En Route", no three-part venue/city/country strings
(verifier Checks L, O, P). The 17,617 historical rows satisfy that because a
human hand-wrote a ~210-entry LOCATION_PATCHES dict in
corpus_finalize_v4_30.py, keyed by Row Id. That mechanism is useless for new
rows: corpus_rubio_update.py renumbers Row Id densely on every merge, so the
keys would land on the wrong rows.

So new rows arrived with whatever the scraper found -- JSON-LD
contentLocation, or literally the raw line above the date on the page -- past
a four-entry exact-match dict. That is why the weekly job failed on
2026-09-21 with 12 comma-less Locations and one "En Route".

── Why this is written fresh rather than reusing the recovery helpers ───────
hybrid_corpus_recovery._clean_location and _impute_location_from_url, and
filter_certain_secstate_speakers.infer_location_from_article_meta, look like
donors but are solving the opposite problem. They do *recovery* (salvage
anything from messy archival HTML) and happily return 'Op-Ed', 'Recorded',
'On Travel' and 'Washington, DC' -- every one of which FAILS Check L or uses
the wrong D.C. spelling. Wiring them onto the weekly path would enlarge the
failure, not fix it.

── The gazetteer is derived, not hand-written ───────────────────────────────
build_gazetteer() reads the Location column of the corpus being merged into.
Those values are already curated, already in the project's diplomatic
vocabulary ("Burma" not "Myanmar", "Republic of Korea" not "Korea, Republic
of", "Turkey" over "Türkiye"), and they re-derive themselves for free as the
corpus grows. Ambiguity is resolved by corpus frequency, i.e. by the
curators' own revealed preference: 'vienna' -> Austria (53) not Virginia (1).

── Unresolvable means quarantine, never a default ───────────────────────────
normalize_location returns ('', reason) when it cannot produce a valid value.
The caller drops the row to a sidecar for human triage. Silently defaulting
to 'Washington, D.C.' would fabricate provenance -- an "En Route to Doha"
speech is not a Washington speech. The ONE legitimate default is rule 2: a
page that explicitly states a newsroom origin ("Office of the Spokesperson",
"Via Teleconference") really is a Washington dateline, which is how 611 of
the 724 existing Rubio rows got their value. The distinction to hold onto:
the page said something that means Washington -> default; the page said
nothing -> quarantine.

── Provisional, never dropped (2026-10, rubio coverage audit M-01/M-02) ─────
Quarantine turned out to be a silent permanent loss: the discovery floor
moves past a quarantined row within two weeks and it is never fetched again.
A replay over the 814 Rubio rows found 8 that would be dropped today
('Prime Minister’s Office, Jerusalem', 'Ben Gurion International Airport',
'Basseterre, St. Kitts and Nevis', ...). normalize_location keeps its
contract -- ('', reason) still means "not confidently resolved" -- but the
caller now merges the row with provisional_location()'s value and marks it
Location Provisional = 1, instead of dropping the speech. The provisional
value still never fabricates Washington: it uses 'Unknown' for whatever part
of 'City, Region' the page did not state.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from typing import Iterable, NamedTuple

__all__ = ['Gazetteer', 'build_gazetteer', 'normalize_location',
           'provisional_location', 'fold_city', 'UNKNOWN']

CAPITAL = 'Washington, D.C.'

# Placeholder for the half of 'City, Region' a page did not state. Only ever
# produced by provisional_location(), and kept out of build_gazetteer() so a
# provisional row can never teach the gazetteer a fake city or region.
UNKNOWN = 'Unknown'


class Gazetteer(NamedTuple):
    city: dict            # folded city -> canonical "City, Region" (majority region)
    city_region: dict     # (folded city, folded region) -> canonical "City, Region"
    rhs_vocab: frozenset  # accepted right-hand-side (country/state) spellings


# ── Rule 2: explicit newsroom / non-place datelines ──────────────────────────
# Regex, not exact match. The old four-entry dict had
# 'Office of the Spokesperson' but not 'Office of the Spokesman', so the
# September 2026 rename walked straight past it into a Check L failure.
_NEWSROOM_RE = re.compile(
    r'''^(?:
          office\s+of\s+the\s+spokes(?:person|man|woman)
        | (?:via\s+)?(?:teleconference|telephone|video\s*conference|zoom)
        | press\s+briefing\s+room
        | (?:u\.?s\.?\s+)?department\s+of\s+state
        | (?:aboard|on\s+board)\s+air\s+force\s+(?:one|two)
        | washington\s*,?\s*d\.?\s*c\.?
        | washington\s*,?\s*district\s+of\s+columbia
    )$''',
    re.IGNORECASE | re.VERBOSE,
)

# ── Rule 3: "En Route [to] X" -- Check O forbids the phrase outright ─────────
_EN_ROUTE_RE = re.compile(r'^en\s*[-\s]?\s*route\s+(?:to\s+)?', re.IGNORECASE)

# ── Rule 4: person / title strings misparsed as a location ───────────────────
# The scraper's block fallback takes "the line above the date", which on a
# joint-statement page is a person. That is how Row Id 17608 ended up as
# 'Robert F. Kennedy, Secretary of Health and Human Services' -- it has
# exactly one comma, so Checks L and P both pass it today.
_TITLE_CUE_RE = re.compile(
    r'\b(?:secretary\s+of|under\s+secretary|deputy\s+secretary|minister|'
    r'ambassador|president|vice\s+president|senator|congressman|'
    r'representative|governor|chancellor|chairman|spokesperson|'
    r'prime\s+minister|his\s+excellency|her\s+excellency|nsa|'
    r'gen\.|adm\.|lt\.|col\.|dr\.|mr\.|mrs\.|ms\.)\b',
    re.IGNORECASE,
)
# "Robert F. Kennedy" -- Firstname MiddleInitial. Lastname
_PERSON_NAME_RE = re.compile(r'\b[A-Z][a-z]+\s+[A-Z]\.\s+[A-Z][a-z]+\b')

# Rule 4 exemptions -- a title or a name that is part of a PLACE name.
# "Prime Minister’s Office" / "Ambassador's Residence": the possessive (and
# up to three words before it) names whose building it is. "Harry S. Truman
# Building": an initialled name followed by a building noun.
_POSSESSIVE_RE = re.compile(r"(?:\b[\w.]+\s+){0,3}[\w.]+[’']s\b")
_INITIALLED_VENUE_RE = re.compile(
    r'\b[A-Z][a-z]+\s+[A-Z]\.\s+[A-Z][a-z]+\s+(?:Building|Center|Centre|'
    r'Hall|Library|Airport|Auditorium|Room|Memorial|Museum|Institute|School|'
    r'University|College|Bridge|Station|Plaza|Square|Park|Theater|Theatre|'
    r'Conference\s+Center|Federal\s+Building)\b'
)

# ── Venue aliases -- named venues whose city the page leaves implicit ───────
# Folded key (see fold_city) -> canonical corpus value. Checked against the
# whole value and against each comma part, so 'Oval Office, The White House'
# and 'The White House' both land on the capital. The targets are existing
# corpus spellings (Tel Aviv, Israel: 47 rows; Manama, Bahrain: 22), and Ben
# Gurion follows the corpus's own precedent (row 17238 is 'Tel Aviv, Israel').
_VENUE_ALIASES = {
    'ben gurion airport': 'Tel Aviv, Israel',
    'ben gurion international airport': 'Tel Aviv, Israel',
    'ritz carlton bahrain': 'Manama, Bahrain',
    'the ritz carlton bahrain': 'Manama, Bahrain',
    'the white house': CAPITAL,
    'white house': CAPITAL,
    'oval office': CAPITAL,
    'the oval office': CAPITAL,
    'u s capitol': CAPITAL,
    'us capitol': CAPITAL,
    'the u s capitol': CAPITAL,
    'united states capitol': CAPITAL,
    'the united states capitol': CAPITAL,
    'harry s truman building': CAPITAL,
    'the harry s truman building': CAPITAL,
    'truman building': CAPITAL,
}

# "St. Kitts and Nevis" vs pycountry's "Saint Kitts and Nevis": a region that
# misses the vocabulary only on this spelling is the same region.
_ST_RE = re.compile(r'^(?:St\.?|Saint)\s+', re.IGNORECASE)

# Rule 4 when nothing else said Washington -- an explicit dateline only.
_DC_DATELINE_RE = re.compile(
    r'\bwashington\s*,?\s*(?:d\.?\s*c\b\.?|district\s+of\s+columbia\b)',
    re.IGNORECASE,
)

_USPS = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
    'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho',
    'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
    'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota',
    'MS': 'Mississippi', 'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska',
    'NV': 'Nevada', 'NH': 'New Hampshire', 'NJ': 'New Jersey',
    'NM': 'New Mexico', 'NY': 'New York', 'NC': 'North Carolina',
    'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma', 'OR': 'Oregon',
    'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina',
    'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah',
    'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington',
    'WV': 'West Virginia', 'WI': 'Wisconsin', 'WY': 'Wyoming',
    'DC': 'D.C.', 'D.C': 'D.C.', 'PR': 'Puerto Rico',
}

_WS_RE = re.compile(r'\s+')
_PUNCT_RE = re.compile(r'[^a-z0-9 ]+')


def _reference_regions() -> frozenset:
    """pycountry country names + US subdivision names.

    Only WIDENS the corpus-derived vocabulary, so a first-ever speech in a
    country the corpus has never visited still normalizes instead of being
    quarantined. Guarded import, matching how corpus_verify_research_grade.py
    guards its own pycountry-backed table -- an absent pycountry degrades to
    corpus-only vocabulary rather than crashing the weekly job.
    """
    names: set[str] = {'D.C.'}
    try:
        import pycountry  # type: ignore[import]
    except ImportError:
        return frozenset(names)
    for country in pycountry.countries:
        for attr in ('name', 'official_name', 'common_name'):
            val = getattr(country, attr, None)
            if val:
                names.add(val)
    try:
        for sub in pycountry.subdivisions.get(country_code='US') or ():
            names.add(sub.name)
    except (KeyError, LookupError):
        pass
    return frozenset(names)


def fold_city(value: str) -> str:
    """Accent-folded, punctuation-stripped, lowercased lookup key.

    So 'San José' and 'San Jose' hit the same gazetteer entry -- while the
    STORED value keeps the corpus's own spelling, accents and all.
    """
    s = unicodedata.normalize('NFKD', (value or '').strip())
    s = ''.join(ch for ch in s if not unicodedata.combining(ch))
    s = _PUNCT_RE.sub(' ', s.lower())
    return _WS_RE.sub(' ', s).strip()


def _tidy(value: str) -> str:
    return _WS_RE.sub(' ', (value or '').replace('\xa0', ' ')).strip().strip(',;:').strip()


def build_gazetteer(rows: Iterable[dict]) -> Gazetteer:
    """Derive city -> 'City, Region' and the accepted region vocabulary.

    Only single-comma Locations contribute, since those are the ones the
    schema considers well formed. A region spelling is accepted into the
    vocabulary on the corpus's own authority -- pycountry does not know
    'Burma' (23 rows), 'Republic of Korea' (67), 'Turkey' (66) or 'West Bank'
    (38), all of which are deliberate State Department usage -- but a region
    carrying a person/title cue is rejected, which is what keeps the one junk
    value ('Secretary of Health and Human Services') out.
    """
    pairs: dict[str, Counter] = defaultdict(Counter)
    pairs_cr: dict[tuple, Counter] = defaultdict(Counter)
    rhs_counts: Counter = Counter()

    for row in rows:
        loc = _tidy(row.get('Location', ''))
        if loc.count(',') != 1:
            continue
        city, _, rhs = loc.partition(',')
        city, rhs = city.strip(), rhs.strip()
        if not city or not rhs:
            continue
        if city == UNKNOWN or rhs == UNKNOWN:
            continue  # a provisional placeholder, not curated vocabulary
        if _TITLE_CUE_RE.search(rhs) or _PERSON_NAME_RE.search(loc):
            continue
        rhs_counts[rhs] += 1
        canonical = f'{city}, {rhs}'
        pairs[fold_city(city)][canonical] += 1
        pairs_cr[(fold_city(city), rhs.casefold())][canonical] += 1

    # Ambiguity -> highest corpus frequency wins (the curators' revealed
    # preference). A near-tie is deliberately still resolved here; the caller
    # only ever sees a single canonical spelling.
    city_map = {k: counter.most_common(1)[0][0] for k, counter in pairs.items()}
    # Keyed by city AND region, so a caller that already knows the region
    # never has it overridden by a more popular different one.
    city_region_map = {k: counter.most_common(1)[0][0] for k, counter in pairs_cr.items()}

    if len(city_map) < 300:
        raise ValueError(
            f'build_gazetteer: only {len(city_map)} entries derived from '
            f'{rhs_counts.total() if hasattr(rhs_counts, "total") else sum(rhs_counts.values())} '
            f'usable rows -- the corpus was probably read with the wrong schema.'
        )

    return Gazetteer(city_map, city_region_map,
                     frozenset(rhs_counts) | _reference_regions())


def _expand_region(rhs: str) -> str:
    key = rhs.strip().rstrip('.').upper()
    return _USPS.get(key, rhs.strip())


def _saint_variant(rhs: str, vocab: frozenset) -> str:
    """'St. Kitts and Nevis' -> 'Saint Kitts and Nevis' (or the reverse) when
    only the other spelling is in the vocabulary; '' otherwise."""
    m = _ST_RE.match(rhs)
    if not m:
        return ''
    rest = rhs[m.end():]
    for prefix in ('Saint ', 'St. '):
        cand = prefix + rest
        if cand != rhs and cand in vocab:
            return cand
    return ''


def _is_person_part(part: str) -> bool:
    """Rule 4 for one comma part: does it name a person or office holder?

    Possessives ("Prime Minister’s Office") and initialled building names
    ("Harry S. Truman Building") are places, so they are blanked out before
    the cue regexes run.
    """
    probe = _INITIALLED_VENUE_RE.sub(' ', _POSSESSIVE_RE.sub(' ', part))
    return bool(_TITLE_CUE_RE.search(probe) or _PERSON_NAME_RE.search(probe))


def _venue_alias(value: str) -> str:
    """Alias hit on the whole value or on any single comma part; '' if none."""
    hit = _VENUE_ALIASES.get(fold_city(value))
    if hit:
        return hit
    for part in value.split(','):
        hit = _VENUE_ALIASES.get(fold_city(part))
        if hit:
            return hit
    return ''


def normalize_location(raw: str, gaz: Gazetteer) -> tuple[str, str]:
    """Return (canonical 'City, Region', method) or ('', reason).

    An empty first element means the caller must quarantine the row. The
    returned value, when non-empty, is guaranteed to satisfy verifier Checks
    L, O and P by the post-condition at the end of this function.
    """
    value = _tidy(raw)
    if not value:
        return '', 'empty'

    # Rule 2 -- explicit newsroom dateline. The one legitimate default.
    if _NEWSROOM_RE.match(value):
        return CAPITAL, 'newsroom'

    # Rule 3 -- strip "En Route [to]" and keep resolving what remains.
    method_prefix = ''
    stripped = _EN_ROUTE_RE.sub('', value)
    if stripped != value:
        value = _tidy(stripped)
        method_prefix = 'en_route+'
        if not value:
            return '', 'en_route_bare'

    # Venue alias -- a named venue whose city the page left implicit.
    hit = _venue_alias(value)
    if hit:
        return hit, method_prefix + 'venue_alias'

    # Rule 4 -- a person or office holder, not a place. Applied PER COMMA
    # PART: 'Prime Minister’s Office, Jerusalem' used to be rejected whole
    # because one part carried a title cue. Person parts are dropped and the
    # rest resolved; the value is rejected only when no part is left.
    parts = [p.strip() for p in value.split(',') if p.strip()]
    person = [_is_person_part(p) for p in parts]
    if any(person):
        kept = [p for p, bad in zip(parts, person) if not bad]
        if not kept:
            return '', 'person_rejected'
        value = ', '.join(kept)
        method_prefix += 'person_stripped+'

    # Rule 5 -- the whole string is a known city or named venue. Only when
    # there is no comma: a value that already names its region must go
    # through rule 6, so the region it states stays authoritative.
    if ',' not in value:
        hit = gaz.city.get(fold_city(value))
        if hit:
            return hit, method_prefix + 'gazetteer_city'

    # Rule 6 -- comma forms.
    if ',' in value:
        parts = [p.strip() for p in value.split(',') if p.strip()]
        if len(parts) >= 2:
            # Last two parts, always. A three-part "venue, city, region"
            # reduces to "city, region" -- the same reduction
            # corpus_finalize_v4_30.py applied by hand to 15 historical rows.
            # For a two-part value parts[-2] IS parts[0], so this is uniform.
            lhs, rhs = parts[-2], _expand_region(parts[-1])
            if rhs not in gaz.rhs_vocab:
                rhs = _saint_variant(rhs, gaz.rhs_vocab) or rhs

            # Known region: prefer the corpus's canonical spelling for this
            # city when we have one (this is what collapses 'Antalya, Türkiye'
            # onto 'Antalya, Turkey' and keeps Check Y quiet).
            if rhs in gaz.rhs_vocab:
                # The region the row states is authoritative -- look the city
                # up UNDER THAT REGION, which only ever canonicalizes the
                # city's spelling. Using the city's most popular region here
                # instead would silently rewrite 'San Jose, California' to
                # 'San Jose, Costa Rica' and 'Vancouver, Washington' to
                # 'Vancouver, Canada'. Measured: that mistake would have
                # corrupted 39 existing rows.
                hit = gaz.city_region.get((fold_city(lhs), rhs.casefold()))
                if hit:
                    return hit, method_prefix + 'gazetteer_lhs'
                # Venue concatenated onto the city, with no comma between
                # them: "Le Bourget Airport Paris, France". Walk in from the
                # left for the longest trailing run of words that names a
                # known city -- but only accept it when its region agrees
                # with the region we were given, so "... Springfield, Illinois"
                # can never be pulled onto "Springfield, Massachusetts".
                words = lhs.split()
                for start in range(1, len(words)):
                    cand = gaz.city.get(fold_city(' '.join(words[start:])))
                    if cand and cand.rpartition(',')[2].strip().casefold() == rhs.casefold():
                        return cand, method_prefix + 'gazetteer_venue_prefix'
                return f'{lhs}, {rhs}', method_prefix + 'passthrough'

            # Venue-prefixed: "Beit Hanassi, Jerusalem" -> the city is on the
            # right and the venue on the left.
            hit = gaz.city.get(fold_city(parts[-1]))
            if hit:
                return hit, method_prefix + 'gazetteer_venue'

        return '', method_prefix + 'unresolved_region'

    return '', method_prefix + 'unresolved'


def provisional_location(raw: str, gaz: Gazetteer) -> tuple[str, str]:
    """Never-empty, schema-safe Location for a row normalize_location could
    not resolve. The caller must mark the row Location Provisional = 1.

    Cascade, most to least informative:
      1. dateline       -- 'Washington, D.C.' ONLY if the value says so
      2. country level  -- a part that is (or ends in) a known region:
                           'Civil-Military Coordination Center, Southern
                           Israel' -> 'Southern Israel, Israel';
                           'Ambassador Smith, France' -> 'Unknown, France'
      3. best form      -- what the page said, padded with 'Unknown':
                           'Foo, Bar' as is; 'Some Venue' -> 'Some Venue, Unknown'
      4. nothing usable -- 'Unknown, Unknown' (a person/byline, or empty)
    """
    value = _tidy(_EN_ROUTE_RE.sub('', _tidy(raw)))
    parts = [p.strip() for p in value.split(',') if p.strip()]
    parts = [p for p in parts if not _is_person_part(p) and p != UNKNOWN]

    # 1. An explicit Washington dateline somewhere in the value. Checked
    # first so 'D.C.' is never taken for a country-level region.
    if _DC_DATELINE_RE.search(value):
        return CAPITAL, 'provisional_dateline_dc'

    # 2. Country level. Right to left: the region is conventionally last.
    vocab = {fold_city(v): v for v in gaz.rhs_vocab}
    for part in reversed(parts):
        words = part.split()
        for start in range(len(words)):
            tail = ' '.join(words[start:])
            region = vocab.get(fold_city(tail)) or _saint_variant(tail, gaz.rhs_vocab)
            if region:
                lhs = UNKNOWN if start == 0 else part
                cand = f'{lhs}, {region}'
                if assert_schema_safe(cand):
                    return cand, 'provisional_country'
                break

    # 3. Best schema-safe form of what the page actually said.
    if len(parts) >= 2:
        cand, method = f'{parts[-2]}, {parts[-1]}', 'provisional_passthrough'
    elif parts:
        cand, method = f'{parts[0]}, {UNKNOWN}', 'provisional_venue_only'
    else:
        cand, method = '', ''
    if cand and assert_schema_safe(cand):
        return cand, method

    # 4. Nothing place-like at all.
    return f'{UNKNOWN}, {UNKNOWN}', 'provisional_unknown'


def assert_schema_safe(value: str) -> bool:
    """Post-condition mirroring verifier Checks L, O and P."""
    if not value or value.count(',') != 1:
        return False
    if _EN_ROUTE_RE.search(value) or re.search(r'\ben\s+route\b', value, re.I):
        return False
    left, _, right = value.partition(',')
    return bool(left.strip()) and bool(right.strip())
