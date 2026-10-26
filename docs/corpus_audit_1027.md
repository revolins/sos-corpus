# Whole-corpus column audit (2026-10-27)

After the Title repair (`docs/title_repair.md`) and the Rubio re-extraction, every column of the catch-up release candidate was checked against the README's column definitions. Repairs go into the same catch-up release (`scripts/corpus_patch_catchup.py`). All numbers below are on that candidate: v2026.10.26 plus the patch, 17,730 rows.

## Mechanical rules: all pass

Every row was checked against these rules:
- Row Hash and both word counts recompute exactly.
- Location has exactly one comma.
- The five list columns use `"; "`, with no case-insensitive duplicates.
- Each country name has one ISO code, and the codes are position-matched.
- People Mentioned (SOS Text) is a subset of People Mentioned (Natural Text).
- Speakers Present includes the row's own Secretary.
- Canonical URL is either the URL or its renamed path.

## Defects found and repaired

| Defect | Before | After | Repair |
|---|---:|---:|---|
| Another speaker's labelled turn inside SOS Text (a foreign leader's speech, reporters' questions, an audience's replies) | 30 rows detected by label shape; 130 rows repaired in total | 2 | `corpus_audit_repairs.remove_other_turns` / `remove_inline_other_turns`. Each non-Secretary turn of Natural Text is removed from SOS Text, label included. |
| Stage directions the strip misses ("(Applause and cheers.)", "(Laughter, applause.)", "(crosstalk)") | 93 | 3 | `strip_combined_stage` |
| SOS / Natural Text starting with a cut word ("d morning, everyone", "ank you") | 76 lower-case starts | 37 | `restore_cut_start` completes the cut word from Raw Text (92 fields). The 37 left start at a word boundary; what precedes them may be another speaker's, so they are not touched. |
| Natural Text shorter than its own SOS Text (Natural is the full transcript) | 44 badly short | 1 | `extend_natural` covers the Raw Text span of both texts (39 rows). |
| Page chrome left in the text ("… Share this page on:", the site footer) | 7 | 0 | `strip_chrome` |
| U+FFFD characters introduced after scraping | 12 fields | 0 | `fix_replacement_chars` takes the characters from Raw Text where Raw Text has them. Raw Text has also lost them in "Petőfi" and "Hau'ofa", which are set by hand. |
| Garbled text ("theQUESTION: uartet", "alQUESTION: aeda") | 1 row | 0 | Known-garble map. |
| Doubled paragraphs | 2 rows | 0 | `dedupe_paragraphs`, only where the page has the paragraph once. |
| Two rows carrying another row's text | 4929 (Nepali New Year had the Lao New Year statement), 14615 ("Spirit of Partnership" had the Jamaica remarks) | 0 | Rebuilt from each page, via `data/catchup_fixes_2026_10.csv`. |
| 1739: SOS Text was the page header plus a reporter's question | 1 | 0 | Powell's reply only (fixes file). |
| Speakers Present entries that are sentence fragments ("Secretary Rumsfeld Would Tell You. Mr. Donaldson"), section headings ("I. Introduction"), "Sa'Ar" / "Abc'S" casing | 26 rows | 0 | `corpus_enrich_v1`: the label is cut back to its last sentence, section headings are not labels, and only the letter after an apostrophe that follows 2+ letters is lower-cased (D'Alema and O'Brien keep their capitals). 33 rows recomputed. |
| Speech Type contradicting the page's own type | 17 | 0 | 10 pages typed Op-Ed by state.gov had been typed Press Statement; 7 Press Statements had been typed Testimony, Op-Ed or Statement. |
| Date | 1 | 0 | 11372: page header April 9, 2017; the transcript dateline repeats a 2018 typo. |
| Location | 147 | 0 | Page evidence only. The page's dateline names a place recorded as Washington (82 rows: New York, Jerusalem, Ramallah, Geneva …). A written statement datelined Washington, DC was recorded at its subject (23). A modern written statement with no dateline carried a place guessed from its subject, such as "Idlib, Syria" or "Bogotá" for Kyrgyz Independence Day (42), and now has the README's Washington, D.C. Spoken items datelined Washington are left alone (the dateline may be the press office). City-vs-city differences are naming (Belfast / Northern Ireland) and are left alone. |

A repair is accepted only where it removes text proven to belong to another speaker, or adds text proven to sit next to the existing text on the page. Every repair type was reviewed on samples. Three false positives found in review are now excluded: "the U.S.:", "the OSCE:" and "BEGIN TEXT:" are not speaker labels.

## Decisions (user, 2026-10-27)

- **Row 0** (Warren Christopher's farewell testimony, 1997-01-08) stays. Removing it would shift every Row Id.
- **Confirmation-hearing statements** given just before taking office stay. There is one per Secretary.
- **Location** is repaired from page evidence only, with the README newsroom rule for written statements.

## Left for a decision or not repairable

- **Rows with none of the Secretary's own words:**
  - 1732: the page has only a reporter's question.
  - 7477: Clinton's Public Schedule, all navigation.
  - 12442: a third-person travel announcement.
  - 6035: an official's briefing; its SOS Text is a reporter's question.

  Removing them renumbers the later rows.
- **5187 and 5957** (Clinton special briefings): labels truncated at the source to "ASSISTANT" leave other officials' words in SOS Text.
- **37 texts start at a word boundary** in mid-sentence, e.g. 47 "act of this magnitude …". The missing words may be another speaker's, so they are not restored automatically.
- **"Foreign Minister Sid'ahmed"** (859): this name keeps its internal capital, which the apostrophe rule cannot tell apart.

## Verification

- The strict verifier passes on the candidate: `--strict-titles --rubio-coverage --strict-rubio-coverage --allow-changed-rows <audit>`. Check Q accepts 7,561 audited changes.
- Compared with v2026.10.26, it adds no warnings.
- Check R (Natural shorter than SOS) falls from 131 to 87 rows; the rest differ by a word or two of tokenisation.
- All 231 tests pass.
