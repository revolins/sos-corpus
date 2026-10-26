#!/usr/bin/env python3
"""rubio_reextract.py — offline re-extraction of every Rubio row from its stored Raw Text.

Plan step P1 / §9 ("Re-extract all Rubio rows offline from Raw Text and write
reextract_diff.csv"). Nothing is fetched: each row's Raw Text is run through the
current rubio_backfill.extract_row, then through the same SOS Text cleaning that
corpus_rubio_update._clean_new_row applies to new rows, and compared with the
published SOS Text.

Scoring (per row) uses rubio_turns against the label-segmented content block of the
Raw Text (footnote errata applied, as in extract_row):
  reference Rubio text   -- his labelled turns; for a label-less page (written
                            statement, video message) the whole content block
  reference foreign text -- every other labelled turn
  rubio_recall           -- share of the reference Rubio 6-gram shingles present
  foreign_leak_words     -- words of the text covered by a shingle that occurs in
                            the foreign turns and nowhere in his turns
  lost_words             -- words of the OLD text covered by no shingle of the NEW
                            text (what re-extraction would remove; review aid)
A new text is auto-accepted only if recall_new >= recall_old AND leak_new <= leak_old.
Rows already known to need a human look (audit §6) are flagged hand_review=1
whatever their scores, and are never accepted=1 (pending hand review).

Outputs (in --out-dir, which defaults to a temp directory and may never be the
repo's data/):
  reextract_diff.csv    one row per Rubio row, scores + flags
  reextract_texts.csv   Row Id, new SOS Text, new Natural Text, extraction_mode
  reextract_summary.txt aggregate before/after, regressions, hand-review list
  reextract_stderr.log  extractor log (unresolved labels, fallbacks)

Usage:
    python3 scripts/rubio_reextract.py --corpus corpus_v2026.10.19.csv \\
        [--out-dir DIR] [--rows 17415,17658]
"""
import argparse
import contextlib
import csv
import io
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

import rubio_turns
from rubio_backfill import (
    _END_MARK_RE,
    _active_speaker_decision,
    _apply_footnote_errata,
    _rubio_content_block,
    extract_row,
)
from corpus_finalize_v4_30 import (
    normalize_encoding,
    strip_boilerplate,
    strip_footer,
    strip_leading_headers,
    strip_stage_directions,
)

csv.field_size_limit(10 ** 8)

_REPO = Path(__file__).resolve().parent.parent
_SHINGLE = 6

# Rows the audit already marked for human review before any automatic acceptance
# (X-07 legacy-cleaner rows, 17095 half-text fallback, split-colon rows).
HAND_REVIEW = frozenset({
    '17061', '16951', '17237', '17095', '16996', '16997', '17347', '17512',
})

_DIFF_COLUMNS = [
    'Row Id', 'URL', 'Speech Type', 'extraction_mode', 'n_rubio_turns',
    'old_words', 'new_words',
    'old_rubio_recall', 'new_rubio_recall',
    'old_foreign_leak_words', 'new_foreign_leak_words',
    'lost_words', 'gained_words', 'untranscribed_rubio_segments',
    'changed', 'accepted', 'hand_review',
]


# Stored Raw Text is get_text('\n'), so inline elements land on lines of their own:
# a <sup> ordinal ("62\nnd\nanniversary") and a bold lead-in before its colon
# ("Colombia\n: With this action"). The weekly path reads the live HTML and never
# sees these; re-extraction from Raw Text has to rejoin them.
_ORDINAL_PUNCT_RE = re.compile(r'(\d)\n(st|nd|rd|th)\n(?=[.,;:!?)\]])')
_ORDINAL_SEAM_RE = re.compile(r'(\d)\n(st|nd|rd|th)\b[ \t]*\n?')
_COLON_SEAM_RE = re.compile(r'(\S)\n:(?=\s)')


