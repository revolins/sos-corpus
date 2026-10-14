#!/usr/bin/env python3
"""corpus_enrich_v1.py — Enrichment stage for the SOS speech corpus.

Runs AFTER corpus_finalize_v4_30.py (clean, QA'd text) and BEFORE
corpus_verify_research_grade.py (validates the enriched output). Appends new
derived columns to the finalize-stage output; it never mutates the 10
existing columns (Secretary ... Raw Text) — the verifier's Check Q enforces
this by diffing against a --baseline copy of the finalize output.

Phase 1 (v1): word counts + checksums.
    SOS Word Count, Natural Word Count  — len(text.split()), matching the
        form already used in corpus_finalize_v4_30.py's check_length_outliers
        / write_distribution_report (NOT the set-deduped word count inside
        check_field_coherence, which serves Jaccard overlap, a different
        purpose).
    Row Hash — sha256 over the row's own content fields (Secretary ...
        Raw Text), deliberately EXCLUDING Row Id so a future finalize
        renumbering pass doesn't spuriously change every row's hash. This is
        a provenance/edit-detection hash — distinct from the unrelated
        fuzzy-dedup md5 key used for URL/content matching in
        corpus_fusion.py; do not conflate the two.
    <output>.manifest.json — dataset-level sidecar (row count, schema,
        whole-file hash, and a fingerprint hash of every row's Row Hash in
        order), mirroring the write_distribution_report() sidecar pattern
        already used by the finalize scripts.

Phase 2 (v1, added after Phase 1 shipped): Speakers Present.
    Speakers Present — every distinct speaker in `Natural Text`, in
        first-appearance order, `; `-joined. Built on extract_speaker_turns(),
        a sibling to corpus_finalize_v4_30.py's secretary_turns_from_transcript()
        that generalizes the same SPEAKER_LABEL_RE/_BROAD_LABEL_RE turn-boundary
        logic to return EVERY speaker's turns, not just the Secretary's.
        normalize_speaker_label() then maps each raw label to a display name
        (own-Secretary turns -> the row's canonical Secretary name;
        QUESTION/MODERATOR/etc. -> kept as an anonymous sentinel, not a name;
        honorific-prefixed / role-prefixed labels -> name or role+name).
        The row's own Secretary is always included even if no matching turn
        label was found (structural guarantee, not left to regex recall) —
        this is what makes the verifier's Check T ("own Secretary present")
        a hard, always-true-by-construction check rather than best-effort.
    NOTE: importing SPEAKER_LABEL_RE etc. from corpus_finalize_v4_30 pulls in
    that module's own imports (requests, ftfy) transitively — this script
    now requires the same env as the finalize/rubio-update scripts (the
    `SOS` conda env locally), not just the stdlib.

Phase 3+4 (v1, added after Phase 2 shipped): People Mentioned + Countries Mentioned.
    One shared spaCy NER pass per row over `Natural Text` (see run_ner()) feeds
    both phases -- PERSON spans for Phase 3, GPE/LOC spans for Phase 4 -- so
    NER only runs once per row, not twice.

    People Mentioned (Natural Text), People Mentioned (SOS Text) -- every
        PERSON entity NOT judged to be direct address (see
        _direct_address_reason()'s numbered rules), `; `-joined, first-
        appearance order. The (SOS Text) column is the subset of the same
        kept mentions whose offset falls inside a Secretary turn (or the
        whole document, for turn-less single-speaker text) -- NOT a second
        NER pass on the extracted SOS Text string, which would lose the
        turn-offset context the direct-address heuristic depends on.
        AUTO-EXTRACTED, UNREVIEWED, APPROXIMATE -- spaCy PERSON recall on
        transcript-style text is imperfect and the direct-address heuristic
        has a real, non-trivial error rate in both directions. See
        `_people_mention_audit.csv` (--audit-prefix) for a sampled
        kept/excluded verdict trail.

    Countries Mentioned, Country ISO Codes Mentioned -- GPE/LOC entities
        resolved to a canonical country name + ISO 3166-1 alpha-3 code via
        a curated alias table that defers to this corpus's own established
        Location-column spellings first (see _COUNTRY_INPUT_ALIASES /
        _CANONICAL_NAME_OVERRIDES), then pycountry EXACT name matches only
        -- pycountry.countries.search_fuzzy() is deliberately never used
        here; it was found during development to produce dangerously wrong
        matches ("Republic of Korea" -> North Korea, "Kosovo" -> Serbia).
        No lat/lon geocoding. US states/D.C./territories resolve to "United
        States"; "Georgia" and "Washington" get row-context disambiguation
        (see resolve_country()) since both collide with a real country/
        capital-city reading. AUTO-EXTRACTED, UNREVIEWED, APPROXIMATE --
        alias table is necessarily incomplete; unresolved GPE/LOC mentions
        are dropped from the column but logged to `_unresolved_gpe_audit.csv`
        for future alias-table extension.

Usage:
    python corpus_enrich_v1.py --input <finalize_output>.csv \
        --output <enriched>.csv [--manifest-path <path>] \
        [--incremental] [--audit-prefix v1_enrich] \
        [--spacy-model en_core_web_lg] [--skip-ner] \
        [--ner-batch-size 50] [--n-process 1] \
        [--drop-secretaries <Lastname> ...]

    --drop-secretaries: ablation flag — exclude every row whose Secretary
    matches one of the given canonical last names BEFORE enrichment, then
    renumber Row Id 0..N-1 so the output is a self-consistent release.
    Because Row Hash deliberately excludes Row Id, surviving rows keep
    byte-identical hashes across the renumbering — only the manifest's
    row_count / csv_sha256 / dataset_fingerprint change. Default: drop
    nothing. First used for the v2026.09.03 release, which removes the
    single transitional Christopher row (his 1997-01-08 farewell
    introducing Albright) so every Secretary in the corpus has a
    substantive speech series.

    --incremental: only (re)compute enrichment for rows missing ANY current
    ENRICH_COLUMNS value (not just Row Hash) — this makes incremental mode
    self-healing across schema versions: a row already enriched by an older
    version of this script (e.g. Phase 1 only) will have Speakers Present
    backfilled on the next incremental run, while a row already carrying
    every current column is left untouched. This is what lets the weekly
    Rubio-update CI job enrich just that week's newly merged rows instead of
    recomputing the whole corpus every run.

    --skip-ner: run only Phases 1-2 (word counts, checksums, Speakers
    Present) — fast, no spaCy model load, useful for iterating on those
    without paying the NER cost every run.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

csv.field_size_limit(10 ** 8)

try:
    import pycountry as _pycountry
except ImportError:
    _pycountry = None

# corpus_finalize_v4_30.py lives alongside this script in both Project2/ and
# github-sos-corpus/scripts/ (dual-copy convention) — reuse its speaker-label
# regexes/name tables rather than redefining them, so turn-boundary logic
# can't drift between the finalize and enrich stages.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
try:
    from corpus_finalize_v4_30 import (  # type: ignore[import]
        SPEAKER_LABEL_RE,
        _BROAD_LABEL_RE,
        _DEPUTY_TITLE_RE,
        LASTNAME,
        SECRETARY_CANON,
    )
except ImportError as _e:
    sys.exit(f"ERROR: cannot import speaker-label regexes from corpus_finalize_v4_30.py — {_e}")

try:
    from corpus_keys import canonicalize_live_url  # type: ignore[import]
except ImportError as _e:
    sys.exit(f"ERROR: cannot import corpus_keys — {_e}")

BASE_COLUMNS = [
    'Row Id', 'Secretary', 'Date', 'Title', 'Speech Type',
    'Location', 'URL', 'SOS Text', 'Natural Text', 'Raw Text',
]

# Columns this script adds, in append order. Later phases extend this list
# (People Mentioned (Natural Text)/(SOS Text) and Countries Mentioned/
# Country ISO Codes Mentioned) rather than reordering existing entries, so a
# partially-enriched CSV from an older version still round-trips through
# --incremental cleanly.
ENRICH_COLUMNS = [
    'SOS Word Count',
    'Natural Word Count',
    'Row Hash',
    'Speakers Present',
    'People Mentioned (Natural Text)',
    'People Mentioned (SOS Text)',
    'Countries Mentioned',
    'Country ISO Codes Mentioned',
    # Appended 2026-09 after state.gov renamed /office-of-the-spokesperson/
    # to /office-of-the-spokesman/. 'URL' is frozen at the value originally
    # scraped (it is hashed into Row Hash, and rewriting it would invalidate
    # every published hash and manifest fingerprint); 'Canonical URL' tracks
    # where the page actually lives now. Deliberately NOT in HASH_FIELDS and
    # NOT in _PHASE_1_2_COLUMNS -- see the comments on both.
    'Canonical URL',
]

# Columns that are legitimately empty for many rows (a speech may mention
# zero third parties or zero countries) -- --incremental's "already
# enriched" check must use KEY PRESENCE in the row dict (`c in row`), not
# truthiness of the value, to tell "never computed" (key absent -- an
# older-schema CSV read via csv.DictReader simply won't have the key) apart
# from "computed and genuinely empty" (key present, value ''). See
# enrich_row().
_NER_COLUMNS = [
    'People Mentioned (Natural Text)',
    'People Mentioned (SOS Text)',
    'Countries Mentioned',
    'Country ISO Codes Mentioned',
]

OUTPUT_COLUMNS = BASE_COLUMNS + ENRICH_COLUMNS

# Fields hashed into Row Hash, in this fixed order. Row Id is intentionally
# excluded — see module docstring.
# NOTE: 'Canonical URL' must never be added here. Row Hash is published, and
# write_manifest() fingerprints the dataset as sha256 over every Row Hash --
# adding a field would change all 17,617 hashes, invalidate the fingerprint in
# every released manifest, and break any downstream user who pinned one.
# Keeping Canonical URL outside the hash is also what makes it safe to UPDATE
# in place the next time state.gov reorganizes its paths.
HASH_FIELDS = [
    'Secretary', 'Date', 'Title', 'Speech Type', 'Location',
    'URL', 'SOS Text', 'Natural Text', 'Raw Text',
]
HASH_DELIM = '\x1f'  # ASCII unit separator; not expected to appear in scraped text


def read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def word_count(text: str) -> int:
    return len((text or '').split())


def row_hash(row: dict[str, str]) -> str:
    payload = HASH_DELIM.join(row.get(f, '') or '' for f in HASH_FIELDS)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


# ── Phase 2: Speakers Present ────────────────────────────────────────────

# Labels that never resolve to a real name in these transcripts -- the
# interlocutor is structurally anonymous, not a missing-data gap. Found via
# a full-corpus raw-label frequency scan (STAFF: 214 rows, A PARTICIPANT:
# 70 rows -- distinct raw spelling of the same anonymous-participant
# convention as bare PARTICIPANT, 815 rows) plus the Phase-2 sanity-check
# distributional pass (AUDIENCE: a town-hall audience-member response, same
# anonymous category as MODERATOR/PARTICIPANT).
_ANONYMOUS_SENTINELS = {
    'QUESTION', 'Q', 'MODERATOR', 'HOST', 'REPORTER', 'NARRATOR',
    'PARTICIPANT', 'A PARTICIPANT', 'STAFF', 'AUDIENCE', 'INTERPRETER',
}
# Variant spellings of the same anonymous role, canonicalized to one entry.
_SENTINEL_CANON = {'A PARTICIPANT': 'PARTICIPANT'}

# str.title() mangles Mc-/O'-prefixed surnames ("MCCORMACK" -> "Mccormack"
# instead of "McCormack") -- found via a full-corpus normalized-name
# frequency scan (McCormack alone: 326 rows). "Mc" and "O'" are safe to
# auto-fix: every Mc-/O'-prefixed word actually observed in the corpus's
# raw labels (50+ distinct real surnames -- McCain, McConnell, McDonough,
# O'Brien, O'Sullivan, etc.) follows the internal-capital convention with
# zero counterexamples. "Mac" is DELIBERATELY EXCLUDED: the same scan found
# "Macron" (French president -- no internal cap), "Mack" (Rep. Connie
# Mack -- no internal cap), "Machimura" (Japanese FM -- romanization, not
# Scottish, no internal cap), and "Machete" (not a name at all, debris from
# a garbage label) alongside genuinely ambiguous cases like "Mackay". Auto-
# "fixing" all of these to MacRon/MacK/MacHimura/MacHete would introduce
# more errors than the original bug. Left as plain title() (e.g. "Macron",
# "Mackay") -- a known, documented casing limitation for the minority of
# Mac- names that do use the convention (e.g. a hypothetical "MacDonald"
# would render "Macdonald"), preferred over the alternative of confidently
# mangling names that don't use it.
_NAME_CASE_FIXUP_RE = re.compile(r"\b(Mc|O')([a-z])")


def _titlecase(s: str) -> str:
    return _NAME_CASE_FIXUP_RE.sub(lambda m: m.group(1) + m.group(2).upper(), s.title())

_HONORIFIC_RE = re.compile(r'^(?:MR|MS|MRS|DR)\.?\s+', re.IGNORECASE)
_SEC_GENERAL_RE = re.compile(r'^SECRETARY[\s-]+GENERAL\b\s*(.*)$', re.IGNORECASE)
_DEPUTY_ASSISTANT_NAME_RE = re.compile(r'^(DEPUTY|ASSISTANT)\s+SECRETARY(?:\s+OF\s+STATE)?\s*(.*)$', re.IGNORECASE)
_ROLE_PREFIX_RE = re.compile(
    r'^(AMBASSADOR|SENATOR|CHAIRMAN|ADMINISTRATOR|COMMISSIONER|'
    r'FOREIGN MINISTER|PRIME MINISTER|MINISTER)\b\s*(.*)$',
    re.IGNORECASE,
)



# SPEAKER_LABEL_RE's title alternatives (SECRETARY/MINISTER/FOREIGN MINISTER/
# PRIME MINISTER/...) allow an unbounded `(?:[A-Z]+\s+)*` prefix and/or an
# unbounded `[A-Z .\'\-]+` tail with no length cap. Since `\s+` matches
# newlines too, this can chain across several original lines of ALL-CAPS
# prose (a stage direction, a caps-locked passage, or similar) that happens
# to end in a colon, producing a "label" that is actually a run-on sentence
# fragment rather than a real speaker name -- found via a full-corpus label
# word-count scan (566/~183k raw matches had >=6 words; some spanned 3
# original lines and 85 words). The longest genuine single-entity title
# observed in the corpus was 7 words ("COSTA RICAN SECOND VICE PRESIDENT
# REBECA GRYNSPAN"); nothing legitimate was found above that. This filter
# rejects (a) any match spanning multiple physical lines (never legitimate)
# and (b) same-line matches over 7 words (same-line runaway prose, e.g. a
# caps-locked passage ending "... MR. RUSSERT:"). Not a complete fix --
# same-line garbage at or under 7 words can still slip through (documented
# limitation, not corrected further here).
_MAX_LABEL_WORDS = 7


def _plausible_label(raw_match_text: str) -> bool:
    # Both SPEAKER_LABEL_RE and _BROAD_LABEL_RE end in a trailing `\s*`
    # after the colon, which greedily consumes the newline that immediately
    # follows almost every real label (format is "LABEL:\nturn content..."),
    # so `raw_match_text` itself almost always ends in '\n' even for a
    # perfectly normal single-line label. .strip() drops that
    # regex-consumed trailing whitespace before the newline check, so only
    # a newline INTERNAL to the label (genuine multi-line runaway) trips it
    # -- checking the unstripped text here was a real bug that rejected the
    # vast majority of legitimate labels (e.g. dropped 318/326 real "MR.
    # MCCORMACK" turns), caught by cross-checking extraction totals against
    # a plain substring count on the raw text.
    core = raw_match_text.strip()
    if '\n' in core:
        return False
    return len(core.split()) <= _MAX_LABEL_WORDS


def extract_speaker_turns(text: str) -> list[tuple[str, str, int, int]]:
    """Return every (raw_label, turn_text, start, end) in a speaker-labeled
    transcript, in document order.

    Sibling to corpus_finalize_v4_30.py's secretary_turns_from_transcript(),
    which merges the same SPEAKER_LABEL_RE (narrow, title-gated) and
    _BROAD_LABEL_RE (any all-caps line-start label, catches bare-surname
    labels like "KIRBY:") matches but only returns the Secretary's own
    turns. This generalizes that merge to expose every speaker's turns, so
    Speakers Present (and later, Phase 3's direct-address filtering) can see
    the full turn structure. Implausible matches (see _plausible_label) are
    dropped BEFORE turn-boundary computation, not after, so their text is
    correctly absorbed into the surrounding real speaker's turn instead of
    being treated as its own (bogus) turn boundary.
    """
    text = text or ''
    narrow = list(SPEAKER_LABEL_RE.finditer(text))
    narrow_starts = {m.start() for m in narrow}
    broad_extra = [m for m in _BROAD_LABEL_RE.finditer(text) if m.start() not in narrow_starts]
    candidates = sorted(narrow + broad_extra, key=lambda m: m.start())
    all_labels = [m for m in candidates if _plausible_label(m.group(0))]
    if not all_labels:
        return []
    turns: list[tuple[str, str, int, int]] = []
    for i, m in enumerate(all_labels):
        label = re.sub(r'[\s|:]+$', '', m.group(0).upper()).strip()
        start = m.end()
        end = all_labels[i + 1].start() if i + 1 < len(all_labels) else len(text)
        turns.append((label, text[start:end].strip(' |\n\t'), start, end))
    return turns


def normalize_speaker_label(raw_label: str, secretary: str) -> str:
    """Map a raw turn label (from extract_speaker_turns) to a display name.

    - SECRETARY-GENERAL [name] -> 'Secretary-General [Name]' (UN chief, not
      a Secretary of State -- checked first so it's never confused with one).
    - DEPUTY/ASSISTANT SECRETARY [name] -> 'Deputy/Assistant Secretary
      [Name]' -- a different official, not the row's own Secretary.
    - Any other label containing SECRETARY: if it embeds one of the 10 known
      Secretaries' last names, that Secretary's canonical name (handles
      joint press conferences / interviews naming a different Secretary);
      otherwise (bare "SECRETARY:"/"SECRETARY OF STATE:") the row's own
      Secretary.
    - QUESTION/Q/MODERATOR/HOST/REPORTER/NARRATOR/PARTICIPANT -> kept as an
      uppercase sentinel (anonymous interlocutor, not a resolvable name).
    - MR./MS./MRS./DR. X -> X, title-cased.
    - Role-prefixed labels (AMBASSADOR X, FOREIGN MINISTER X, ...) -> role +
      title-cased name kept together (the role disambiguates otherwise-
      colliding surnames).
    - Anything else (bare all-caps name via the broad-label regex) ->
      title-cased as-is.
    """
    label = (raw_label or '').strip().upper()
    if not label:
        return ''
    if label in _ANONYMOUS_SENTINELS:
        # Canonicalize variant spellings of the same anonymous role (e.g.
        # "A PARTICIPANT" -> "PARTICIPANT") so they dedup as one speaker
        # rather than appearing as two separate sentinel entries. Bracket-
        # wrapped so these read as structural role placeholders, not proper
        # nouns -- bare "QUESTION" sits in a Speakers Present list looking
        # exactly like a named individual ("Blinken", "QUESTION", "Kerry"),
        # which is misleading for a column meant to represent WHO spoke.
        # Cross-checked against Speech Type before making this change: a
        # sentinel like QUESTION is legitimate, context-appropriate content
        # (95.6% of Press Availability rows and 97.5% of Joint Press
        # Availability rows have one, vs. 0.0% of Press Statement and 2.1%
        # of Op-Ed) -- it correctly tracks genuinely interactive speech
        # types, so the fix here is presentation (mark it as non-proper-
        # noun), not removal (the underlying "a distinct anonymous party
        # spoke" fact stays in the data).
        return f'[{_SENTINEL_CANON.get(label, label)}]'

    # An honorific-prefixed address to an anonymous role ("MR. MODERATOR" --
    # someone saying "Thank you, Mr. Moderator") reduces to a bare sentinel
    # once the honorific is stripped, but doesn't match the EXACT-label
    # sentinel check above (which only matches bare "MODERATOR"). Checked
    # here, before the SECRETARY branch and the general title-case
    # fallback, so it can't fall through to either and produce an
    # unbracketed "Moderator" -- exactly the same bug class as "MR.
    # SECRETARY" (fixed earlier producing "Secretary Secretary"), just
    # generalized to cover every sentinel instead of special-casing
    # SECRETARY alone. Found via Check T's own bracket-consistency guard
    # (added alongside the bracketing fix) catching row 6227's bare
    # "Moderator" on the very next full-corpus run.
    honorific_stripped_early = _HONORIFIC_RE.sub('', label).strip()
    if honorific_stripped_early in _ANONYMOUS_SENTINELS:
        return f'[{_SENTINEL_CANON.get(honorific_stripped_early, honorific_stripped_early)}]'

    m = _SEC_GENERAL_RE.match(label)
    if m:
        rest = _HONORIFIC_RE.sub('', m.group(1)).strip()
        return f'Secretary-General {_titlecase(rest)}'.strip() if rest else 'Secretary-General'

    m = _DEPUTY_ASSISTANT_NAME_RE.match(label)
    if m:
        role = f'{m.group(1).title()} Secretary'
        rest = _HONORIFIC_RE.sub('', m.group(2)).strip()
        return f'{role} {_titlecase(rest)}'.strip() if rest else role

    if 'SECRETARY' in label:
        # "MR. SECRETARY"/"MS. SECRETARY OF STATE" -- an honorific-prefixed
        # generic address to whoever's speaking (like saying "Mr.
        # President"), not a named official. Must be checked before the
        # LASTNAME loop and the bare-prefix regex below, both of which only
        # recognize "SECRETARY" as a LEADING token -- "MR. SECRETARY" fails
        # that anchor (it starts with "MR."), falls through to the "named
        # SECRETARY X" branch, and (after honorific-stripping reduces the
        # label back to bare "SECRETARY") wrongly produced "Secretary
        # Secretary". Real bug found via the corpus-wide component
        # distribution report (32 rows, all this exact raw label).
        honorific_stripped = _HONORIFIC_RE.sub('', label).strip()
        if honorific_stripped in ('SECRETARY', 'SECRETARY OF STATE'):
            return SECRETARY_CANON.get((secretary or '').strip().lower(), secretary or '')

        # Match one of the 10 known Secretaries of State by last name first
        # (handles joint press conferences / interviews naming a different
        # SoS than the row's own).
        for canon, lname in LASTNAME.items():
            if lname in label:
                return canon
        bare = re.sub(r'^(?:FOREIGN\s+)?SECRETARY(?:\s+OF\s+STATE)?\s*(.*)$', r'\1', label).strip()
        if not bare:
            # Generic "SECRETARY:"/"SECRETARY OF STATE:" with no name at
            # all -- refers to whoever is speaking, i.e. the row's own
            # Secretary.
            return SECRETARY_CANON.get((secretary or '').strip().lower(), secretary or '')
        # A named "SECRETARY X" / "FOREIGN SECRETARY X" where X is NOT one
        # of the 10 known Secretaries of State -- a different Cabinet-level
        # official (e.g. Secretary of Defense Gates, UK Foreign Secretary
        # Straw). Keep as its own distinct label rather than conflating
        # with the row's own Secretary -- this was a real bug found via a
        # full-corpus label frequency scan (SECRETARY GATES: 299 rows,
        # SECRETARY STRAW/FOREIGN SECRETARY STRAW: 383 rows combined).
        rest = _HONORIFIC_RE.sub('', bare).strip()
        prefix = 'Foreign Secretary' if label.startswith('FOREIGN SECRETARY') else 'Secretary'
        return f'{prefix} {_titlecase(rest)}'.strip() if rest else prefix

    m = _ROLE_PREFIX_RE.match(label)
    if m:
        role = m.group(1).title()
        rest = _HONORIFIC_RE.sub('', m.group(2)).strip()
        return f'{role} {_titlecase(rest)}'.strip() if rest else role

    stripped = _HONORIFIC_RE.sub('', label).strip()
    return _titlecase(stripped or label)


def speakers_present(natural_text: str, secretary: str) -> str:
    """Every distinct speaker in `natural_text`, first-appearance order,
    `; `-joined. The row's own Secretary is always included even if
    extract_speaker_turns() found no matching label for them -- a
    structural guarantee (not left to regex recall) that backs the
    verifier's Check T.
    """
    own_canon = SECRETARY_CANON.get((secretary or '').strip().lower(), secretary or '')
    ordered: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        if name and name.upper() not in seen:
            seen.add(name.upper())
            ordered.append(name)

    for raw_label, _turn_text, _start, _end in extract_speaker_turns(natural_text):
        add(normalize_speaker_label(raw_label, secretary))
    add(own_canon)
    return '; '.join(ordered)


# 'Canonical URL' is deliberately absent. enrich_row()'s --incremental skip
# tests every column in this list, so adding it would mark all already-enriched
# rows incomplete and re-run enrichment over the whole corpus -- silently
# recomputing Speakers Present, which is NOT a base column and therefore NOT
# covered by the verifier's Check Q baseline diff. It is filled separately, by
# backfill_canonical_urls().
_PHASE_1_2_COLUMNS = ['SOS Word Count', 'Natural Word Count', 'Row Hash', 'Speakers Present']


def backfill_canonical_urls(rows: list[dict[str, str]]) -> int:
    """Fill any empty 'Canonical URL' from 'URL' via the path-rename map.

    A rule applied on every run, not a one-shot migration script: it is
    idempotent, it self-heals a release produced before the column existed,
    and it needs no separate pass over a 477MB file. Rows that already carry
    a value are left alone, so a hand-corrected entry survives.
    """
    filled = 0
    for row in rows:
        if not (row.get('Canonical URL') or '').strip():
            row['Canonical URL'] = canonicalize_live_url(row.get('URL', ''))
            filled += 1
    return filled


def enrich_row(row: dict[str, str], *, incremental: bool) -> bool:
    """Populate the Phase 1-2 columns (_PHASE_1_2_COLUMNS) on `row` in place.

    Returns True if the row was (re)computed, False if skipped because
    --incremental was set and the row already carries a non-empty value for
    all four. None of these four are ever legitimately empty, so truthiness
    is a safe completeness signal here (contrast enrich_rows_ner(), which
    must use key presence instead since People/Countries Mentioned CAN be
    legitimately empty). Checking all four (not just Row Hash) makes
    --incremental self-healing across schema versions: a row enriched by an
    older version of this script (missing a column a later phase added)
    gets that column backfilled on the next incremental run, rather than
    being skipped as "already enriched."
    """
    if incremental and all((row.get(c) or '').strip() for c in _PHASE_1_2_COLUMNS):
        return False
    row['SOS Word Count'] = str(word_count(row.get('SOS Text', '')))
    row['Natural Word Count'] = str(word_count(row.get('Natural Text', '')))
    row['Row Hash'] = row_hash(row)
    row['Speakers Present'] = speakers_present(row.get('Natural Text', ''), row.get('Secretary', ''))
    return True


# ══════════════════════════════════════════════════════════════════════════
# Phase 4: Countries / Locations Mentioned
# ══════════════════════════════════════════════════════════════════════════
#
# NEVER uses pycountry.countries.search_fuzzy() -- verified during
# development that it produces dangerously wrong matches: "Republic of
# Korea" fuzzy-matches to NORTH Korea, "Kosovo" fuzzy-matches to Serbia.
# Only exact string matches (via the curated alias table below, or
# pycountry's own name/official_name/common_name fields) are used.

# US states + D.C. + territories that appear in the corpus's own Location
# column as the "country" position (e.g. "Austin, Texas", "Washington,
# D.C."). A free-text mention of any of these resolves to United States.
# "Georgia" and "Washington" are deliberately EXCLUDED (see
# _AMBIGUOUS_PLACE_NAMES below): both collide with a real country name
# (Georgia) or a national-capital/common-surname reading (Washington) and
# need row-context disambiguation, not a blanket mapping.
_US_STATES_AND_TERRITORIES = {
    'alabama', 'alaska', 'arizona', 'arkansas', 'california', 'colorado',
    'connecticut', 'delaware', 'florida', 'hawaii', 'idaho', 'illinois',
    'indiana', 'iowa', 'kansas', 'kentucky', 'louisiana', 'maine',
    'maryland', 'massachusetts', 'michigan', 'minnesota', 'mississippi',
    'missouri', 'montana', 'nebraska', 'nevada', 'new hampshire',
    'new jersey', 'new mexico', 'new york', 'north carolina',
    'north dakota', 'ohio', 'oklahoma', 'oregon', 'pennsylvania',
    'rhode island', 'south carolina', 'south dakota', 'tennessee', 'texas',
    'utah', 'vermont', 'virginia', 'west virginia', 'wisconsin', 'wyoming',
    'd.c.', 'washington, d.c.', 'district of columbia',
    'puerto rico', 'guam', 'american samoa', 'u.s. virgin islands',
    'northern mariana islands',
}
_AMBIGUOUS_PLACE_NAMES = {'georgia', 'washington'}

# Canonical-display-name overrides applied on top of pycountry's own
# `.name` field, keyed by alpha_3 (independent of which name field
# matched), so this corpus's free-text "Countries Mentioned" field uses
# the SAME spelling already established by the corpus's own structured
# `Location` column. Source: a full tail-token frequency scan of the
# published corpus's Location column (2026-08-24) cross-checked against
# pycountry's default names -- e.g. "Turkey" 66x vs "Türkiye" 1x (pycountry
# default), "Republic of Korea" 67x vs 0x "South Korea", "Burma" 38x with
# 0x "Myanmar", "Democratic Republic of the Congo" 18x. A few entries
# (Bolivia/Venezuela/Tanzania/Moldova/Brunei/Congo/Micronesia/Taiwan) have
# no direct corpus-precedent count but pycountry's default name is either
# an awkward formal-register form ("Bolivia, Plurinational State of") or,
# for Taiwan, reflects a specific political framing ("Taiwan, Province of
# China") that doesn't match how US diplomatic speech actually refers to
# it -- simplified to the common form a reader would expect.
_CANONICAL_NAME_OVERRIDES = {
    'TUR': 'Turkey',
    'MMR': 'Burma',
    'KOR': 'Republic of Korea',
    'PRK': 'North Korea',
    'COD': 'Democratic Republic of the Congo',
    'COG': 'Republic of the Congo',  # pycountry's bare "Congo" is ambiguous with DRC
    'CZE': 'Czech Republic',
    'RUS': 'Russia',
    'VNM': 'Vietnam',
    'LAO': 'Laos',
    'IRN': 'Iran',
    'SYR': 'Syria',
    'BOL': 'Bolivia',
    'VEN': 'Venezuela',
    'TZA': 'Tanzania',
    'MDA': 'Moldova',
    'BRN': 'Brunei',
    'FSM': 'Micronesia',
    'TWN': 'Taiwan',
    'PSE': 'Palestine',
}

# Entries pycountry has NO record of at all (not a recognized ISO 3166-1
# country), but which are legitimate places mentioned in diplomatic speech
# and already appear as Location-column values in this corpus. Kosovo has
# no official ISO 3166-1 alpha-3 code -- left blank rather than fabricated
# (a non-ISO-standard user-assigned code "XK" exists in some contexts but
# is not used here; the verifier's Check V special-cases this one blank
# entry rather than treating a blank code as a failure). West Bank and Gaza
# use Palestine's ISO code (PSE) but keep their own distinct display names
# per existing Location-column precedent (the corpus already lists "West
# Bank" and "Gaza" as separate Location values, not merged into
# "Palestine") -- mirrors what the Location column already does, takes no
# further position on Palestinian statehood.
_TIER_C_COUNTRIES = {
    'Kosovo': '',
    'West Bank': 'PSE',
    'Gaza': 'PSE',
}

# Natural-speech variant (lowercase) -> canonical display name (a key in
# _ISO3_BY_CANONICAL_NAME). Checked BEFORE any pycountry lookup. Combines
# (a) the corpus's own established Location-column conventions (mirrors
# corpus_finalize_v4_30.py's LOCATION_PATCHES CAT 5 comments: South
# Korea/Korea, People's Republic of China, etc.) and (b) common everyday
# English names that don't match any pycountry name field at all (Ivory
# Coast, Holland).
_COUNTRY_INPUT_ALIASES = {
    'türkiye': 'Turkey', 'turkiye': 'Turkey',
    'myanmar': 'Burma',
    'south korea': 'Republic of Korea', 'korea': 'Republic of Korea',
    "people's republic of china": 'China', 'peoples republic of china': 'China',
    'mainland china': 'China', 'prc': 'China',
    'netherlands': 'Netherlands', 'holland': 'Netherlands',
    "cote d'ivoire": "Côte d'Ivoire", 'ivory coast': "Côte d'Ivoire",
    'england': 'United Kingdom', 'britain': 'United Kingdom',
    'great britain': 'United Kingdom', 'uk': 'United Kingdom',
    'czechia': 'Czech Republic',
    'russian federation': 'Russia',
    'viet nam': 'Vietnam',
    "lao people's democratic republic": 'Laos', 'lao pdr': 'Laos',
    'islamic republic of iran': 'Iran',
    'syrian arab republic': 'Syria',
    'democratic republic of congo': 'Democratic Republic of the Congo',
    'drc': 'Democratic Republic of the Congo',
    'congo-brazzaville': 'Republic of the Congo',
    'macedonia': 'North Macedonia',
    'former yugoslav republic of macedonia': 'North Macedonia', 'fyrom': 'North Macedonia',
    'swaziland': 'Eswatini',
    'cape verde': 'Cabo Verde',
    'bahamas': 'Bahamas',
    'state of palestine': 'Palestine', 'palestinian territories': 'Palestine',
    # Found via a full-corpus unresolved-GPE audit (2026-08-24), high-frequency:
    'united states': 'United States', 'u.s.': 'United States', 'us': 'United States',
    'united states of america': 'United States', 'america': 'United States',
    'zaire': 'Democratic Republic of the Congo',  # pre-1997 name for the DRC
    'bosnia': 'Bosnia and Herzegovina', 'bosnia-herzegovina': 'Bosnia and Herzegovina',
    'bosnia.': 'Bosnia and Herzegovina',  # sentence-final period sometimes captured in the span
    # Northern Ireland is constitutionally part of the UK -- same treatment
    # as a US state resolving to United States, not a separate country.
    'northern ireland': 'United Kingdom',
    # spaCy sometimes splits the compound country name into two separate
    # GPE spans ("Trinidad" and "Tobago" mentioned individually rather than
    # as one "Trinidad and Tobago" span) -- found via the unresolved-GPE
    # audit (Trinidad: 36x, Tobago: 40x).
    'trinidad': 'Trinidad and Tobago', 'tobago': 'Trinidad and Tobago',
}

# Defunct/historical political entities mentioned in speeches spanning
# 1997-2026 that have no modern ISO 3166-1 equivalent to map to without
# being misleading (the Soviet Union is not simply "Russia"; Yugoslavia and
# Czechoslovakia each split into multiple modern states with no single
# correct successor). Treated like Kosovo -- a recognized canonical entry
# with a blank ISO code, not silently dropped and not force-mapped to a
# successor state. Found via the same unresolved-GPE audit.
_HISTORICAL_ENTITIES = {
    'soviet union': 'Soviet Union',
    'the soviet union': 'Soviet Union',
    'yugoslavia': 'Yugoslavia',
    'czechoslovakia': 'Czechoslovakia',
}

# Major world capitals/cities that appeared frequently enough in the
# unresolved-GPE audit to be worth resolving to their country (a mention of
# "Beijing" is informative about China being discussed). NOT a general
# gazetteer -- deliberately small and evidence-driven (>=10 occurrences in
# the audit), not an attempt at exhaustive city coverage. Jerusalem and
# Hebron are DELIBERATELY EXCLUDED despite meeting the frequency bar --
# resolving either to a specific country would take an implicit political
# position this corpus should not take silently; left unresolved instead.
_CITY_TO_COUNTRY = {
    'madrid': 'Spain', 'beijing': 'China', 'helsinki': 'Finland',
    'moscow': 'Russia', 'paris': 'France', 'sarajevo': 'Bosnia and Herzegovina',
    'tokyo': 'Japan', 'prague': 'Czech Republic', 'seoul': 'Republic of Korea',
    'brussels': 'Belgium', 'london': 'United Kingdom', 'geneva': 'Switzerland',
    # 'hague' not 'the hague' -- resolve_country() strips a leading "the "
    # BEFORE this lookup runs, so a "the X" key here would never match (a
    # real bug found via the unresolved-GPE audit: "The Hague" was still
    # showing up unresolved despite this entry existing with the old key).
    'hague': 'Netherlands',
    'baghdad': 'Iraq', 'berlin': 'Germany',
    'belgrade': 'Serbia', 'dayton': 'United States', 'denver': 'United States',
    'san francisco': 'United States', 'miami': 'United States',
    'oslo': 'Norway', 'santiago': 'Chile', 'doha': 'Qatar',
}


def _build_iso3_by_name() -> dict[str, str]:
    """Canonical-display-name -> ISO alpha-3, built at import time from
    pycountry.countries with _CANONICAL_NAME_OVERRIDES applied, plus
    _TIER_C_COUNTRIES and _HISTORICAL_ENTITIES (both blank-code entries).
    Empty ({}) if pycountry isn't installed -- Phase 4 columns are then
    left blank for every row (see resolve_country()).
    """
    table: dict[str, str] = {}
    if _pycountry is not None:
        for c in _pycountry.countries:
            name = _CANONICAL_NAME_OVERRIDES.get(c.alpha_3, c.name)
            table[name] = c.alpha_3
    table.update(_TIER_C_COUNTRIES)
    table.update({name: '' for name in set(_HISTORICAL_ENTITIES.values())})
    return table


_ISO3_BY_CANONICAL_NAME = _build_iso3_by_name()
_MAX_UNRESOLVED_LOG = 5000  # cap so a pathological row can't blow up memory


def resolve_country(mention_text: str, row_location_country: str) -> tuple[str, str] | None:
    """Resolve a raw GPE/LOC mention to (canonical_name, iso_alpha3).
    Returns None if unresolved (caller logs to the unresolved-GPE audit).

    `row_location_country` is the row's own already-standardized `Location`
    column's tail token (the part after the last comma) -- used ONLY to
    disambiguate _AMBIGUOUS_PLACE_NAMES ("Georgia" the country vs. the US
    state; "Washington" the US state vs. the capital/a surname): if the
    row's own Location country is "United States", an ambiguous mention
    defaults to the US-state reading; otherwise it defaults to the country
    reading. This is a best-effort heuristic, not authoritative -- flagged
    as such in the module docstring's known-limitations note.
    """
    if not mention_text:
        return None
    # Normalize: collapse internal whitespace/newlines (a spaCy span can
    # cross a line break, e.g. "the United\nStates"), strip a leading
    # "the "/"The " article, and strip a trailing possessive ('s or ')
    # -- found via a full-corpus unresolved-mention audit: "the United
    # States" (682x), "the Republic of Korea" (20x), "Hong Kong's" (28x),
    # "North Korea's" (16x) were all going unresolved purely because of
    # this surface-form noise, not because the underlying place was
    # actually unrecognized.
    key = re.sub(r'\s+', ' ', mention_text).strip()
    key = re.sub(r'^the\s+', '', key, flags=re.IGNORECASE)
    key = re.sub(r"['’]s?$", '', key)
    key = key.strip().lower()
    if not key:
        return None

    if key in _AMBIGUOUS_PLACE_NAMES:
        # US-context check: the row's own Location country counts as US
        # context not just when it's literally "United States"/"USA"/etc,
        # but also when it's "D.C." or any US state -- e.g. a row located
        # in "Seattle, Washington" or "Anytown, D.C." is itself evidence
        # the row is US-based. Found via a full-corpus unresolved-GPE
        # audit: "Washington" was going unresolved in 103 rows whose own
        # Location was "..., D.C." alone (almost certainly meaning the US
        # federal government -- "Washington believes...", "sources in
        # Washington say..."), plus dozens more across individual US
        # states (Hawaii, New York, Delaware, California, ...) -- the
        # original check only recognized the literal string "United
        # States" and missed all of these. "Georgia" never hit this gap
        # (it always resolves one way or the other via the fallback below).
        # (_US_STATES_AND_TERRITORIES already includes 'd.c.' and its
        # variants, so checking it alone covers those too.)
        loc_key = (row_location_country or '').strip().lower()
        if loc_key in ('united states', 'usa', 'u.s.', 'us') or loc_key in _US_STATES_AND_TERRITORIES:
            return 'United States', _ISO3_BY_CANONICAL_NAME.get('United States', '')
        # else: fall through to the country reading (Georgia the country;
        # "Washington" with no clear US context is left unresolved rather
        # than guessed, since "Washington" alone is more often the D.C.
        # shorthand or a surname than the state).
        if key == 'georgia':
            return 'Georgia', _ISO3_BY_CANONICAL_NAME.get('Georgia', '')
        return None

    if key in _US_STATES_AND_TERRITORIES:
        return 'United States', _ISO3_BY_CANONICAL_NAME.get('United States', '')

    if key in _COUNTRY_INPUT_ALIASES:
        canon = _COUNTRY_INPUT_ALIASES[key]
        return canon, _ISO3_BY_CANONICAL_NAME.get(canon, '')

    if key in _HISTORICAL_ENTITIES:
        canon = _HISTORICAL_ENTITIES[key]
        return canon, ''

    if key in _CITY_TO_COUNTRY:
        canon = _CITY_TO_COUNTRY[key]
        return canon, _ISO3_BY_CANONICAL_NAME.get(canon, '')

    # Exact match against the canonical-name table itself (mention text IS
    # already a recognized canonical name, e.g. "China", "France").
    for canon, iso3 in _ISO3_BY_CANONICAL_NAME.items():
        if canon.lower() == key:
            return canon, iso3

    return None


# Placeholder written into Country ISO Codes Mentioned at the position of
# any no-ISO-code entry (Kosovo, Soviet Union, Yugoslavia, Czechoslovakia),
# so the two output columns stay positionally 1:1 -- codes.split('; ')[i]
# always corresponds to names.split('; ')[i], with no filtering needed on
# the consumer's side. Simply DROPPING the blank (the original design) was
# a real bug found via output review: 1,521/17,595 rows (8.6%) had a
# names/codes count mismatch, meaning a naive positional zip() would
# silently misalign every country after the first no-code entry in that
# row. 'N/A' can never collide with a real code (ISO 3166-1 alpha-3 codes
# are exactly 3 uppercase letters; Check W's malformed-code check treats
# 'N/A' as the one allowed exception, not a violation).
_NO_ISO_CODE_PLACEHOLDER = 'N/A'


def countries_mentioned(doc, row_location: str, unresolved_log: list[tuple[str, str]] | None = None) -> tuple[str, str]:
    """Extract (Countries Mentioned, Country ISO Codes Mentioned) from a
    spaCy Doc's GPE/LOC entities. Always the SAME count, in the SAME order,
    between the two output strings -- a no-ISO-code entry (Kosovo, Soviet
    Union, ...) gets the literal placeholder 'N/A' in the codes string
    rather than being dropped, so position i in one column always
    corresponds to position i in the other.
    """
    if doc is None:
        return '', ''
    row_country = (row_location or '').rsplit(',', 1)[-1].strip()
    names: list[str] = []
    codes: list[str] = []
    seen: set[str] = set()
    for ent in doc.ents:
        if ent.label_ not in ('GPE', 'LOC'):
            continue
        resolved = resolve_country(ent.text, row_country)
        if resolved is None:
            if unresolved_log is not None and len(unresolved_log) < _MAX_UNRESOLVED_LOG:
                unresolved_log.append((ent.text, row_country))
            continue
        canon, iso3 = resolved
        if canon in seen:
            continue
        seen.add(canon)
        names.append(canon)
        codes.append(iso3 or _NO_ISO_CODE_PLACEHOLDER)
    return '; '.join(names), '; '.join(codes)


# ══════════════════════════════════════════════════════════════════════════
# Phase 3: People Mentioned
# ══════════════════════════════════════════════════════════════════════════

_GREETING_VOCATIVE_RE = re.compile(
    r'(?:Thank you|Thanks|Well|Good morning|Good afternoon|Good evening|Hello|Welcome)\s*,\s*$',
    re.IGNORECASE,
)
_POSSESSIVE_RE = re.compile(r"['’]s?\s*$")  # 's? -- names ending in "s" take a bare apostrophe ("Ross'"/"Abbas'"), not "'s"
_LEADING_ARTICLE_RE = re.compile(r'^(?:the|a|an)\s+', re.IGNORECASE)
# A leading em/en-dash separator ("the Secretary of Defense -- Bob Gates --
# announced") occasionally leaks into the PERSON span itself; a trailing
# comma/period/semicolon leaks in from adjoining punctuation ("Beatriz
# Roque,", "Igor Ivanov."). Both found via the distributional near-
# duplicate scan while investigating a "-- Bob Gates"/"Bob Gates" group.
_LEADING_DASH_RE = re.compile(r'^-+\s*')
_TRAILING_PUNCT_RE = re.compile(r'[,.;]+\s*$')
_MAX_PERSON_NAME_CHARS = 60  # spaCy PERSON spans are usually short; a long one is almost always a mis-tag
# A colon inside a "name" is always a title-with-subtitle ("Saddam Hussein:
# Disarm") or a leaked speaker-label fragment ("COL CLARK:"), never a real
# name; a URL is never a name either. A "Ramadan/Eid Kareem/Karim/Mubarak"
# span is the fixed Islamic holiday greeting idiom, not a person -- narrowly
# scoped to that exact two-word pattern so it doesn't also catch real names
# that happen to share one of those words (e.g. "Hosni Mubarak" -- "Hosni"
# isn't "Ramadan"/"Eid" so this regex doesn't match it). A digit anywhere in
# the span is essentially never a real name in this corpus -- a full-corpus
# frequency scan found 495 digit-containing "People Mentioned" spans and
# 494 were military hardware (F-16s, AK-47s, S-400), UN resolutions/
# executive orders (Resolution 1441, E.O. 13818), flight numbers (MH17,
# PS752), or similar; accepted the loss of the ~1 borderline case ("George
# Bush 43") given the overwhelming precision gain. All found via a
# full-corpus run's People Mentioned frequency scan.
_NOT_A_NAME_RE = re.compile(
    r':|https?://|www\.|\d|^(?:Ramadan|Eid)\s+(?:Kareem|Karim|Mubarak)$',
    re.IGNORECASE,
)

# A bare title/role word with no attached name ("the Minister", "Mr.
# Secretary") is genuine content -- someone really was addressed or
# referred to this way -- but carries no information about WHO, so it
# doesn't belong in a column meant to answer that question. Found via a
# full-corpus scan specifically for "doesn't read like a proper noun"
# entries: "Minister" (9x, all genuinely bare -- "Mr. Minister", "the
# Minister, and I now invite him...") and "Secretary" (3x) were the two
# with real volume; "Council" was a single bare occurrence.
_BARE_TITLE_NAMES = {'minister', 'secretary', 'council'}

# Specific well-known organizations/places that spaCy occasionally tags
# PERSON despite being places or institutions, not people. Kept as an exact
# allowlist (not a general "contains House" rule) since "House" alone is a
# real, if uncommon, surname -- these three specific names are the actual
# false positives observed, not the word "House" in general.
_NOT_A_PERSON_EXACT = {'blair house', 'spaso house', 'freedom house', 'united nations'}

# A multi-word span ending in one of these institutional/organizational
# words is a reference to an INSTITUTION, not a direct mention of the
# person whose name it may contain -- e.g. "Obama Administration" is a
# reference to a presidency, and "Obama" himself is already captured
# separately (3,685x) wherever the text names him directly; "Carl Vinson
# Strike Group" / "Theodore Roosevelt Carrier Strike Group" are US Navy
# formations named after historical figures, not mentions of those people.
# Essentially zero risk of rejecting a real surname (none of these words
# are used as surnames in this corpus). Found via a full-corpus org-word
# scan of People Mentioned.
_ORG_SUFFIX_RE = re.compile(r'\b(?:Administration|Government|Institute|Program|Office|Group)\s*$', re.IGNORECASE)


def _clean_person_name(raw: str) -> str:
    name = (raw or '').strip()
    name = _LEADING_DASH_RE.sub('', name).strip()
    name = _TRAILING_PUNCT_RE.sub('', name).strip()
    name = _POSSESSIVE_RE.sub('', name).strip()
    name = _LEADING_ARTICLE_RE.sub('', name).strip()
    name = re.sub(r'\s+', ' ', name)
    # A genuinely ALL-CAPS extraction (from an all-caps passage in the
    # source text) gets title-cased so it merges with the same person's
    # normal-case mentions elsewhere in the corpus instead of appearing as
    # a separate distinct entry ("BIDEN" vs "Biden") -- found via a
    # distributional near-duplicate scan across the whole corpus (65
    # candidate groups in People Mentioned, dominated by this exact
    # pattern: BIDEN/CLINTON/ARAFAT/KERRY/RICE/POWELL/BLINKEN/ALBRIGHT/WANG
    # each had a handful of all-caps occurrences separate from their normal-
    # case majority). Deliberately does NOT touch already-mixed-case text
    # (e.g. "bin Laden", "Kim Jong-un", "Bashar al-Assad") -- those are
    # legitimate transliteration/spelling variance in the source across
    # different eras/speeches, not a casing bug, and forcing a single
    # "correct" spelling among them would be presumptuous.
    if name.isupper() and any(ch.isalpha() for ch in name):
        name = _titlecase(name)
    # A genuinely ALL-LOWERCASE extraction is rejected outright (not
    # title-cased) -- the opposite asymmetric treatment from all-caps
    # above, and deliberately so: real person names are essentially never
    # rendered all-lowercase in this corpus's genre (formal diplomatic
    # transcripts), so an all-lowercase PERSON tag is almost always a
    # genuine spaCy mis-tag, not a stylistic name variant. Found via the
    # same distributional near-duplicate scan (investigating a "Bush"/
    # "bush" candidate group): confirmed EVERY lowercase "bush" occurrence
    # was the literal word (wilderness/"beat around the bush"/"burning
    # bush"), none were about a President Bush -- titlecasing it would have
    # created 11 false mentions. A broader scan of all 160 distinct all-
    # lowercase People Mentioned tokens found the same pattern at scale:
    # "mike" (77x, clearly "microphone"), foreign-language closing phrases
    # ("merci beaucoup", "asante sana", "gracias"), sentence-fragment
    # dashes ("that's--"), and ordinal-suffix debris ("nd anniversary").
    elif name.islower():
        return ''
    return name


def _turn_containing(offset: int, turns: list[dict]) -> int | None:
    for i, t in enumerate(turns):
        if t['start'] <= offset < t['end']:
            return i
    return None


# Boundary-character sets for rule 6 (vocative-comma-set-off). '' (empty)
# means start-of-turn (nothing before the name at all).
_CLAUSE_LEFT_BOUNDARY_CHARS = set('.!?\n,-—)') | {''}
_CLAUSE_RIGHT_BOUNDARY_CHARS = set(',.!?')


def _last_nonspace_char(s: str) -> str:
    for ch in reversed(s):
        if ch not in ' \t':
            return ch
    return ''


def _first_nonspace_char(s: str) -> str:
    for ch in s:
        if ch not in ' \t':
            return ch
    return ''


def _annotate_turns(raw_turns: list[tuple[str, str, int, int]], secretary: str) -> list[dict]:
    own_canon = SECRETARY_CANON.get((secretary or '').strip().lower(), secretary or '')
    out = []
    for raw_label, turn_text, start, end in raw_turns:
        norm = normalize_speaker_label(raw_label, secretary)
        out.append({'speaker': norm, 'is_own': norm == own_canon, 'text': turn_text, 'start': start, 'end': end})
    return out


def _direct_address_reason(ent, full_text: str, own_lname: str, turn: dict | None, adjacent_speakers: list[str]) -> str | None:
    """Return a short rule-tag if `ent` (a spaCy PERSON span) should be
    dropped as direct address / self-reference, else None (keep as a
    genuine third-person mention). Rules, in order:

    1. own-secretary: the span names the row's own Secretary and falls
       inside their OWN turn (already covered by Speakers Present -- would
       just be a redundant self-reference, not a "mention").
    2. greeting-vocative: immediately preceded by "Thank you,"/"Well,"/etc.
       at a clause start -- classic "Thank you, Mary," direct address.
    3. bare-name-turn: the ENTIRE turn is just this name (<=4 words) --
       e.g. "Sheikh Saad." as its own turn is the Secretary yielding the
       floor to that person, i.e. direct address, not a description of them.
    4. followed-by-question-mark: name immediately followed by "?" in the
       same clause (e.g. "..., John?").
    5. adjacent-speaker-address: last-name token overlaps the speaker of
       the immediately preceding or following turn -- covers interview
       address (e.g. "..., Larry," next to a "MR. KING:" turn).
    6. vocative-comma-set-off: the name sits at a clause boundary (start of
       turn, or right after '.'/'!'/'?'/'\n'/','/')') AND is immediately
       followed by ','/'.'/'!'/'?' -- catches the dominant pattern found in
       a full-corpus scan: an interviewer addressed repeatedly by bare
       first name with NO turn label of their own to match via rule 5
       (their turns are generically labeled "QUESTION:"), e.g. "John, you've
       talked...", "You know,\nJohn, sometimes...", "Thank you very much,
       \nJohn." ("John"/"Tony"/"David"/"Bill"/"George" were 149-450 rows
       each before this rule, almost entirely this pattern on inspection).
       Gated to turn is not None (dialogue transcripts) since narrative
       prose (Op-Eds, single-turn speeches) more plausibly uses the same
       punctuation shape for a legitimate appositive ("Zelenskyy, the
       Ukrainian president, ..."), which this corpus's Q&A-heavy interview
       format makes comparatively rare in dialogue but not zero in prose.

    Everything else survives as a third-person mention (rule 7 in the
    design doc: "President Putin has said...").
    """
    before = full_text[max(0, ent.start_char - 30):ent.start_char]
    after = full_text[ent.end_char:ent.end_char + 15]

    if turn is not None and turn['is_own'] and own_lname and own_lname in ent.text.upper():
        return 'own-secretary'

    if _GREETING_VOCATIVE_RE.search(before):
        return 'greeting-vocative'

    if turn is not None:
        turn_stripped = turn['text'].strip().rstrip('.!').strip()
        if turn_stripped and len(turn_stripped.split()) <= 4 and ent.text.strip() in turn_stripped:
            return 'bare-name-turn'

    if after.lstrip().startswith('?'):
        return 'followed-by-question-mark'

    if adjacent_speakers:
        ent_last = ent.text.strip().split()[-1].upper().rstrip('.,') if ent.text.strip() else ''
        for adj in adjacent_speakers:
            adj_last = adj.strip().split()[-1].upper().rstrip('.,') if adj and adj.strip() else ''
            if adj_last and ent_last and ent_last == adj_last:
                return 'adjacent-speaker-address'

    if turn is not None:
        left_ch = _last_nonspace_char(before)
        right_ch = _first_nonspace_char(after)
        if left_ch in _CLAUSE_LEFT_BOUNDARY_CHARS and right_ch in _CLAUSE_RIGHT_BOUNDARY_CHARS:
            return 'vocative-comma-set-off'

    return None


def people_mentioned(
    doc, natural_text: str, secretary: str,
    audit_log: list[tuple[str, str, str, str]] | None = None,
) -> tuple[str, str]:
    """Extract (People Mentioned (Natural Text), People Mentioned (SOS Text))
    from a spaCy Doc's PERSON entities, filtering direct address via
    _direct_address_reason(). `audit_log`, if given, gets
    (row-identifying text unused here, ent.text, verdict, rule) tuples
    appended for a sampled human-review sidecar -- see write_people_audit().
    """
    if doc is None or not (natural_text or '').strip():
        return '', ''
    raw_turns = extract_speaker_turns(natural_text)
    annotated = _annotate_turns(raw_turns, secretary)
    own_canon = SECRETARY_CANON.get((secretary or '').strip().lower(), secretary or '')
    own_lname = LASTNAME.get(own_canon, '').upper()

    kept_natural: list[str] = []
    kept_sos: list[str] = []
    seen: set[str] = set()

    for ent in doc.ents:
        if ent.label_ != 'PERSON':
            continue
        if _NOT_A_NAME_RE.search(ent.text):
            # spaCy occasionally tags a colon-containing title fragment
            # ("Saddam Hussein: Disarm", "COL CLARK:" -- a raw speaker
            # label spaCy mis-included the colon in) or a literal URL as
            # PERSON. Neither is ever a real name -- found via a full-
            # corpus run (Check V in the verifier).
            if audit_log is not None:
                audit_log.append((ent.text, 'dropped', 'not-a-name', ''))
            continue
        if annotated:
            turn_idx = _turn_containing(ent.start_char, annotated)
            if turn_idx is None:
                if audit_log is not None:
                    audit_log.append((ent.text, 'dropped', 'label-fragment', ''))
                continue
            turn = annotated[turn_idx]
            adjacent = []
            if turn_idx > 0:
                adjacent.append(annotated[turn_idx - 1]['speaker'])
            if turn_idx + 1 < len(annotated):
                adjacent.append(annotated[turn_idx + 1]['speaker'])
        else:
            turn, adjacent = None, []

        reason = _direct_address_reason(ent, natural_text, own_lname, turn, adjacent)
        if reason is not None:
            if audit_log is not None:
                audit_log.append((ent.text, 'dropped', reason, ''))
            continue

        name = _clean_person_name(ent.text)
        if not name or len(name) > _MAX_PERSON_NAME_CHARS:
            if audit_log is not None:
                audit_log.append((ent.text, 'dropped', 'empty-or-too-long', ''))
            continue
        if name.lower() in _BARE_TITLE_NAMES or name.lower() in _NOT_A_PERSON_EXACT or _ORG_SUFFIX_RE.search(name):
            if audit_log is not None:
                audit_log.append((ent.text, 'dropped', 'bare-title-or-org', ''))
            continue
        key = name.upper()
        if key in seen:
            continue
        seen.add(key)
        kept_natural.append(name)
        if turn is None or turn['is_own']:
            kept_sos.append(name)
        if audit_log is not None:
            audit_log.append((ent.text, 'kept', '', name))

    return '; '.join(kept_natural), '; '.join(kept_sos)


# ══════════════════════════════════════════════════════════════════════════
# Shared spaCy NER pass (feeds both Phase 3 and Phase 4)
# ══════════════════════════════════════════════════════════════════════════

_NLP_CACHE: dict[str, object] = {}


def get_nlp(model_name: str):
    """Lazily load (and cache) a spaCy model. Only called when NER phases
    actually run (--skip-ner bypasses this entirely), so Phase 1-2-only
    runs never pay the spaCy import/load cost.
    """
    if model_name not in _NLP_CACHE:
        import spacy
        _NLP_CACHE[model_name] = spacy.load(model_name)
    return _NLP_CACHE[model_name]


def enrich_rows_ner(
    rows: list[dict[str, str]], nlp, *, incremental: bool,
    batch_size: int = 50, n_process: int = 1,
    audit_log: list | None = None, unresolved_log: list | None = None,
) -> int:
    """Populate _NER_COLUMNS on every row needing it, via one shared
    nlp.pipe() pass over `Natural Text` (PERSON spans feed Phase 3, GPE/LOC
    spans feed Phase 4 -- NOT two separate nlp() calls). Returns the number
    of rows (re)computed.

    --incremental uses KEY PRESENCE (`c in row`), not truthiness, as the
    completeness signal -- People/Countries Mentioned can be legitimately
    empty for a row with no third-party mentions, which must be
    distinguished from "never computed" (key absent entirely, e.g. a row
    read from an older-schema CSV via csv.DictReader).
    """
    if incremental:
        targets = [r for r in rows if not all(c in r for c in _NER_COLUMNS)]
    else:
        targets = rows
    if not targets:
        return 0

    texts = [r.get('Natural Text', '') or '' for r in targets]
    docs = nlp.pipe(texts, batch_size=batch_size, n_process=n_process)
    for row, doc in zip(targets, docs):
        pm_nat, pm_sos = people_mentioned(doc, row.get('Natural Text', ''), row.get('Secretary', ''), audit_log=audit_log)
        row['People Mentioned (Natural Text)'] = pm_nat
        row['People Mentioned (SOS Text)'] = pm_sos
        cm_names, cm_codes = countries_mentioned(doc, row.get('Location', ''), unresolved_log=unresolved_log)
        row['Countries Mentioned'] = cm_names
        row['Country ISO Codes Mentioned'] = cm_codes
    return len(targets)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(manifest_path: Path, csv_path: Path, rows: list[dict[str, str]]) -> None:
    fingerprint = hashlib.sha256(
        ''.join(r.get('Row Hash', '') for r in rows).encode('utf-8')
    ).hexdigest()
    manifest = {
        'schema_version': 1,
        'generated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'row_count': len(rows),
        'columns': OUTPUT_COLUMNS,
        'csv_sha256': sha256_file(csv_path),
        'dataset_fingerprint_sha256': fingerprint,
        'generator_script': 'corpus_enrich_v1.py',
    }
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2)
        f.write('\n')


def write_people_audit(path: Path, audit_log: list[tuple[str, str, str, str]], sample_size: int = 200) -> None:
    """Sample up to `sample_size` (ent_text, verdict, rule, kept_name)
    entries for human spot-check of the direct-address heuristic.
    """
    import random
    sample = audit_log if len(audit_log) <= sample_size else random.sample(audit_log, sample_size)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['entity_text', 'verdict', 'rule', 'kept_name'])
        writer.writerows(sample)


def write_unresolved_gpe_audit(path: Path, unresolved_log: list[tuple[str, str]]) -> None:
    """Frequency-count unresolved GPE/LOC mentions (text, row's own
    Location country) for extending the country alias table later.
    """
    from collections import Counter
    counts = Counter(unresolved_log)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['mention_text', 'row_location_country', 'count'])
        for (text, loc_country), n in sorted(counts.items(), key=lambda kv: -kv[1]):
            writer.writerow([text, loc_country, n])


def write_component_distribution_report(prefix: Path, rows: list[dict[str, str]]) -> None:
    """Write a frequency/percentile distribution report for the enrichment
    stage's list-valued columns (Speakers Present, People Mentioned x2,
    Countries Mentioned, Country ISO Codes Mentioned) -- the durable,
    repeatable version of the ad-hoc analysis that found real bugs during
    development (Check Y's near-duplicate scan, the Washington/US-state
    disambiguation gap, etc.). Mirrors corpus_finalize_v4_30.py's
    write_distribution_report() sidecar convention: a .pkl (Counter objects,
    for programmatic reuse -- e.g. diffing against a prior run) and a .txt
    (human-readable, for manual review) written together.
    """
    import pickle
    from collections import Counter
    from datetime import date as _date_cls

    def _pct_stats(values: list[int]) -> dict:
        if not values:
            return {}
        s = sorted(values)
        n = len(s)

        def _p(p: float) -> int:
            return s[max(0, int(n * p) - 1)]

        return {
            'count': n, 'min': s[0], 'p5': _p(0.05), 'p25': _p(0.25),
            'median': s[n // 2], 'p75': _p(0.75), 'p95': _p(0.95), 'p99': _p(0.99),
            'max': s[-1], 'mean': round(sum(s) / n, 2),
        }

    list_cols = [c for c in (
        'Speakers Present', 'People Mentioned (Natural Text)',
        'People Mentioned (SOS Text)', 'Countries Mentioned',
        'Country ISO Codes Mentioned',
    ) if rows and c in rows[0]]

    token_counters: dict[str, Counter] = {c: Counter() for c in list_cols}
    count_lists: dict[str, list[int]] = {c: [] for c in list_cols}
    for r in rows:
        for c in list_cols:
            tokens = [t.strip() for t in (r.get(c) or '').split(';') if t.strip()]
            count_lists[c].append(len(tokens))
            token_counters[c].update(tokens)

    n_total = len(rows)
    count_stats = {c: _pct_stats(count_lists[c]) for c in list_cols}

    payload = {
        'meta': {'generated': str(_date_cls.today()), 'row_count': n_total},
        'token_counters': token_counters,
        'per_row_count_stats': count_stats,
    }
    pkl_path = prefix.with_name(prefix.name + '_component_distribution.pkl')
    with open(pkl_path, 'wb') as fh:
        pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)

    txt_path = prefix.with_name(prefix.name + '_component_distribution.txt')
    sep = '-' * 60
    lines: list[str] = [
        f'=== Component Distribution Report: {prefix.name}  ({_date_cls.today()}) ===',
        f'Total rows: {n_total:,}', '',
    ]
    for c in list_cols:
        s = count_stats[c]
        lines += [sep, f'{c} -- entries per row', '']
        for label in ('count', 'min', 'p5', 'p25', 'median', 'p75', 'p95', 'p99', 'max', 'mean'):
            lines.append(f'  {label:<8}  {s.get(label, "n/a")}')
        lines.append('')
        cnt = token_counters[c]
        lines += [f'{c} -- top 30 of {len(cnt):,} unique values', '']
        for val, n in cnt.most_common(30):
            pct = 100.0 * n / n_total if n_total else 0
            lines.append(f'  {n:6,}  {pct:5.1f}%   {val}')
        lines.append('')

    with open(txt_path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines))
    print(f'Component distribution report: {txt_path}')
    print(f'Component distribution pickle: {pkl_path}')


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description='SOS corpus enrichment stage (v1: word counts + checksums + '
                     'Speakers Present + People/Countries Mentioned)'
    )
    ap.add_argument('--input', required=True, type=Path)
    ap.add_argument('--output', required=True, type=Path)
    ap.add_argument('--manifest-path', type=Path, default=None,
                     help='Defaults to <output>.manifest.json')
    ap.add_argument('--audit-prefix', default='v1_enrich',
                     help='Prefix for audit sidecars: <prefix>_people_mention_audit.csv '
                          '(sampled kept/excluded PERSON-span verdicts) and '
                          '<prefix>_unresolved_gpe_audit.csv (frequency-counted GPE/LOC '
                          'mentions the country alias table could not resolve). Written '
                          'only when the NER pass actually runs (not with --skip-ner).')
    ap.add_argument('--incremental', action='store_true',
                     help='Only compute enrichment for rows missing a current column '
                          '(e.g. freshly merged rows in the weekly CI update) '
                          'instead of recomputing the whole corpus.')
    ap.add_argument('--skip-ner', action='store_true',
                     help='Run only Phases 1-2 (word counts, checksums, Speakers '
                          'Present) -- no spaCy model load, no People/Countries '
                          'Mentioned columns computed this run.')
    ap.add_argument('--spacy-model', default='en_core_web_lg',
                     help='spaCy model for the NER pass (default: en_core_web_lg -- '
                          'materially better PERSON recall on transcript-style text '
                          'than en_core_web_sm; use en_core_web_sm only for fast dev '
                          'iteration on the direct-address heuristic).')
    ap.add_argument('--ner-batch-size', type=int, default=50,
                     help='nlp.pipe() batch_size.')
    ap.add_argument('--n-process', type=int, default=1,
                     help='nlp.pipe() n_process -- tune per host; multi-process spaCy '
                          'has real memory overhead per worker, start at 1.')
    ap.add_argument('--drop-secretaries', nargs='+', default=[], metavar='LASTNAME',
                     help='Ablation: exclude rows whose Secretary matches one of these '
                          'canonical last names, then renumber Row Id 0..N-1 before '
                          'enrichment. Surviving Row Hashes are unchanged (they exclude '
                          'Row Id). Names must be in SECRETARY_CANON.')
    return ap


def main() -> None:
    args = build_parser().parse_args()
    rows = read_csv(args.input)

    if args.drop_secretaries:
        drop = {name.strip() for name in args.drop_secretaries}
        unknown = drop - set(SECRETARY_CANON.values())
        if unknown:
            sys.exit(f'ERROR: --drop-secretaries names not in SECRETARY_CANON: {sorted(unknown)}')
        kept = [r for r in rows if r.get('Secretary') not in drop]
        for name in sorted(drop):
            n = sum(1 for r in rows if r.get('Secretary') == name)
            print(f'--drop-secretaries: dropped {n} row(s) for {name}')
        rows = kept
        for i, row in enumerate(rows):
            row['Row Id'] = str(i)
        print(f'--drop-secretaries: {len(rows)} rows remain, Row Id renumbered 0..{len(rows) - 1}')

    computed = 0
    for row in rows:
        if enrich_row(row, incremental=args.incremental):
            computed += 1

    ner_computed = 0
    if not args.skip_ner:
        nlp = get_nlp(args.spacy_model)
        audit_log: list[tuple[str, str, str, str]] = []
        unresolved_log: list[tuple[str, str]] = []
        ner_computed = enrich_rows_ner(
            rows, nlp, incremental=args.incremental,
            batch_size=args.ner_batch_size, n_process=args.n_process,
            audit_log=audit_log, unresolved_log=unresolved_log,
        )
        audit_dir = args.output.parent
        people_audit_path = audit_dir / f'{args.audit_prefix}_people_mention_audit.csv'
        unresolved_audit_path = audit_dir / f'{args.audit_prefix}_unresolved_gpe_audit.csv'
        write_people_audit(people_audit_path, audit_log)
        write_unresolved_gpe_audit(unresolved_audit_path, unresolved_log)
        print(f'Wrote {people_audit_path}')
        print(f'Wrote {unresolved_audit_path}')

    write_csv(args.output, rows)

    manifest_path = args.manifest_path or args.output.with_name(args.output.name + '.manifest.json')
    write_manifest(manifest_path, args.output, rows)

    dist_prefix = args.output.parent / args.audit_prefix
    write_component_distribution_report(dist_prefix, rows)

    mode = 'incremental' if args.incremental else 'full'
    print(f'Enriched {computed}/{len(rows)} rows Phase 1-2 ({mode} mode).')
    if not args.skip_ner:
        print(f'Enriched {ner_computed}/{len(rows)} rows Phase 3-4 NER ({mode} mode).')
    print(f'Wrote {args.output}')
    print(f'Wrote manifest {manifest_path}')


if __name__ == '__main__':
    main()
