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
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from typing import Iterable, NamedTuple

__all__ = ['Gazetteer', 'build_gazetteer', 'normalize_location', 'fold_city']

CAPITAL = 'Washington, D.C.'


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

    # Rule 4 -- a person or office holder, not a place.
    if _TITLE_CUE_RE.search(value) or _PERSON_NAME_RE.search(value):
        return '', 'person_rejected'

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

        return '', 'unresolved_region'

    return '', 'unresolved'


def assert_schema_safe(value: str) -> bool:
    """Post-condition mirroring verifier Checks L, O and P."""
    if not value or value.count(',') != 1:
        return False
    if _EN_ROUTE_RE.search(value) or re.search(r'\ben\s+route\b', value, re.I):
        return False
    left, _, right = value.partition(',')
    return bool(left.strip()) and bool(right.strip())
