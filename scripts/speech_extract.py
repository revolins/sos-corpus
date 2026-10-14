#!/usr/bin/env python3
"""speech_extract.py — standalone speech text enrichment for the SoS corpus.

Reads the patched fused CSV (preferred) or the plain fused CSV as fallback.
For each row, retrieves archived page text from checkpoint JSON files, then
produces three text columns:

  raw_text        — page text as stored in the checkpoint (minimal processing)
  speaker_text    — all natural speech from ALL speakers, boilerplate removed
  speech_text     — ONLY the Secretary of State's own spoken utterances

Output is written to a new enriched CSV; the input CSV is never modified.

Usage:
    python3 speech_extract.py [--version v44] [--mode report|extract]
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))

from hybrid_corpus_recovery import extract_secretary_only_text, normalize_whitespace
from corpus_fusion import (
    _strip_known_boilerplate,
    _strip_archive_nav_prefix,
    _find_speech_content_start,
)

csv.field_size_limit(10 ** 7)

BASE_DIR = Path(__file__).parent

DOMAIN_CHECKPOINTS: dict = {
    '1997-2001.state.gov': BASE_DIR / 'corpus_fusion_archived_crawl_1997.json',
    '2001-2009.state.gov': BASE_DIR / 'corpus_fusion_archived_crawl.json',
    '2009-2017.state.gov': BASE_DIR / 'corpus_fusion_archived_crawl_2009.json',
    '2017-2021.state.gov': BASE_DIR / 'corpus_fusion_archived_crawl_2017.json',
    '2021-2025.state.gov': BASE_DIR / 'corpus_fusion_archived_crawl_2021.json',
}

_loaded_checkpoints: dict = {}


def _get_checkpoint(domain: str) -> dict:
    if domain not in _loaded_checkpoints:
        path = DOMAIN_CHECKPOINTS.get(domain)
        if path is None or not path.exists():
            _loaded_checkpoints[domain] = {}
        else:
            print(f'[speech_extract] Loading checkpoint: {path.name}', flush=True)
            with open(path, encoding='utf-8') as f:
                _loaded_checkpoints[domain] = json.load(f)
    return _loaded_checkpoints[domain]


def _resolve_url(row: dict, audit: dict) -> str:
    row_id = row.get('row_id', '')
    return (
        row.get('url', '').strip()
        or audit.get(row_id, {}).get('new_url', '')
        or audit.get(row_id, {}).get('old_url', '')
        or ''
    )


def _get_raw_text(url: str) -> str:
    if not url:
        return ''
    hostname = urlparse(url).hostname or ''
    checkpoint = _get_checkpoint(hostname)
    entry = checkpoint.get(url)
    if entry is None:
        return ''
    if isinstance(entry, dict):
        return entry.get('text', '')
    return str(entry)


# Known footer/nav signals that should never appear at the start of speaker_text.
# These are FOOTER-specific phrases: adding archive header phrases (e.g.
# "Other State Department Archive Sites", "Skip to Main Content") would over-trigger
# recovery for pages where archive-nav stripping partially succeeds but
# the speech content was still correctly extracted by secretary_anchor mode.
_FOOTER_SIGNALS: tuple = (
    'Frequent Questions', 'Contact Us', 'External links',
    'Bureau of Public Affairs', 'Electronic Information',
    'Office of Website Management', 'FOIA', 'Privacy Policy',
    'Subject Index', 'Accessibility Statement', 'External Link Policy',
    'No Fear Act',
    # DOSFAN-era (1997-2001) footer signals
    '[End of Document]',
    "Return to the Secretary's Home Page",
    'Return to the DOSFAN',
)


# DOSFAN-era doc-ID metadata prefix: "2006/673\n[DSL/Cable]\n[audio]\n(7:35 p.m. EDT)\n"
# These are document reference IDs and media-player links, not natural speech.
_DOC_ID_PREFIX_RE = re.compile(
    r'^(?:'
    r'\d{4}/\d+\s*\n'                            # doc ID line: "2006/673"
    r'|(?:\[[\w/\xa0\s]+\]\s*\n)+'               # media bracket lines: [DSL/Cable], [audio]
    r'|\(\d{1,2}:\d{2}\s+[ap]\.m\.\s+\w+\)\s*\n' # time stamp: (7:35 p.m. EDT)
    r')+',
    re.IGNORECASE,
)

# Paragraphs that start with a QUESTION: marker in paragraph_filter mode are transcribed
# audience questions, not SoS speech. Strip them to keep speech_text SoS-only.
_QUESTION_PARA_RE = re.compile(r'(?i)^[ \t]*(?:QUESTION|Q\.|Q:)\b')

# Bare "SECRETARY:" label (without last name) used in some Powell/Albright-era transcripts.
# Pre-normalize to "SECRETARY LASTNAME:" so secretary_anchor_regex can match.
_BARE_SECRETARY_RE = re.compile(r'(?im)^\s*SECRETARY:\s')


def _normalize_secretary_label(text: str, secretary: str) -> str:
    """Convert bare 'SECRETARY:' to 'SECRETARY [LASTNAME]:' for anchor matching.

    Some transcripts use bare 'SECRETARY:' without the last name. The
    secretary_anchor_regex requires the last name; pre-normalizing here
    lets extract_secretary_only_text find the anchor and extract SoS-only speech.
    Only applied when 'SECRETARY:' exists but 'SECRETARY [LASTNAME]:' does not.
    """
    last_name = secretary.upper().split()[-1] if secretary else ''
    if not last_name:
        return text
    named_re = re.compile(rf'(?im)^\s*SECRETARY\s+{re.escape(last_name)}\b')
    if _BARE_SECRETARY_RE.search(text) and not named_re.search(text):
        return _BARE_SECRETARY_RE.sub(f'SECRETARY {last_name}: ', text)
    return text


def _strip_doc_id_prefix(text: str) -> str:
    """Strip leading DOSFAN-era doc-ID and media-metadata lines from speaker_text."""
    return _DOC_ID_PREFIX_RE.sub('', text).strip()


def _has_nav_boilerplate(text: str, limit: int = 500) -> bool:
    """Return True if text begins with known footer/nav boilerplate signals."""
    head = text[:limit]
    return any(sig in head for sig in _FOOTER_SIGNALS)


def _recover_speaker_text(raw_text: str) -> str:
    """Paragraph-level fallback: scan raw_text for speech content after nav stripping fails.

    Filters out paragraphs that are footer-flagged or nav-list blocks (many short lines).
    Returns joined clean paragraphs, or '' if none are found.
    """
    result = []
    for p in re.split(r'\n\n+', raw_text):
        p = p.strip()
        if len(p) < 80:
            continue
        if any(sig in p[:400] for sig in _FOOTER_SIGNALS):
            continue
        # Nav sidebars have many short lines (avg < 25 chars per line).
        # Speech paragraphs have long sentences.
        lines = [l for l in p.split('\n') if l.strip()]
        if len(lines) >= 5 and (sum(len(l) for l in lines) / len(lines)) < 25:
            continue
        result.append(p)
    return '\n\n'.join(result)


def make_speaker_text(raw_text: str, url: str, secretary: str) -> str:
    """Strip nav/boilerplate from raw_text to isolate natural speech from all speakers."""
    if not raw_text:
        return ''
    clean = _strip_known_boilerplate(raw_text) or ''
    if not clean:
        return ''

    domain = urlparse(url).hostname or ''

    if '2017-2021' in domain:
        # Remove A-Z country menu (~7,000 chars) that precedes speech content
        az_idx = clean.lower().find('afghanistan\nalbania\n')
        if az_idx != -1:
            az_end = clean.find('\n\n', az_idx + 100)
            if az_end != -1 and az_end < az_idx + 12000:
                clean = clean[az_end:].strip()
    elif '2009-2017' in domain or '2001-2009' in domain or '1997-2001' in domain:
        stripped = _strip_archive_nav_prefix(clean, secretary)
        if stripped and stripped != clean:
            # _strip_archive_nav_prefix already anchored at speech start.
            # Calling _find_speech_content_start on the stripped result double-anchors:
            # it misidentifies an embedded date or section header as a nav boundary.
            # Truncate any trailing footer block (footer signals at any position, plus
            # the 2001-2009 "Released on [Month]" release-date line that precedes nav).
            _best = len(stripped)
            for _sig in _FOOTER_SIGNALS:
                _p = stripped.find(_sig)
                if _p != -1 and _p < _best:
                    _best = _p
            _rel = re.search(
                r'\nReleased on (?:January|February|March|April|May|June|July|'
                r'August|September|October|November|December)',
                stripped,
            )
            if _rel and _rel.start() < _best:
                _best = _rel.start()
            if _best < len(stripped):
                # Cut directly at the footer boundary, not at the prior newline.
                # Using rfind('\n', 0, _best) clips too early when the footer
                # starts on the same line as the last speech utterance.
                stripped = stripped[:_best].strip()
            if stripped and stripped != clean:
                return re.sub(r'\n{3,}', '\n\n', stripped.strip())
        clean = stripped or clean

    anchor = _find_speech_content_start(clean, secretary)
    # Preserve paragraph breaks (\n\n) so extract_secretary_only_text can split on them.
    # Only collapse runs of 3+ blank lines to double-newlines; do not flatten to spaces.
    body = clean[anchor:].strip()
    return re.sub(r'\n{3,}', '\n\n', body)


def _process_row(row: dict, audit: dict) -> tuple:
    """Return (raw_text, speaker_text, speech_text, extraction_mode)."""
    secretary = row.get('secretary', '')
    existing_speech = row.get('speech_text', '')

    url = _resolve_url(row, audit)
    if not url:
        return '', '', existing_speech, 'no_url'

    raw_text = _get_raw_text(url)
    if not raw_text:
        return '', '', existing_speech, 'no_checkpoint'

    speaker_text = make_speaker_text(raw_text, url, secretary)
    speaker_text = _strip_doc_id_prefix(speaker_text)

    # Fix 2: if nav stripping landed in the footer, try paragraph-level recovery.
    if speaker_text and _has_nav_boilerplate(speaker_text):
        recovered = _recover_speaker_text(raw_text)
        speaker_text = recovered  # may be '' if recovery finds nothing

    text_for_extraction = speaker_text or raw_text
    # Convert bare "SECRETARY:" to "SECRETARY [LASTNAME]:" so secretary_anchor_regex
    # can match transcripts that omit the last name from the speaker label.
    text_for_extraction = _normalize_secretary_label(text_for_extraction, secretary)

    speech_text, mode = extract_secretary_only_text(text_for_extraction, secretary)

    # For paragraph_filter mode, strip any paragraphs that start with QUESTION:/Q:/Q.
    # These are transcribed audience questions, not SoS speech.
    if mode == 'paragraph_filter':
        paras = speech_text.split('\n\n')
        clean_paras = [p for p in paras if not _QUESTION_PARA_RE.match(p.strip())]
        if clean_paras:
            speech_text = '\n\n'.join(clean_paras)

    # Fix 3: for fallback_full mode, preserve paragraph structure.
    # extract_secretary_only_text's fallback_full branch collapses all whitespace via
    # normalize_whitespace; instead use speaker_text directly (already de-duped to \n\n
    # paragraph breaks by make_speaker_text / _recover_speaker_text).
    # When speaker_text is empty (recovery found nothing), text_for_extraction is raw_text
    # which still contains nav/boilerplate — use existing fused speech_text instead.
    if mode == 'fallback_full':
        if speaker_text:
            speech_text = speaker_text.strip()[:120000]
        else:
            speech_text = existing_speech
            mode = 'recovery_failed'

    # Accept short but non-trivial extractions (e.g. Powell's "No." response).
    # Only fall back for fallback/full-text modes where short results indicate
    # a failed extraction rather than a genuinely brief speech.
    min_len = 50 if 'fallback' in mode or 'recovery' in mode else 3
    if len(speech_text) < min_len:
        speech_text = existing_speech
        mode = mode + '_fallback' if mode else 'fallback'

    return raw_text, speaker_text, speech_text, mode


def _load_audit(ver: str) -> dict:
    path = BASE_DIR / f'corpus_fusion_full_audit.{ver}.csv'
    if not path.exists():
        print(f'[speech_extract] WARNING: Audit CSV not found: {path.name}', file=sys.stderr)
        return {}
    audit: dict = {}
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            audit[row['row_id']] = row
    print(f'[speech_extract] Loaded audit: {len(audit):,} rows from {path.name}')
    return audit


def _select_fused_csv(ver: str) -> Path:
    patched = BASE_DIR / f'secretaryofstate_speeches_final.fused.{ver}.patched.csv'
    plain = BASE_DIR / f'secretaryofstate_speeches_final.fused.{ver}.csv'
    if patched.exists():
        print(f'[speech_extract] Using patched CSV: {patched.name}')
        return patched
    print(f'[speech_extract] Patched CSV not found — using plain fused CSV')
    return plain


def _output_path(fused_path: Path) -> Path:
    name = fused_path.name
    if name.endswith('.csv'):
        return fused_path.parent / (name[:-4] + '.enriched.csv')
    return fused_path.parent / (name + '.enriched.csv')


def run_report(ver: str) -> None:
    """Print coverage statistics without writing any files."""
    audit = _load_audit(ver)
    fused_path = _select_fused_csv(ver)

    with open(fused_path, newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))

    total = len(rows)
    url_resolved = 0
    in_checkpoint = 0
    mode_counts: Counter = Counter()
    type_mode: dict = {}

    for i, row in enumerate(rows, 1):
        if i % 2000 == 0:
            print(f'  ... {i:,}/{total:,}', flush=True)

        url = _resolve_url(row, audit)
        if url:
            url_resolved += 1
        if url and _get_raw_text(url):
            in_checkpoint += 1

        _, _, _, mode = _process_row(row, audit)

        mode_counts[mode] += 1
        stype = (row.get('speech_type') or 'unknown').lower().strip()
        if stype not in type_mode:
            type_mode[stype] = Counter()
        type_mode[stype][mode] += 1

    print(f'\nSpeech extraction report — {ver}')
    print(f'  Total rows:              {total:,}')
    print(f'  Rows with URL resolved:  {url_resolved:,} ({url_resolved/total*100:.1f}%)')
    print(f'  Rows in checkpoint:      {in_checkpoint:,} ({in_checkpoint/total*100:.1f}%)')
    no_data = total - in_checkpoint
    print(f'  No URL / no checkpoint:  {no_data:,} ({no_data/total*100:.1f}%)  → will use existing speech_text')

    print(f'\nExtraction mode breakdown:')
    for mode, cnt in sorted(mode_counts.items(), key=lambda x: -x[1]):
        pct = cnt / total * 100
        print(f'  {mode:<34} {cnt:6,}  ({pct:.1f}%)')

    print(f'\nSpeech type coverage:')
    for stype, modes in sorted(type_mode.items(), key=lambda x: -sum(x[1].values())):
        n = sum(modes.values())
        anchor_n = modes.get('secretary_anchor', 0)
        pct = anchor_n / n * 100 if n else 0
        print(f'  {stype:<30} {n:5,} → secretary_anchor: {anchor_n:5,} ({pct:.1f}%)')


def run_extract(ver: str) -> None:
    """Write the enriched CSV."""
    audit = _load_audit(ver)
    fused_path = _select_fused_csv(ver)
    out_path = _output_path(fused_path)

    with open(fused_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    new_fields = ['raw_text', 'speaker_text', 'extraction_mode']
    out_fields = fieldnames + [c for c in new_fields if c not in fieldnames]

    total = len(rows)
    mode_counts: Counter = Counter()

    print(f'[speech_extract] Processing {total:,} rows → {out_path.name}')
    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=out_fields)
        writer.writeheader()
        for i, row in enumerate(rows, 1):
            if i % 1000 == 0:
                print(f'  ... {i:,}/{total:,}', flush=True)
            raw, speaker, speech, mode = _process_row(row, audit)
            row['raw_text'] = raw
            row['speaker_text'] = speaker
            row['speech_text'] = speech
            row['extraction_mode'] = mode
            mode_counts[mode] += 1
            writer.writerow(row)

    print(f'[speech_extract] Wrote {total:,} rows to {out_path}')
    print(f'\nMode breakdown:')
    for mode, cnt in sorted(mode_counts.items(), key=lambda x: -x[1]):
        print(f'  {mode:<34} {cnt:6,}  ({cnt/total*100:.1f}%)')


def main() -> None:
    ap = argparse.ArgumentParser(description='Enrich SoS corpus with extracted speech text.')
    ap.add_argument('--version', default='v44', help='Corpus version (default: v44)')
    ap.add_argument('--mode', choices=['report', 'extract'], default='report',
                    help='report: print coverage stats (default); extract: write enriched CSV')
    args = ap.parse_args()

    if args.mode == 'report':
        run_report(args.version)
    else:
        run_extract(args.version)


if __name__ == '__main__':
    main()