def rejoin_inline_seams(text: str) -> str:
    t = _ORDINAL_PUNCT_RE.sub(r'\1\2', text or '')
    t = _ORDINAL_SEAM_RE.sub(lambda m: m.group(1) + m.group(2) + ' ', t)
    return _COLON_SEAM_RE.sub(r'\1:', t)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _tokens(text: str) -> list[str]:
    t = (text or '').translate(str.maketrans({'’': "'", '‘': "'"})).lower()
    return re.findall(r"[^\W_]+(?:'[^\W_]+)?", t)


def _shingles(toks: list[str]) -> set[tuple]:
    if len(toks) < _SHINGLE:
        return {tuple(toks)} if toks else set()
    return {tuple(toks[i:i + _SHINGLE]) for i in range(len(toks) - _SHINGLE + 1)}


def _covered(toks: list[str], sh: set[tuple]) -> list[bool]:
    """Per-token flag: is this token inside some shingle that belongs to `sh`."""
    cov = [False] * len(toks)
    if len(toks) < _SHINGLE:
        if toks and tuple(toks) in sh:
            cov = [True] * len(toks)
        return cov
    for i in range(len(toks) - _SHINGLE + 1):
        if tuple(toks[i:i + _SHINGLE]) in sh:
            for j in range(i, i + _SHINGLE):
                cov[j] = True
    return cov


def _recall(ref_sh: set[tuple], toks: list[str]) -> float:
    if not ref_sh:
        return 1.0
    return len(ref_sh & _shingles(toks)) / len(ref_sh)


def _leak(toks: list[str], foreign_only: set[tuple], rubio_sh: set[tuple]) -> int:
    if not foreign_only:
        return 0
    f = _covered(toks, foreign_only)
    r = _covered(toks, rubio_sh)
    return sum(1 for a, b in zip(f, r) if a and not b)


def reference(raw: str) -> tuple[str, str, int]:
    """(rubio_ref, foreign_ref, n_rubio_turns) from the label-segmented content block."""
    block, _ = _rubio_content_block({'raw_text': raw})
    if not block.strip():
        return '', '', 0
    block, _ = _apply_footnote_errata(block)
    block = rejoin_inline_seams(_END_MARK_RE.sub('', block).rstrip())
    turns = rubio_turns.segment(block)
    mine = [t.text for t in turns if t.label is not None and t.is_rubio]
    other = [t.text for t in turns if t.label is not None and not t.is_rubio]
    if not mine:
        return block, '\n'.join(other), 0
    return '\n'.join(mine), '\n'.join(other), len(mine)


def clean_sos_text(text: str, title: str) -> str:
    """The SOS Text half of corpus_rubio_update._clean_new_row (kept in step by hand:
    that module is not importable without the NER stack)."""
    t = normalize_encoding(text or '')
    t = strip_boilerplate(strip_footer(strip_leading_headers(t)), title)
    return strip_stage_directions(t, rubio_path=True)


def score(text: str, ref_sh: set, foreign_only: set) -> tuple[int, float, int]:
    toks = _tokens(text)
    return len(text.split()), _recall(ref_sh, toks), _leak(toks, foreign_only, ref_sh)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _check_out_dir(out: Path) -> None:
    data = (_REPO / 'data').resolve()
    o = out.resolve()
    if o == data or data in o.parents:
        sys.exit(f'ERROR: --out-dir {out} is inside the repo data/ directory; '
                 f're-extraction output is a review artefact, not corpus data.')


