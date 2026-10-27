# Whole-corpus column audit (2026-10-27)

After the Title repair (`docs/title_repair.md`) and the Rubio re-extraction, every column of the catch-up release candidate was checked against the README's column definitions. Repairs go into the same catch-up release (`scripts/corpus_patch_catchup.py`). All numbers are on that candidate: v2026.10.26 plus the patch, 17,721 rows.

## Mechanical rules: all pass

Every row was checked against these rules:
- Row Hash and both word counts recompute exactly.
- Location has exactly one comma.
- The five list columns use `"; "`, with no case-insensitive duplicates.
- Each country name has one ISO code, and the codes are position-matched.
- People Mentioned (SOS Text) is a subset of People Mentioned (Natural Text).
- Speakers Present includes the row's own Secretary.
- Canonical URL is either the URL or its renamed path.

## Defects found and repaired (first pass, sample-reviewed)

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

## Distribution audit (second pass)

The first pass reviewed samples of each repair. The second pass checks every row and every column. `scripts/corpus_distribution_audit.py` runs on each release candidate and computes:

- **For each text column, per row:**
  - distinct words against the corpus-wide length curve (Heaps' law z-score), flagging repetition and word lists;
  - words no other row uses (garbled or merged words);
  - the share of upper-case tokens (leftover labels);
  - SOS words absent from Natural Text, and words absent from the page.
- **Per-turn coverage:** whether each SOS paragraph occurs on the row's own page and in its own Natural Text.
- **Across rows:**
  - 8-word phrases recurring in 25+ rows (boilerplate);
  - row pairs sharing most of their rare phrases (copied text).
- **For categorical and list columns:** value frequencies, with every rare or odd-shaped value reviewed.

Defects it found, now repaired:

| Defect | Rows | Repair |
|---|---:|---|
| An interviewer's question inside SOS Text: "QUESTION: Secretary Kerry, is there any news on Syria?" had been read as Kerry's own label | about 760 rows, ~100k words | `remove_foreign_paragraphs`, `drop_vocative_remainders`: every SOS paragraph is placed in Natural Text (else Raw Text); the words the page gives another speaker are removed, and the Secretary's merged answer stays |
| A statement whose opening sentences were lost (the old half-text fallback) | 41 | `restore_statement_start`: from the page's release line, Share widget or dateline to the cut |
| The Secretary's first word lost after their own label ("SECRETARY POMPEO: Good" + "morning") | 10 | `restore_turn_head` |
| SOS Text that is another item's speech (7467 "U.S.-Spain Council" carried the Rio+20 speech; 16325 carried the Summit for Democracy speech) | 2 | `sos_from_natural` |
| Natural Text that does not contain its own SOS Text | 34 | `extend_natural`, now also triggered by coverage |
| A stray ":" or ")" left by a split label or a broken "(As Delivered" note | 156 | `strip_stray_punctuation` |
| People Mentioned values that are not people: places ("Sierra Leone", "Goma"), groups ("Boko Haram", "Houthis", "Kosovars"), agreements and concepts ("Quartet", "Roadmap", "Brexit", "Abraham Accords"), greetings and bare titles | 3,084 cells in 1,702 rows | `corpus_enrich_v1.is_not_a_person`: a reviewed list from the value distribution, plus shape rules (opens with a lower-case non-particle word, ends in "of"); real names such as Lloyd Austin or al-Sisi are kept |
| Rows with none of the Secretary's own words, or out of scope | 9 | Removed (below) |

Bugs in the repairs themselves were found by re-running the audit on its own output and are fixed, with tests:
- Page label typos ("SECRETARY ABRIGHT", "SECRETARY RUBI") and a bare "SECRETARY :" had made the Secretary's own turns look foreign. Labels now tolerate small misspellings.
- A guard skips any removal that would strip more than 80% of a row's SOS Text.
- A comma-led paragraph is removed only where the page puts it inside another speaker's turn. "Secretary Albright, Estonian President Meri, …" opens a genuine joint statement; its subject is restored instead.

**Before → after** (v2026.10.26 → candidate):

| Measure | Before | After |
|---|---:|---:|
| SOS Text starting with punctuation | 209 | 11 |
| Comma-led SOS paragraphs | 788 | 13 |
| SOS Text starting lower-case | 181 | 53 |
| Missed stage directions | 93 | 3 |
| Other speakers' labels in SOS Text | 30 | 0 |
| Natural Text badly shorter than SOS Text | 44 | 0 |
| SOS Text mostly not on its own page | 4 | 0 |

The 53 lower-case starts and the 11 punctuation starts are the page's own text. They are transcripts that begin "(In progress)" or "(Inaudible)", or paragraphs whose preceding hyperlinked word was lost at the source. Recurring phrases are diplomatic formulae; none is site chrome.

## The audit as verifier checks (AK-AM)

`corpus_verify_research_grade.py --audit-checks` (`--strict-audit-checks` for every row) turns the audit into checks:

| Check | Rule | Severity |
|---|---|---|
| **AK** | A repair in `corpus_audit_repairs` would still change the row: another speaker's turn or a vocative question in SOS Text, a lost opening or first word, a missed stage direction, page chrome, lost characters, Natural Text not covering SOS Text. Also fails SOS Text that is mostly not on its own page. | FAIL |
| **AL** | A People Mentioned value that is not a person (`corpus_enrich_v1.is_not_a_person`) | FAIL |
| **AM** | SOS Text vocabulary is an outlier for its length (Heaps' law, \|z\| >= 4) | Warning only: an outlier is a lead, not an error |

The repairs serve as the detectors, so a check and its fix cannot drift apart. Running AK strictly over the repaired corpus also tests that the repairs are idempotent.

**Where they run:**
- The weekly job (`corpus_rubio_update.py`) passes `--audit-checks` to both the per-row gate and the full verifier. A new row that fails AK or AL is held, not published.
- Without `--strict-audit-checks`, rows unchanged since `--baseline` are skipped. AK costs about 0.07 s a row.

**Checked before wiring it in:** all 24 rows the weekly job added in v2026.10.26 pass AK and AL.

**What its first strict run found.** It caught 65 candidate rows that the repairs would still change, now fixed:
- A garble fix that was not anchored had doubled "Secretary Albright" in 684.
- A stray "." joined into "..".
- A reporter's question under a label split over lines with a note ("QUESTION\n(unofficial translation from Czech)") in 975 and 3077.
- A paragraph tidy (`tidy_paragraph_starts`) that had been added after that run began.

## Rows removed

All renumber the rows after them (Check Q accepts the audited removals).

| v2026.10.26 Row Id | Why |
|---|---|
| 17461, 17463 | Spokesperson media notes about Rubio (out of scope) |
| 1732 | The page has only a reporter's question |
| 7477 | A Public Schedule page (site navigation) |
| 12442 | A third-person travel announcement |
| 6035 | An official's briefing; its SOS Text was a reporter's question |
| 35, 73 | Nick Burns's press briefings as spokesman |
| 6838 | A background briefing by senior administration officials |
| 10170, 10171 | Spanish and French translations of row 10172 (translations of English items are out of scope) |

## Decisions (user, 2026-10-27)

- **Row 0** (Warren Christopher's farewell testimony) stays.
- **Pre-tenure confirmation statements** stay.
- **Location** is repaired from page evidence only.
- **Rows with none of the Secretary's own words** are removed.
- **Audit method:** exhaustive distributions over every row and column, not spot checks, and every known issue is addressed.

## Not changed, by design

- **Transcripts that start "(In progress)" or "(Inaudible)"** are kept: this is the page's own text.
- **Words lost at the source** (a hyperlinked word before a comma) cannot be recovered.
- **"Foreign Minister Sid'ahmed"** (859): this name keeps its internal capital, which the apostrophe rule cannot tell apart.
- **The long tail of People Mentioned** (about 14,000 values used by one row each) remains automatic extraction, as the README states. The frequent errors are removed.

## Verification

Strict verifier on the candidate (`--strict-titles --rubio-coverage --strict-rubio-coverage --allow-changed-rows`):
- PASSED, with 17,721 rows and 8,457 audited changes.
- Compared with v2026.10.26, it adds no warnings. Check R falls from 131 to 85, and short People Mentioned tokens from 866 to 833.
- All 239 tests pass.