def run(args: argparse.Namespace) -> int:
    out = Path(args.out_dir)
    _check_out_dir(out)
    out.mkdir(parents=True, exist_ok=True)

    cols = ['Row Id', 'Secretary', 'Title', 'Speech Type', 'URL', 'SOS Text', 'Raw Text']
    df = pd.read_csv(args.corpus, usecols=cols, dtype=str, keep_default_na=False)
    df = df[df['Secretary'].str.contains('Rubio')]
    if args.rows:
        want = {r.strip() for r in args.rows.split(',') if r.strip()}
        df = df[df['Row Id'].isin(want)]
    print(f'[rubio_reextract] {len(df):,} Rubio rows from {Path(args.corpus).name}',
          file=sys.stderr)

    diff_rows, text_rows = [], []
    log = io.StringIO()
    for rec in df.to_dict('records'):
        rid, url, raw = rec['Row Id'], rec['URL'], rec['Raw Text']
        with contextlib.redirect_stderr(log):
            ex = extract_row(url, {'raw_text': raw})
        if ex is None:
            print(f'{rid}: extract_row returned None', file=log)
            ex = {'speech_text': '', 'speaker_text': '', 'extraction_mode': 'skipped',
                  'untranscribed_rubio_segments': 0}
        # The weekly path's third-person gate: under a third-person intro with a
        # transcript marker (the Cuba video message), his text is what follows it.
        _, _, after = _active_speaker_decision(
            url, ex['speech_text'], ex.get('speaker_text') or ex['speech_text'],
            int('rubio_turns' in ex['extraction_mode']))
        if after:
            ex['speech_text'] = after
            ex['extraction_mode'] += '+after_transcript_marker'
        new_text = rejoin_inline_seams(clean_sos_text(ex['speech_text'], rec['Title']))
        old_text = rec['SOS Text']

        rubio_ref, foreign_ref, n_rt = reference(raw)
        if not rubio_ref:
            rubio_ref = new_text          # no content block (17095): self-reference
        ref_sh = _shingles(_tokens(rubio_ref))
        foreign_only = _shingles(_tokens(foreign_ref)) - ref_sh

        ow, orc, olk = score(old_text, ref_sh, foreign_only)
        nw, nrc, nlk = score(new_text, ref_sh, foreign_only)
        otoks, ntoks = _tokens(old_text), _tokens(new_text)
        lost = sum(1 for c in _covered(otoks, _shingles(ntoks)) if not c)
        gained = sum(1 for c in _covered(ntoks, _shingles(otoks)) if not c)
        changed = ' '.join(old_text.split()) != ' '.join(new_text.split())
        # A hand-review row is never auto-accepted, whatever its scores: a patch
        # step that filters on accepted == 1 must not apply it unreviewed.
        accepted = (nrc >= orc - 1e-9) and (nlk <= olk) and rid not in HAND_REVIEW
        diff_rows.append({
            'Row Id': rid, 'URL': url, 'Speech Type': rec['Speech Type'],
            'extraction_mode': ex['extraction_mode'], 'n_rubio_turns': n_rt,
            'old_words': ow, 'new_words': nw,
            'old_rubio_recall': round(orc, 4), 'new_rubio_recall': round(nrc, 4),
            'old_foreign_leak_words': olk, 'new_foreign_leak_words': nlk,
            'lost_words': lost, 'gained_words': gained,
            'untranscribed_rubio_segments': ex['untranscribed_rubio_segments'],
            'changed': int(changed), 'accepted': int(accepted),
            'hand_review': int(rid in HAND_REVIEW),
        })
        text_rows.append({
            'Row Id': rid, 'SOS Text': new_text, 'Natural Text': rejoin_inline_seams(ex['speaker_text']),
            'extraction_mode': ex['extraction_mode'],
            'Untranscribed Rubio Segments': ex['untranscribed_rubio_segments'],
        })

    d = pd.DataFrame(diff_rows, columns=_DIFF_COLUMNS)
    d.to_csv(out / 'reextract_diff.csv', index=False)
    pd.DataFrame(text_rows).to_csv(out / 'reextract_texts.csv', index=False)
    (out / 'reextract_stderr.log').write_text(log.getvalue(), encoding='utf-8')
    summary = summarize(d)
    (out / 'reextract_summary.txt').write_text(summary, encoding='utf-8')
    print(summary)
    print(f'[rubio_reextract] wrote {out}/reextract_diff.csv, reextract_texts.csv, '
          f'reextract_summary.txt, reextract_stderr.log', file=sys.stderr)
    return 0


def summarize(d: pd.DataFrame) -> str:
    ch = d[d['changed'] == 1]
    rec_up = d[d['new_rubio_recall'] > d['old_rubio_recall'] + 1e-9]
    rec_dn = d[d['new_rubio_recall'] < d['old_rubio_recall'] - 1e-9]
    leak_dn = d[d['new_foreign_leak_words'] < d['old_foreign_leak_words']]
    leak_up = d[d['new_foreign_leak_words'] > d['old_foreign_leak_words']]
    pending = d[(d['accepted'] == 0) & (d['hand_review'] == 1)]
    rejected = d[(d['accepted'] == 0) & (d['hand_review'] == 0)]
    gained = (rec_up['new_words'] - rec_up['old_words']).clip(lower=0).sum()
    big_loss = d[(d['lost_words'] >= 20) & (d['changed'] == 1)]

    def ids(x: pd.DataFrame) -> str:
        return ', '.join(x['Row Id'].tolist()) or '-'

    lines = [
        f'Rows: {len(d)}   changed: {len(ch)}   unchanged: {len(d) - len(ch)}',
        f'Words: old {d["old_words"].sum():,} -> new {d["new_words"].sum():,} '
        f'(net {d["new_words"].sum() - d["old_words"].sum():+,})',
        f'Mean rubio_recall: old {d["old_rubio_recall"].mean():.4f} -> '
        f'new {d["new_rubio_recall"].mean():.4f}',
        f'Rows with recall < 0.98: old {(d["old_rubio_recall"] < 0.98).sum()} -> '
        f'new {(d["new_rubio_recall"] < 0.98).sum()}',
        f'Recall improved: {len(rec_up)} rows, +{gained:,} words (net word gain on those rows)',
        f'Foreign leak words: old {d["old_foreign_leak_words"].sum():,} in '
        f'{(d["old_foreign_leak_words"] > 0).sum()} rows -> new '
        f'{d["new_foreign_leak_words"].sum():,} in {(d["new_foreign_leak_words"] > 0).sum()} rows',
        f'Leak reduced: {len(leak_dn)} rows',
        f'Untranscribed Rubio segments: {d["untranscribed_rubio_segments"].sum()} in '
        f'{(d["untranscribed_rubio_segments"] > 0).sum()} rows',
        f'Accepted automatically: {int(d["accepted"].sum())}   rejected: {len(rejected)}   '
        f'pending hand review (never auto-accepted): {len(pending)}',
        '',
        f'REGRESSIONS recall down ({len(rec_dn)}): {ids(rec_dn)}',
        f'REGRESSIONS leak up ({len(leak_up)}): {ids(leak_up)}',
        f'Changed rows losing >= 20 old words ({len(big_loss)}): {ids(big_loss)}',
        f'Hand review (audit list): {ids(d[d["hand_review"] == 1])}',
        f'Rows with recall < 0.98 after: {ids(d[d["new_rubio_recall"] < 0.98])}',
        f'Rows with leak > 0 after: {ids(d[d["new_foreign_leak_words"] > 0])}',
        '',
        'Modes: ' + ', '.join(f'{k}={v}' for k, v in d['extraction_mode'].value_counts().items()),
    ]
    return '\n'.join(lines) + '\n'


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--corpus', required=True, help='Released corpus CSV (with Raw Text)')
    ap.add_argument('--out-dir', default=str(Path(tempfile.gettempdir()) / 'rubio_reextract'),
                    help='Output directory (default: %(default)s; never the repo data/)')
    ap.add_argument('--rows', default='', help='Comma-separated Row Ids to restrict to')
    sys.exit(run(ap.parse_args()))


if __name__ == '__main__':
    main()
