# Rubio coverage audit (2026-10-21)

A read-only audit of how genuine Marco Rubio items fail to reach the SOS corpus, or reach it mangled. Baseline: release `v2026.10.19`.
The audit ran 138 agents: stage tracers, two adversarial refuters per finding, and a completeness critic. 60 failure modes survived; 5 were refuted.

## Decided scope (supersedes the "recommended defaults" in §1 below)

| Decision | Outcome |
|---|---|
| Unit of coverage | Both: item recall = 1.0 against ground truth, plus per-row Rubio-turn recall ≥ 0.98 with zero words from other speakers. |
| Modality | Every item in which Rubio speaks or writes **in his own voice as Secretary of State**, across all Speech Types: spoken transcripts, Press Statements / Statements / Op-Eds bylined to him, video messages he delivers, and joint statements he co-signs. Spokesperson-voice items, readouts and briefings by others are excluded. |
| Host | **state.gov only.** whitehouse.gov, NSA, congress.gov and GovInfo are out of scope. |
| Hearings | The opening statement as posted on state.gov is the complete Testimony item. Hearing Q&A is out of scope (D5/S-01/P6 below are dropped). |
| Roles | Only items published on state.gov in his capacity as Secretary. |
| Schema | **No new published columns.** Recommendations below that add columns (Modality, Event Date, Source Host, Role, Location Method/Provisional, Untranscribed Rubio Segments, pre_tenure) are kept only in the non-published `rubio_merge_provenance.csv` sidecar or dropped. `corpus_enrich_v1.OUTPUT_COLUMNS` is unchanged. |

---

**Baseline:** `corpus_v2026.10.19.csv`, 814 Rubio rows, all on `www.state.gov`, dated 2025-01-15 to 2026-10-17. Repo: `sos-corpus-clean`. All line numbers below refer to `scripts/` unless another path is given.

## Bottom line

**What is lost today.** Almost everything that state.gov lists in the `/remarks-secretary-rubio/` collection reaches the corpus:
- The 2026-08-31 full walk found 733 candidates, wrote 729 rows and skipped 4.
- The May-2026 listing snapshot matched the corpus 582 of 582.
- No Rubio row has disappeared in any of 12 release-to-release transitions.

The proven losses are:
- **3 whole spoken items:**
  - 2025-12-04, Institute of Peace remarks to press.
  - 2026-05-21, Miami/Homestead remarks to press.
  - 2026-05-20, Cuban Independence Day video message.
- **2 joint written statements**, whose inclusion depends on scope:
  - Rubio and RFK Jr., 2025/07.
  - WHO withdrawal, 2026/01.
- **About 14.6k Rubio words missing inside about 72 rows that are in the corpus.** Most of this (about 68 rows, about 13.1k words) is the final-turn footer bug.
- **About 3.7k words of other people's speech labelled as Rubio, in about 31 rows.**
- **All congressional hearing Q&A**, which is a scope question.

**The structural risks behind this:**
- The remarks collection is the only working discovery source.
- Nothing reconciles the corpus against a ground truth.
- Every rejection and loss is silent.

---

## 1. Scope decisions to make first

You cannot compute "100% coverage" until these are settled. Each one changes the denominator. A recommended default is given for each.

| # | Decision | Options | Evidence | Recommended default |
|---|---|---|---|---|
| D1 | **Unit of coverage** | (a) pages/items; (b) Rubio turns and words inside pages; (c) both | Item coverage is near-complete. Turn and word coverage within transcripts is about 97%, with about 14.6k words lost. | **Both.** Report item recall and turn-level recall/precision separately. |
| D2 | **Modality** | spoken transcript / written statement in his name / video or recorded message / co-signed joint statement / Spokesperson release quoting him / X posts | 566 Press Statements + 2 Statements = 69.8% of Rubio rows. Only about 246 rows are transcripts. The schema has no modality column. | Include spoken transcripts, written statements bylined to him, video messages with a transcript, and joint statements he co-signs. Add a `Modality` column. Exclude third-person Spokesperson releases. |
| D3 | **Tenure window and date semantics** | Floor at 2025-01-15 (current code, `rubio_backfill.py:1176-1180`) or 2025-01-21 (`RUBIO_START`, `:85`; also `filter_certain_secstate_speakers.py:108`). `Date` is the publication date (`_parse_date` `:795-815`), not the event date. | Row 17464 (2025-01-15 confirmation opening) is the only pre-tenure row. Rows 16895-16897 are designate interviews given on tenure day (keep them). Rows 16926-16928 are dated one day after the event. | Use `Event Date` ≥ 2025-01-21 for scope. Keep `Date` as the publication date. Flag 17464 as `pre_tenure=1` or drop it. |
| D4 | **Host / venue** | state.gov only / any official U.S. government transcript / anything | All 814 rows are on www.state.gov. A verifier who could reach whitehouse.gov found that it publishes about 0 additional Rubio text transcripts (Cabinet meetings and Oval sprays are video-only). Everything that names Rubio there is already mirrored in the corpus except the 2025-06-12 Iran statement, which is row 17095. | state.gov plus congressional transcripts. Record whitehouse.gov video-only, audio/video-only, and outlet-only interviews as "documented out of scope". |
| D5 | **Congressional hearing Q&A** | include via GovInfo CHRG / committee captions, or exclude | The 10 Testimony rows (17059, 17061-17063, 17379, 17464, 17491-17494) hold opening statements only. The Q&A is roughly 25-35 hours. | Include as Tier B, with a `Source Host=govinfo.gov` column and a pending queue (GPO publishes months later). |
| D6 | **Roles** (National Security Advisor, acting USAID Administrator, acting Archivist) | in scope or not | No reachable text source outside state.gov has been found. | Include if published on an in-scope host. Add a `Role` column. |
| D7 | **Untranscribed Spanish** (`(In Spanish.)`) | count as missing, or as "untranscribed at source" | About 11-16 Rubio segments in about 8-10 rows. The words do not exist in any text source; `/translations/` pages are translations of the English originals. | Mark as "untranscribed at source". Keep a per-row count column instead of silently stripping the marker. |
| D8 | **Precision as part of "coverage"** | Non-Rubio words in SOS Text | About 3.7k words in about 31 rows | Treat this as a hard requirement: 0 non-Rubio label text in SOS Text. |
| D9 | **Archival clips of Rubio** (`SENATOR RUBIO:` 2022 clip in row 17194; `MRS RUBIO` in 17610) | | Both are correctly excluded today. | Exclude, and state the exclusion explicitly so the fuzzy resolver does not pull them back in. |

---

## 2. Failure taxonomy (deduplicated, with verifier corrections applied)

**Status key:** P = proven loss or error in the published corpus. L = latent mechanism, proven in code, no loss observed yet. H = hypothesis that needs the CI crawl. S = scope decision. R = refuted (listed at the end).

### 2a. Discovery

| ID | Mechanism | Evidence (file:line, counts, examples) | Impact | Status | Fix |
|---|---|---|---|---|---|
| **D-01 Single effective source** | `collect_candidate_urls` (`rubio_backfill.py:628-674`) combines three sources: page-sitemaps, the `/remarks-secretary-rubio/` walk (`discover_remarks_urls :478-625`, `.collection-result` only, `:528`), and an optional supplemental file. CI never passes `--urls` (`rubio-update.yml:82-85`). Corpus coverage is therefore capped at whatever editors tag into a curated collection. | The 09-28 full walk (run 36422566949) is a superset of every Rubio row dated ≤ 09-27, with 0 exceptions. 814 corpus rows against "813 items" (Wayback, 10-16). **Missing, found off-collection:** `…/office-of-the-spokesperson/2025/12/secretary-of-state-marco-rubio-remarks-to-press-at-donald-j-trump-institute-of-peace/` (0 corpus hits for "security cooperation, we want to"); `…/office-of-the-spokesperson/secretary-of-state-marco-rubio-remarks-to-press-at-the-miami-homestead-airport` (no `/YYYY/MM/`; the 2 corpus rows mentioning "Homestead" are other events); the joint statements with RFK Jr. (2025/07, IHR) and on WHO termination (2026/01). Neither of these 4 URLs appears in any of 15 CI logs. | **P:** 2 spoken + 2 joint. Upper bound unknown. The May-2026 press-release sitemap has 1,178 URLs not in the corpus: 308 with 'rubio' in the slug, mostly readouts, and 689 non-readout candidates without it. **Unverified:** these need a byline check. | P (floor) + H (size) | P4a, P4d, plus the ground-truth (GT) crawl |
| **D-02 Sitemap path is dead for current-era content** | `discover_sitemap_urls` keeps only `'page-sitemap' in loc` (`:432-437`). It never reads `state_press_release-sitemap*.xml`, `state_briefing-sitemap.xml` or `state_ext_content-sitemap.xml`. It then requires 'rubio' in the URL (`_is_rubio_speech_url :275-289`, `:282`). Incremental runs get `attempts=1` and a 5-minute budget (`:108`, `:645-648`). **Correction:** the 4 items in D-01 have 'rubio' in their slugs and match no `_NON_SPEAKER_URL_PATS`. They are missed because of the page-sitemap restriction, not the 'rubio' filter. | Every incremental log shows "0 from sitemaps" (10-12, 10-14, 10-19). The full walk got 64 hits, all legacy flat slugs (page-sitemap 6/7/8/9/13). The Wayback press-release sitemaps (1,692 locs) contain 413/413 corpus spokes rows for 2025-04 to 2026-04. 471 of 657 dated corpus URLs have no 'rubio'. Run with the current filter, 121/414 'rubio' locs pass and 15 are missing from the corpus; most of those are readouts that slip past the slug blacklist. | Removes all redundancy: any archive miss becomes permanent. The 'rubio' filter alone would also drop about 70% of real releases. | P (mechanism), H (count) | P4a |
| **D-03 Archive pagination drift** | Pages are fetched one at a time (`:565-583`). There is no consistent snapshot and no check against the "N items" header or `input[name=total_pages]`. Duplicate links are silently collapsed at `:665-672` (`canonical_url_key`, `corpus_keys.py:57-66`). A duplicated slot means a missed slot. | **21 transient misses** over 8 full-walk transitions (corrected from 20): 08-17 −9, 08-24 −10, 08-31 −1, 09-21 −1. Examples: Raddatz ABC, Brennan CBS, Kuala Lumpur, peru-national-day. Runs 08-17 and 08-24 show about 10 extra duplicate slots, matching the drops. **One incremental miss:** dispatch 36457033920 (09-28) missed `…/2026/09/mexico-national-day-2/`, recovered 10-05 as row 17687. The 10-19 run logged 86 archive links but 81 unique (5 duplicate slots; possible misses not logged). The drops are scattered, so per-page CDN cache age is the likely cause. This is **inferred**; no cache headers were logged. | About 0.35% of slots per walk. 0 permanent losses proven. | L/P (1 incr. miss, recovered) | P4b |
| **D-04 Incremental floor + early stop + no scheduled rescan** | `resolve_since` (`:1032-1061`) = max corpus Rubio Date − 14 days. The month-floor is applied per link (`_extract_page_links :522, :542`), even on page 1. Early stop after two stale pages (`:590-596`). `--full-rescan` only runs on manual dispatch (`rubio-update.yml:8, :85`). The floor follows max Date, not the last *complete* crawl. **Correction:** `Date` is JSON-LD `datePublished` (657/657 URL-month = Date-month), so a transcript posted late is filed under the current month and is safe. What is actually at risk: backdated or relisted items, items added to the collection late, and items on pages that failed in a run that still advanced max Date. | No permanent loss: in 12 release diffs, 0 new rows fell below the previous floor. Next floor = 2026-10-03 (month 2026-10). 09-dated items first listed after 10-19 can never be discovered. serbia-statehood-day (dated 2026-02) appeared in the archive late and was saved only by the accidental August full walks. No full walk has run since 08-31. | Latent, silent, undetectable without a full walk. Expected loss is a handful per year. | L | P4c, harness |
| **D-05 Failed listing pages continue** | A page ≥2 that fails after `_get_with_retry` gets `continue` (`:569-581`). The walk raises only if there are 0 candidates (`:612-623`). | 10-12 dispatch 38313493994: 9 unreadable pages [3,5,6,10,11,13,14,16,17], 49 candidates vs 81-86 normally. It still merged 13 rows (rows 17688-17700) and advanced max Date. **Correction:** that run did reach the corpus. 10-14 re-covered the window. The "10-12 scheduled 0-candidate" case was an index-page failure, which is now fatal (`:495-506`). | 0 permanent losses so far. Up to about 10 items per failed in-window page. | L | P4b |
| **D-06 `max_page` from pagination links only** | `:509-513` defaults to 1. The canary at `:603` requires `max_page>1`. A page 1 yielding 0 `.collection-result` links also does not raise. | Today: 82 pages parsed, early stop at 11. | About 90% of in-window items lost per run if the markup changes. | L | P4b |
| **D-07 `?` and `/translations/` hrefs rejected silently** | `_is_excluded :249-260` (`_EXCLUDE_PATHS :147`) is applied at `:533` (before `seen+=1`) and at `:466`. | 0 such URLs in the corpus or Project2. No observed instance. | About 0 | L (low) | Strip the query instead of rejecting. Log rejections. |
| **D-08 Off-collection path families** (`/releases/secretary-of-state/`, `/external-content/`) | No code lists these families. Items reach the corpus only if cross-listed in the archive. | Corpus has 3 such rows (17528, 17540 tonga with no 'rubio', 17095). The archive does cross-list this family. | Unknown, low prior | H | GT crawl E4 |
| **D-09 Thin windows / spoken-rate deficit** | Symptom only. Spoken items per month: Rubio 11.7 vs Tillerson 14.9, Pompeo 26.6, Blinken 32.6. Interviews: 3.7 vs Pompeo 12.9. 0 gaggle, town hall or briefing rows. | 0-spoken weeks ending: 2025-08-31, 2025-12-28, 2026-03-22, 2026-06-21, 2026-08-23, 2026-08-30. June 2025 has no G7/NATO remarks. **2026-08-12..08-31 has 0 spoken rows while 32 statements were captured.** Peer comparisons are confounded by archive regime and the NSA role. Corrected peer press-statement shares: 54-59%. | Unknown (about 0 to 440 items) | H | GT crawl E4/E5 as acceptance tests |
| **D-10 Hosted outside state.gov** | Seeds hard-coded at `:88-89`. No whitehouse/congress/usaid/embassy adapters. | 814/814 www.state.gov. White House posts naming Rubio: 4. White House videos: 7. All are mirrored except row 17095. Some White House podium appearances are reposted on state.gov (row 17538, Briefing Room). | About 0 for White House text. Hearings: see S-01. | S | D4/D5 |

### 2b. Fetch

| ID | Mechanism | Evidence | Impact | Status | Fix |
|---|---|---|---|---|---|
| **F-01 One attempt per page, no requeue, green job** | `fetch_rubio_page :694-701`: one `session.get(timeout=30)` on a bare `requests.Session()` (`:1081`). `_get_with_retry` (`:115`) is not used. On failure: `failed += 1; continue` (`:1156-1159`), printed only (`:1249`). The retry window is about 2-6 weeks because the floor is month-granular. | 16 distinct `[fetch] ERROR` URLs (15 read timeouts, 1 403). **All 16 are in the corpus now.** Examples: the 10-12 five (rows 17701-17705), togo 2025/04, malawi 2026/07, KL, traveling-press. The 10-12 run published with `Failed: 5`; the job went red only at the HF step. | 0 lost. Latent risk if a page fails in every run of its window. | L | P3 |
| **F-02 `/external-content/` → whitehouse.gov** | Redirect target, no state.gov anchors. CI saw a 403 once (run 36422566949, 2026-09-28). On 08-31 the same URL showed `Failed: 0`, so the 403 is **intermittent**. Monthless URL, so it is always re-listed. | Only 1 such URL ever seen (row 17095). Pre-fetch dedup normally skips it. | 0 lost today | L | P3 (redirect class + pending) |

### 2c. Classification

| ID | Mechanism | Evidence | Impact | Status | Fix |
|---|---|---|---|---|---|
| **C-01 Third-person gate drops a video-message transcript** | `_THIRD_PERSON_OPENINGS :219-230`. The matching entry is **`'Today, Secretary of State Marco Rubio delivered'` (`:229`)**, after `\xa0` normalization. `_is_active_speaker :898-908` uses `startswith` on `speech_text[:500]` and never looks past the intro. Skip at `:1195-1199`. The pattern has existed since the initial commit (130dab0, 2026-07-14). | `…/2026/05/secretary-rubios-message-to-the-cuban-people-on-their-independence-day/` (Date 2026-05-20, about 868 words). Text preserved in `Project2/rubio_backfill_2026-06-22.csv` (row_id 17483). The marker is title-case **"Translated Transcript"**, after "A courtesy translation of the Secretary's video message is below." Re-run gives `active=False`. It is in no release. Skipped in 12 archive runs (both path variants). It is **now below the floor and no longer even fetched**. In every Project2 backfill CSV, it is the only gate rejection. | **1 proven.** The future risk is the same opening. "Possibly every video message" is unsupported. | P | P2, plus targeted backfill |
| **C-02 No byline check: Spokesperson media notes stored as Rubio "Remarks"** | `_parse_block_metadata :744-791` puts the byline into `block_location`. `_LOC_NORMALIZE :857-862` maps it to Washington, D.C. The type falls to the `'remarks'` default (`:854`). Verifier Check M (`corpus_verify_research_grade.py:395-419`) misses this. | Rows **17461, 17463** ("Media Note / Office of the Spokesperson"), about 274 words of third-person text. Bylines: 806 Rubio, 2 Spokesperson, 1 Trump-first (17509, a legitimate joint event). Test the byline line, not a substring: "Office of the Spokesperson" appears in the footer of 811 rows. | 2 rows (precision) | P | P2 |
| **C-03 Rejections leave no structured record** | `[skip]` lines at `:1017, :1181, :1197` go to stderr only. `:1190` skips silently. The audit upload (`rubio-update.yml:140-150`) carries only Location/People/GPE. | **Corrected count:** 47 "Not active speaker" lines (one log per run), covering 5 pages. Only the Cuban message is a real utterance. The others are an index page, the IWOC preview, and the Gerapetritis and Frostadottir readouts. 0 "Too short" or Date skips. | It is why C-01 went unnoticed for 8 weeks | P (observability) | P0 |
| **C-04 URL blacklist on the sitemap path** | `_NON_SPEAKER_URL_PATS :171-178`, applied at `:287-288`, sitemap path only. | 0 corpus or archived URLs. All 1,115 Wayback blacklisted URLs are readout or travel slugs. One verifier refuted this. | about 0 | R/L | Move decisions after fetch (P2/P4a) |

### 2d. Extraction and speaker filtering (largest measured word loss)

**Common root cause.** `extract_row` (`rubio_backfill.py:925-1023`) calls `speech_recover._run_extraction` (`speech_recover.py:167-183`), which calls `make_speaker_text` (`speech_extract.py:173-223`). For modern state.gov pages there is **no footer cut**. Its output goes to `extract_secretary_only_text` (`hybrid_corpus_recovery.py:631-675`). The Tags cut (Pass 5, `rubio_backfill.py:994-1003`) runs only *after* extraction. Truncation recovery (Pass 4, `:978-992`) is gated on fewer than 500 characters plus `raw_populated`. The other-speaker cleanup (Pass 6, `:1009-1013`) fires only if an uppercase own-line label survives in the text. A replay of the current code reproduces the published SOS Text for 773 of 814 rows, so these defects are live in CI.

| ID | Mechanism | Evidence | Impact | Status | Fix |
|---|---|---|---|---|---|
| **X-01 Final Rubio turn dropped at the footer** (merges the earlier "final-turn-footer", "multi-turn truncation" and "historical-rows-never-reextracted" findings) | The last Rubio chunk has no following label, so it runs to EOF and includes the footer's "Contact Us". `BOILERPLATE_CUES` (`hybrid_corpus_recovery.py:72-75`) matches, and **the whole turn is vetoed** at `:653`. **Correction:** the extractor does *not* stop at the first interjection; it visits every anchor. The cause is the veto plus the unbounded text. It also affects weekly-era rows, not only historical ones. Row 17031 was a false example: its 505 words equal Rubio's turns. | 136 transcripts end on a Rubio turn. **68 lose it, 13,104 words; 31 of those lose ≥20 words; 12 rows date from the weekly era (≥2026-07)** (17552, 17560, 17574, 17578, 17589, 17627, 17657, 17658, 17661, 17669, 17685, 17706). Worst: 17409 CARICOM 141/1,683; 17379 SFRC Venezuela 138/1,572; 17083 1,033/3,693; 17045 2,059/3,937; 17685 1,031/1,823. A looser heuristic gives 76 rows / about 19k words. **Additionally, unconfirmed:** about 15 single-anchor rows (e.g. 17697, 3,590 words) are missing their final text in the release even though the current code recovers it. That would be stale extraction. Re-check in the harness. | About 13.1k words (about 2.4-2.9% of labelled Rubio words); recurs weekly | P | P1a, P1b; offline re-extraction |
| **X-02 Boilerplate cue inside a real turn** | Same veto at `:653`, and the paragraph fallback at `:668`. No word boundaries, so "privacy" also matches "privacy-related". | **Row 17415** (2026-03-03, `remarks-to-press-7`): Rubio's 866-word opening ("I have a couple updates…") is dropped for containing "so people can contact us and register". It is the only non-footer drop across 4,117 anchors. | 1 row, 866 words | P | P1b |
| **X-03 Case-insensitive anchor captures vocatives** | `secretary_anchor_regex` (`hybrid_corpus_recovery.py:623-628`): `(?im)…RUBIO\b[:\- ]*` makes the colon optional. A line like "Secretary Rubio, thank you…" inside another speaker's turn becomes a Rubio anchor. The leaked chunk has no label, so Pass 6 cannot detect it. The function is shared with other secretaries (`corpus_fusion.py`, `speech_recover.py:173/682/769`, `speech_extract.py:252`). | **25 rows, about 2,270 words** (two independent counts: 2,271 / 2,276 / 2,286). 17342 has Hegseth 467 words; 17021 has 397 interviewer words; 17216, 16921, 17702, 17357, 17379 (Risch), 17146 (Bahraini FM), 17392, 17028 (SOS Text begins ", welcome to Honestly."). Title anchors in 17461/17463 are covered under C-02. | Precision, about 3% of Rubio rows | P | P1c (Rubio path only). **Must still accept** `SECRETARY RUBIO\n:` (rows 17285, 17347, 17431, 16951) and `SECRETARY DESIGNATE RUBIO`. |
| **X-04 Non-ASCII, NBSP or ’ labels are not boundaries** | `UPPER_SPEAKER_LINE_RE` (`hybrid_corpus_recovery.py:77`) and `_ANY_UPPER_SPEAKER_RE` (`rubio_backfill.py:202`) use the ASCII class `[A-Z .'\-]`. The only text normalization happens inside `_is_active_speaker` (`:902-905`). | **4 contaminated rows, about 1,130 words** (corrected: adds 17451): 16919 Arévalo about 542; 17189 Ramírez about 205; 17704 Gunnarsdóttir about 170; 17451 Šefčovič about 200 (split label `COMMISSIONER\nŠEFČOVIČ\n:`). Literal labels such as "FOREIGN MINISTER RAMÍREZ: Yes, exactly." appear inside SOS Text. At risk with 0 leakage: 16935 (SA’AR), 17061 (CHAIRMAN\xa0RISCH), 17513 (\xa0JAISHANKAR). | Precision; recurs with every non-ASCII counterpart | P | P1c |
| **X-05 Split-colon labels** (`LABEL\n:`) | `get_text('\n', strip=True)` (`rubio_backfill.py:709`) puts a newline between `<strong>LABEL</strong>` and `:`. The boundary regexes need the colon on the same line. | 8 rows have the pattern: 16951, 16996, 17237, 17285, 17347, 17357, 17431, 17658. Published text is clean today (it came through the legacy path), but **a replay of the current code pollutes 3 of them**: 17347 (Lezcano, about 183 words), 16996 (Dixon, 14), 17237 (Netanyahu "Go ahead."). | Latent on any re-extraction; in effect for new pages | L | P1c |
| **X-06 Typo or split Rubio labels** | Both the anchor and `_RUBIO_TURN_RE` (`:204-207`) require exact `SECRETARY…RUBIO`. A typo label still *ends* the previous chunk via `UPPER_SPEAKER_LINE_RE`. | Census across 814 rows: `SECRETARY RUBI:` (17658): **203 words lost (0/11 sentences)**. `SEC\nRETARY RUBIO:` (17153): 2 words. `SECREATRY RUBIO:` (17538): "Okay." lost. **Corrected:** the colon-on-next-line case causes no artifacts or losses, and `SECRETARY-DESIGNATE` (17464, pre-tenure) is complete. `SENATOR RUBIO` (17194) is a 2022 clip and is correctly excluded. | 1 row, 203 words (+3) | P | P1c, with fuzzy resolver excluding SENATOR and MRS |
| **X-07 Legacy cleaner false labels (pre-CI rows)** | `corpus_finalize_v4_30.clean_text_fields` (`:1297-1330`) → `secretary_turns_from_transcript` (`:1086-1115`) with `SPEAKER_LABEL_RE` (`:131-139`), which is `(?i)` and not line-anchored. CI no longer runs this. | **17435** (Hannity, 2026-03-31) loses 235 words at "question:". **17183** (2025-08-12) loses 179 words at "…decided by:". Legacy path reproduces 8 rows: 16996, 16997, 17061, 17183, 17237, 17347, 17435, 17512. **Caution:** for 16996, 16997, 17347 and 17512 the legacy output is *more correct* than the current extractor (it removes reporter and Lezcano text). For 17061 it is about 900 Rubio words *better*. Of the 42 rows the current code cannot reproduce, 34 come from some other older path. | 2 rows, 414 words | P | Offline re-extraction **with a diff guard** (§6) |
| **X-08 Half-text fallback** | `corpus_fusion._find_speech_content_start` returns `len(text)//2` (`corpus_fusion.py:2493`). The nav tail survives because `_footer_re` (`:2654`) needs `\nRelated`. Pass 1 rescues only state.gov nav. | 3 rows hit it (17095, 17461, 17463). Only **17095** is damaged: the opening "Tonight, Israel took unilateral action…" is lost, SOS Text starts with "hat they believe…", about 90 words are White House nav, and Date is 2025-06-13 while the page says June 12. The full text is still in Raw Text. | 1 row | P | Return −1 / quarantine; repair 17095 from Raw Text |
| **X-09 Stage-direction, footnote and inline residue** | `get_text('\n')` (`:709`). `_STAGE_DIR_LANGUAGE_RE` (`corpus_finalize_v4_30.py:293-297`) has no "via translation". | Footnote markers in 7 rows (17008, 17251, 17255, 17502, 17578, 17686, 17700); footnote bodies appended in 17251, 17492 ("↩︎"), 17700. These are **official errata**: apply them, do not drop them. "(Via translation)" in 4 rows. Orphan-punctuation lines in 42-53 Rubio rows (964 corpus-wide). | Formatting only, about 55-60 rows | P (quality) | P1e |
| **X-10 `(In Spanish.)` placeholders** | Untranscribed at the source. `strip_stage_directions` (`corpus_rubio_update.py:94`) removes the marker inconsistently; 17552 keeps it. | Rubio-only placeholder turns: 3 in 16920. Inline gaps in about 8-10 rows: 16911, 16914, 16915, 16920, 17045, 17189, 17462, 17552, 17690(?), 17538(?). **Not losses:** 17655 and 17658 (translated text follows the marker); 17347, 17568, 17659 (other speakers). No recovery source exists. | About 11-16 segments (scope D7) | S | Flag column |
| **X-11 No coverage validation** | The only gate, `if len(speech_text)<100 and len(raw)<200` (`rubio_backfill.py:1016`), can never fire (minimum Raw Text is 1,243 characters). The quality summary counts only empty text (`:1252-1257`). The verifier label check (`corpus_verify_research_grade.py:142`, `:266-270`) is line-start only. Check J only warns under 20 words. | Every CI run prints "Empty speech_text: 0" while turns were being dropped. | It is why X-01…X-08 stayed silent | P | §5 |
| **X-12 Minimum turn length of 20 characters** (`_collect_rubio_turns :893`) | **Disputed.** One verifier found 8 rows whose SOS Text equals the filtered turns, with closing lines like "Thank you." dropped (16959, 17033, 17258, 17433, 17434, 17516, 17552, 17657). The other found 0 caused by this filter. Other latent rules (`_strip_trailing_footer`, the 120k cap, `strip_state_nav_footer`) have 0 hits. The QUESTION cue is **not** a risk: `\b` after "Mr." does not match "Mr. Chairman", and the cue correctly catches `QUESTION\n:`. **Keep it.** | | ≤8 trivial turns | L/disputed | Drop the minimum in the new segmenter. The harness decides the count. |

### 2e. Merge, verifier, release

| ID | Mechanism | Evidence | Impact | Status | Fix |
|---|---|---|---|---|---|
| **M-01 Location quarantine drops whole speeches** | `corpus_rubio_update.py:249-264`: if `normalize_location` returns '' or fails `assert_schema_safe`, the row gets `continue`. `corpus_location.py:244` (Rule 4 person/title regex on the whole string), plus `'unresolved'` / `'unresolved_region'` (`:247-300`). | Replay: **8 of 814 rows would be quarantined today**: 16986 ("NSA Waltz"), 17237 and 17293 ("Prime Minister’s Office, Jerusalem"), 17238 (Ben Gurion Airport), 17296 (CMCC Southern Israel), 17409 (Basseterre, St. Kitts and Nevis), 17473 (Ritz-Carlton Bahrain), 17609 (RFK byline). Probes "Harry S. Truman Building, Washington, D.C." and "The White House" are also rejected. 0 quarantines in CI so far (these rows predate the code). The replay ignores JSON-LD `contentLocation`. | About 1% of future rows, skewed toward travel | L | P5a |
| **M-02 Quarantine never retried; an all-quarantined week ends green** | Deterministic; the floor moves past the row. `corpus_rubio_update.py:301-303` exits 0. `rubio-update.yml:128-135` blames "www./case-only". The release note (`:180-187`) is gated on `merged=='true'`. **Correction:** the sidecar *is* uploaded (`if: always()`, 90 days) and the log line exists. It is retried, and fails again, while still inside the window. | 0 occurrences | Permanent loss once outside the window | L | P5a |
| **M-03 Verifier is all-or-nothing** | `corpus_rubio_update.py:351-363` runs `sys.exit(1)` on any FAIL. **Correction:** Location problems are now quarantined per row; every other row-level check still blocks the whole release. | Blocked 09-07, 09-14, 09-21, 09-28 (about 3 weeks; cleared by 36457033920). 08-05 was fixed the same day. 0 permanent loss (floor pinned). | Delay | P (delay) | P5b |
| **M-04 Wrong "Washington, D.C." Location** | **Correction:** these come from the legacy Project2 ingest (`Project2/metadata_refine.py:318`, `Project2/corpus_rubio_update.py:117`) and persist because existing rows are never re-derived. The current `_resolve_location` fallback (`rubio_backfill.py:865-870`) is a latent source. | About 10 rows: 16934, 17229, 17236, 17237, 17239, 17293, 17296, 17409, 17410, 17532 (Aboard AF1). All present since v2026.07.14. | Metadata only | P | Patch release + P5a |
| **M-05 Speakers Present conflation** | `corpus_enrich_v1.normalize_speaker_label :352-466`: "MRS RUBIO" becomes "Rubio"; "SECREATRY RUBIO" becomes a separate speaker. | 17610, 17538, 17194 | Labels only | P | Reuse the label resolver from P1c |
| **M-06 Undated page fails the release** | `_parse_date` returns ''. The URL fallback is effectively dead: `DATE_FROM_URL_PATTERNS` (`hybrid_corpus_recovery.py:78-83`) needs a day, and only 1 of 814 URLs has one. Verifier `:227-232`, `:1000-1004`. | 0 observed (one verifier refuted). | Would block the week | L | Fall back to the listing date; per-row hold |
| **M-07 No `concurrency:` group; tag = date** | `rubio-update.yml` | 0 collisions | Delay | L (low) | Add a concurrency group |

### 2f. Scope classes (need decisions in §1)

| ID | Item | Evidence | Status |
|---|---|---|---|
| S-01 | **Congressional hearing Q&A absent** | All 10 Testimony rows are opening statements; no SENATOR or REPRESENTATIVE turns. Row 17379: "I've submitted a written statement…". About 25-35 hours; estimated 50k-150k Rubio words. | P (scope) |
| S-02 | **No modality definition** | 69.8% of rows are written; there is no `Modality` column. | P (scope) |
| S-03 | **Date is publication, not event** | 16926-16928 (Feb 11 events dated 02-12), 17119 | P |
| S-04 | **Pages never refreshed after state.gov edits them** | Dedup at the pre-fetch loop and `corpus_rubio_update.py:229`. No case observed. | H |
| S-05 | **Audio/video-only utterances** | No ASR stage | H |
| S-06 | **Pre-tenure row 17464** | `:1176-1180` vs `:85` | S |

**Refuted (excluded from the fix plan):**
- (slug, Date) dedup false positives: 0 collisions.
- Silent skip on empty URL: unreachable.
- Publish-step tag collisions: never fired.
- Overstated v2026.08.05 release-note count: reporting bug only, already fixed.
- Speaker-filter drops in the original ingest: all repaired; that code is not in CI.
- QUESTION-cue truncation: 0 hits; the cue is needed.

---

## 3. Read-only CI ground-truth crawl

state.gov returns 403 to this server, so the crawl has to run on a GitHub runner, like the weekly job.

**Workflow:** a new `.github/workflows/rubio-ground-truth.yml`. It is triggered by `workflow_dispatch` only. It has **no release, no commit and no Hugging Face publish**. Its outputs are an `actions/upload-artifact` (retention 90 days) and, optionally, a **prerelease** asset tagged `gt-YYYY.MM.DD` that is never marked latest. That keeps `gh release download` in `rubio-update.yml` unaffected.

Requirements: the same minimal requirements file as the weekly job (requests, bs4, lxml, pandas). Do not add analysis dependencies; see the 2026-10-12 numpy<2 incident.

**Script:** `scripts/rubio_ground_truth.py`. Reuse these existing helpers so ground truth and production parse identically:
- `_get_with_retry` (`rubio_backfill.py:115`)
- `_extract_page_links` (`:524-545`) **with the floor disabled**
- `_parse_block_metadata` (`:744-791`), `_parse_date` (`:795-815`)
- the JSON-LD parser inside `fetch_rubio_page` (`:715-725`)
- `load_existing_urls` (`:360-372`)
- `canonical_url_key` / `slug_date_key` (`corpus_keys.py:57-66, 103-109`), `url_year_month`
- `_extract_modern_state_content` (`speech_recover.py:78-85`), `_collect_rubio_turns` (`:877`)

### What to enumerate, in order of cost

| Stage | Requests (est.) | Collect |
|---|---|---|
| **E1 Sitemap inventory** | 1 + about 70 | Every child `<loc>` in `sitemap_index.xml`, with URL count, min/max lastmod, and sample paths. Settles whether `/releases/` lives in `state_press_release-sitemap*`. |
| **E2 Release sitemaps** | in E1 | Every URL in `state_press_release-sitemap*`, `state_briefing-sitemap*`, `state_ext_content-sitemap*`, and `page-sitemap*` whose URL month is ≥ 2025-01 or (if undated) lastmod ≥ 2025-01-15. **No 'rubio' filter.** |
| **E3 Remarks collection walk ×2** | 2 × 82 (fewer if `?results=30` works) | Per page: page number, every `.collection-result` href, type line, title, date, the `input[name=total_pages]` value, the "N items" header text, and response headers `x-cache`, `age`, `cf-cache-status`, `date`. Add a cache-buster query `?gt=<runid>` on the second pass. Also probe the month-scoped listings (`?coll_filter_year=YYYY&coll_filter_month=MM`, **unverified syntax**: record whether the response count changes). Diffing the two passes measures D-03 directly. |
| **E4 Other listings** | about 100-300 | Date-ordered `/releases/office-of-the-spokesman/` listing, `/releases/secretary-of-state/` listing, department-press-briefings listing, from 2025-01-21 onward. |
| **E5 Public schedule** | about 300 (dated) | Secretary's public-schedule releases: extract events marked press, camera, interview or gaggle, giving an independent event inventory for D-09. |
| **E6 Content probe** (the expensive step, kept minimal) | Only for the diff set: (E2 ∪ E3 ∪ E4) minus corpus keys, minus already-classified URLs in `data/rubio_gt_classified.csv`. Expected low hundreds. | Per URL: final URL after redirects and its host, HTTP status, JSON-LD `datePublished`, `dateModified`, `contentLocation`, `author`; block type line, byline line, dateline; title; presence and count of own-line `SECRETARY(-DESIGNATE)? RUBIO\s*\n?:` labels (Unicode-aware, case-sensitive) plus fuzzy-label candidates; markers (case-insensitive `translated transcript`, `video message is below`, `as prepared`, `transcript`); readout opener (`The below is attributable to`); content-block word count; first 600 characters of the content block. |
| **E7 Refresh probe** (optional, separate dispatch input) | 814 | For each corpus Rubio URL: `dateModified` and content-block word count, to measure S-04. |
| **E8 Congress** (no state.gov) | about 50 | congress.gov API committee-meetings with witness "Rubio" since 2025-01-15. GovInfo CHRG package IDs and availability. Committee-site PDF links. |

### Keeping load on state.gov low
- One connection, no parallelism. At least 2 s between requests with ±0.5 s jitter.
- Back off exponentially on 429, 503 and 504, and honor `Retry-After`.
- **Hard cap** of about 1,500 requests per run.
- Resume from a state file (`gt_state.json`) passed between runs as an artifact, so a full inventory can be spread over several nights.
- Order the work: sitemaps (cheap, large coverage) → listings → content fetches for the diff only.
- Send `If-Modified-Since` / `If-None-Match` on E7.
- Identifying `User-Agent` reused from `_HEADERS` (`:136-142`).
- Run off-peak (for example 07:00 UTC Sunday), never at the same time as the Monday cron (`concurrency: state-gov-crawl` shared by both workflows).

### Artifact (`rubio_gt_<date>/`)
- `sitemap_inventory.csv`: `child_loc, n_urls, lastmod_min, lastmod_max`
- `listing_snapshot.csv`: `pass, page, pos, href, url_key, slug_date_key, type, title, list_date, x_cache, age, total_pages, items_header`
- `candidates.csv`: union of E2-E4 with `source_set` (bitmask), `url_key`, `slug_date_key`, `in_corpus`
- `probe.csv`: E6 columns plus a derived `gt_class` ∈ {`rubio_spoken`, `rubio_written_byline`, `rubio_video_transcript`, `rubio_joint`, `readout`, `other_principal`, `spokesperson_third_person`, `external_redirect`, `fetch_error`}
- `ground_truth.csv`: the in-scope subset after applying §1 decisions. Columns: `url_key, slug_date_key, canonical_url, gt_class, modality, event_date, source_set, in_scope`. **This is the file the verifier consumes.**
- `gt_diff.csv`: in-scope minus corpus, with class and evidence
- `drift_report.csv`: E3 pass1 vs pass2, duplicate slots, items-header vs unique count
- `run_meta.json`: counts, request totals, errors, git SHA

### Questions the crawl answers
- **U1:** how many off-collection Rubio items exist (D-01, D-08).
- **U2:** drift rate and the cache hypothesis (D-03).
- **U3:** items listed late or backdated below the floor (D-04; compare against E3 positions and `datePublished`).
- **U4:** refreshed pages (S-04).
- **U5:** whether thin windows are real (D-09; E4 and E5 inside the windows listed in that finding, plus 2026-08-12..31).
- **U6:** hearing inventory (S-01).

---

## 4. Pipeline changes, in order

Each step is independently shippable. Earlier steps make the effect of later ones measurable.

**P0. Observability (first).**
- `rubio_backfill.py`: write `data/rubio_skipped.csv` with `url, reason, stage, block_type, byline, title, first_400, label_inventory, run_id` from every rejection path (`:1017, :1181, :1190, :1197`, the `_is_excluded` drops at `:533`/`:466`, and the fetch failures at `:1156`). Add it to the `Upload audit sidecars` list (`rubio-update.yml:140-150`).
- Write `data/rubio_listing_snapshot.csv` for every walk (the same schema as E3, scoped to the window).
- Print `listed_in_window`, `unique`, `dup_slots`, `failed_pages`, `fetched`, `failed`, `skipped_by_reason`.
- Fix the misleading `merged=false` message (`rubio-update.yml:133-135`) so it reports dedup rejects and quarantines separately.

**P1. Extraction core (largest word recovery; runs offline on stored Raw Text).**
- **a. Bound the text before extraction.** In `extract_row` (`rubio_backfill.py:925-1023`), compute the content block (hide → Tags / "Back to Top") with `_extract_modern_state_content` (`speech_recover.py:78-85`) and feed *that* to `_run_extraction`. Move Pass 5 ahead of extraction. This alone fixes X-01.
- **b. Remove the whole-chunk `BOILERPLATE_CUES` veto** on the Rubio path (`hybrid_corpus_recovery.py:653, :668`). If a guard is still wanted, require several nav tokens or a run of short lines, and trim the chunk instead of dropping it. Fixes X-02.
- **c. One label segmenter.** New module `scripts/rubio_turns.py`, used by `extract_row`, Pass 4, Pass 6, `_has_non_rubio_label`, `corpus_enrich_v1.normalize_speaker_label`, the reconciler and the verifier. It:
  - normalizes `\xa0` to a space and ’ to an apostrophe;
  - joins split labels (`^LABEL\n\s*:` → `LABEL:`; `^SEC\nRETARY` → `SECRETARY`; three-line `COMMISSIONER\nŠEFČOVIČ\n:`);
  - treats a label as an own-line, case-sensitive span where `lbl == lbl.upper() and any(c.isalpha())`, of length 3-60, followed by `:`, or an inline `LABEL: text`;
  - resolves Rubio labels (`SECRETARY(-| )?(DESIGNATE)? (OF STATE )?(MARCO )?RUBIO`, or a fuzzy match within edit distance 2 of `SECRETARY RUBIO` / `RUBIO`) and **excludes** `MRS\.? RUBIO` and `SENATOR RUBIO`;
  - logs every unresolved label containing `RUB` or `SECR`;
  - turns end only at labels, so vocative "Secretary Rubio," never starts a turn;
  - keeps `QUESTION\n:` handled as a label (the reason the old cue existed);
  - has no 20-character minimum; it removes stage directions instead.

  Build SOS Text from the concatenated Rubio turns whenever the block has ≥1 Rubio label. Use `extract_secretary_only_text` only for label-less pages. **Do not change the shared** `secretary_anchor_regex` (`hybrid_corpus_recovery.py:623-628`) globally, because other secretaries depend on it; route Rubio through the new path. Fixes X-03 to X-06.
- **d.** `corpus_fusion.py:2493`: return −1 instead of `len//2` when called from the Rubio path. The caller then uses the content block or quarantines the row (X-08).
- **e.** Use per-`<p>` text (`get_text(' ')` inside each block element, blocks joined with `\n`) in `fetch_rubio_page :709`, so the source no longer creates orphan lines and split labels. Extend `_STAGE_DIR_LANGUAGE_RE` (`corpus_finalize_v4_30.py:293-297`) to cover `via translation`. Apply footnote errata (replace a `[n]` marker with the footnote text, then drop the footnote block). Add a column `Untranscribed Rubio Segments` counting `(In Spanish.)` placeholders in Rubio turns before stripping them (X-09, X-10).

**P2. Classification.**
- `_is_active_speaker` (`:898-908`): when an opening matches, search the block case-insensitively for a transcript marker (`translated transcript`, `courtesy translation`, `video message is below`, `as prepared`, `as delivered`) or any Rubio label. If found, keep the page and set SOS Text to the text after the marker. Narrow the `… and` patterns to readout verbs (`met|spoke|discussed|held`).
- Add a byline gate using `_parse_block_metadata`: extract the byline line (index 2) as its own field, stop using it as Location (`:857-862`), keep a page when the byline names Marco Rubio or it has Rubio labels, and send `Office of the Spokesperson` / `Department Spokesperson` bylines to the skip sidecar (C-02).
- Map the block type `Media Note` to its own Speech Type instead of the `'remarks'` default (`:854`).
- Add the `Modality` column (D2).

**P3. Fetch.**
- `fetch_rubio_page` (`:694-701`): use `_get_with_retry` with the `(10,45)` timeout.
- Persist `data/rubio_pending.csv` (url, first_seen, attempts, last_error) as an asset of each release (fetched with `gh release download --pattern rubio_pending.csv`). Re-feed it through `--urls` every run regardless of `--since`, and remove entries once ingested.
- Detect redirects to a non-state.gov host (`/external-content/`) and record them as `external_redirect` with the target URL. Try once with a dedicated extractor; on failure leave the entry pending.
- Warn in the release notes when `failed>0`. Open an issue (`gh issue create`) when the same URL fails 3 runs in a row.

**P4. Discovery.**
- **a. Second independent source.** In `discover_sitemap_urls` (`:432-437`), also read `state_press_release-sitemap*`, `state_briefing-sitemap*` and `state_ext_content-sitemap*`. For these, skip `_is_rubio_speech_url` and `_NON_SPEAKER_URL_PATS`; filter by URL `/YYYY/MM/` ≥ floor month or `lastmod ≥ since`, then classify **after fetch** using P2. Limit fetches to the 'rubio' slug class plus non-readout slugs, capped at about 50 per run, until GT data sets the true need. In incremental runs give sitemaps the retry budget too (`:645-648`).
- **b. Make the remarks walk verifiable.**
  - Parse `input[name=total_pages]` and the "N items" header.
  - Count `dup_slots`. If `dup_slots>0` or `unique < expected_in_window`, re-walk the affected pages with a cache-buster.
  - Make a failed listing page inside the floor window **fatal** (`:571-581`, `:612-623`).
  - Fail if page 1 yields 0 links, or if `max_page==1` with ≥10 links (`:509-513`).
  - Use month-scoped listings or `results=30` if E3 confirms they work.
- **c. Floor.** Persist `last_complete_floor` (from a run with no failed pages and no duplicate slots) in `data/rubio_discovery_state.json` as a release asset. Set `since = min(last_complete_floor, max_date − 60d)` and drop the per-link month filter for links whose key is not already in the corpus. Add a `schedule:` cron for the first Monday of each month that sets `full_rescan` (`rubio-update.yml:4-11, :85`). It walks everything and fetches only new keys, so the cost is about 82 listing requests plus new items.
- **d. Supplemental list.** `data/rubio_supplemental_urls.txt`, with comments, holding the known off-collection items, passed through `--urls` in CI (`rubio-update.yml:82-85`).
- **e.** `_is_excluded` (`:249-260`): strip query strings instead of rejecting the URL.

**P5. Merge, verifier, workflow.**
- **a. Location never drops a row.**
  - In `corpus_rubio_update.py:249-264`, on '' merge the row with a provisional Location (country-level if derivable, else the newsroom default) and new columns `Location Method` / `Location Provisional=1`, recorded in the sidecar.
  - In `corpus_location.py:244`, run Rule 4 per comma part, only when no part resolves; ignore possessives (`Minister’s Office`, `Ambassador's Residence`) and initialed building names (`Harry S. Truman`).
  - Add a venue alias table: Ben Gurion → Tel Aviv, Israel; Ritz-Carlton Bahrain → Manama, Bahrain; White House / Oval Office / Capitol / Truman Building → Washington, D.C.; plus `St.` → `Saint` region aliases.
  - Only then remove the `'Washington, D.C.'` fallback in `_resolve_location` (`rubio_backfill.py:865-870`), and prefer the block dateline over JSON-LD when they disagree, logging the disagreement.
- **b. Per-row verifier gating.** In `corpus_rubio_update.py:351-363`, run the row-local checks on new rows first, divert failing rows to `data/rubio_held.csv` with the check name (re-attempted each run like pending entries), and merge the rest. Corpus-global checks (Q, Z, Row Id, gaps) stay hard failures. Exit non-zero, or open an issue, when anything is held.
- **c.** Add `concurrency: {group: state-gov-crawl, cancel-in-progress: false}` to both workflows.
- **d.** Date fallback order: JSON-LD, block date (also accepting abbreviated months such as `Sept. 3, 2025`), listing date from the snapshot, URL month. Add the `Event Date` column (S-03).

**P6. Scope expansions** (only after the D-decisions). Congressional adapter (GovInfo CHRG, with GPO `Secretary RUBIO.` turn format), with a pending-hearings queue that is not subject to the 14-day floor. `Source Host` and `Role` columns.

---

## 5. Coverage-verification harness

### 5a. `scripts/rubio_coverage_reconcile.py`

**Inputs:**
- `--corpus corpus.csv`
- `--ground-truth ground_truth.csv` (from §3)
- `--exclusions data/rubio_coverage_exclusions.csv` with columns `url_key, slug_date_key, reason, decided_by, date`. Seed it with the readouts, the IWOC preview, the index page, the `SENATOR RUBIO` clip, and the pre-tenure 17464 if D3 says so.
- optional `--listing-snapshot`

**Item level:**
- Normalize with `canonical_url_key` plus a spokesperson→spokesman path fold, and with `slug_date_key`. Match on **either** key. Do not match on slug alone: the corpus has 814 keys but only 778 slugs.
- Report a status per GT row: `present`, `missing`, `excluded`, `out_of_scope`.
- Report per-month and per-`gt_class` recall.
- Flag numbered-slug-family gaps (`remarks-to-the-press-N`, `remarks-to-press-N`, `…-N`).

**Turn level**, for every Rubio row with ≥1 label in the Raw Text content block, using `rubio_turns.py`:
- `rubio_recall`: the share of Rubio-turn sentences (normalized, 6-gram shingles) found in SOS Text.
- `foreign_leak_words`: words from non-Rubio turns found in SOS Text.
- `untranscribed_segments`.
- Flags for a mid-word start (first char lowercase and the preceding raw char a letter) and for a non-Rubio byline.

**Outputs:** `coverage_items.csv`, `coverage_turns.csv`, `coverage_summary.json`, and a Markdown summary for release notes. **Exit code 1** if any in-scope GT item is `missing`, or if any row has `rubio_recall < 0.98` or `foreign_leak_words > 0`. Rows on an allowlist with an expiry date get exit 0 with a warning.

**Regression test fixture:** these rows must reach the target once fixed:
- 17409, 17379, 17083, 17045, 17685, 17415, 17658, 17435, 17183, 17706 (full recall)
- 17342, 17021, 16919, 17189, 17451, 17704, 17347, 16996, 17237 (zero leak)
- 17095 (clean start)
- the Cuban message raw text from Project2 (kept)

### 5b. Verifier checks (`corpus_verify_research_grade.py`)

| Check | Rule | Severity |
|---|---|---|
| **AC Ground-truth membership** | Load `--ground-truth` (the newest `gt-*` prerelease asset, downloaded in `rubio-update.yml` before the merge step). For each in-scope GT row with `event_date ≥ 2025-01-21` and `first_seen ≤ corpus max Date − 7d`, FAIL if not present and not excluded. Print the URLs. | FAIL (release); in weekly runs, missing items are **auto-added to `rubio_pending.csv`** first, so FAIL fires only for items that stay missing across 2 runs |
| **AD Rubio turn recall** | `rubio_recall ≥ 0.98` | FAIL for new or changed rows; WARN for legacy rows until the patch release, then FAIL |
| **AE Foreign leak** | `foreign_leak_words == 0`. Also extend `SPEAKER_LABEL_LINE` (`:142`, `:266-270`) to catch **inline** uppercase labels anywhere in SOS Text, Unicode-aware. | FAIL |
| **AF Byline** | Rubio rows whose block byline names neither Rubio nor a joint principal | FAIL |
| **AG Mid-word start / nav tail** | Lowercase first character preceded by a letter in Raw Text; `Related … Notifications` tail | FAIL |
| **AH Listing reconciliation** | From the weekly `rubio_listing_snapshot.csv`: every in-window listed href is present, excluded, pending, or held | FAIL |
| **AI Pending age** | Entries in `rubio_pending.csv` or `rubio_held.csv` older than 21 days | WARN, plus an issue |

---

## 6. Backfill procedure for the new release

1. **Branch and freeze.** Branch `rubio-coverage-v1` from the main branch of `sos-corpus-clean`. Baseline is `v2026.10.19` (814 Rubio rows / 17,708 total). Make no pushes to the old archive repo.
2. **Land P0, P1 and P2 code with unit tests.** Use the §5a fixture plus synthetic cases: split colon, NBSP, `SEC\nRETARY`, `SECRETARY RUBI:`, vocative line, `MRS RUBIO`, `SENATOR RUBIO`, `QUESTION\n:`.
3. **Run the ground-truth crawl (§3)** by dispatch, over 1-3 nights. Download the artifact. Hand-review the `gt_diff.csv` rows whose classes are ambiguous (`rubio_written_byline` vs `spokesperson_third_person`; joint statements per D2). Write the decisions to `data/rubio_coverage_exclusions.csv` and `data/rubio_supplemental_urls.txt`.
4. **Seed the supplemental list** with the known items, then everything GT finds:
   - `…/office-of-the-spokesperson/2025/12/secretary-of-state-marco-rubio-remarks-to-press-at-donald-j-trump-institute-of-peace/`
   - `…/office-of-the-spokesperson/secretary-of-state-marco-rubio-remarks-to-press-at-the-miami-homestead-airport`. It has no `/YYYY/MM/`, so check that `url_year_month` = None is always kept and that Date comes from JSON-LD.
   - `…/office-of-the-spokesman/2026/05/secretary-rubios-message-to-the-cuban-people-on-their-independence-day/` (also try the spokesperson variant)
   - the two Rubio–RFK Jr. joint statements, if D2 includes them
5. **Offline re-extraction of all 814 existing rows** from stored Raw Text (no fetch) with the new extractor. This yields a `reextract_diff.csv` with old and new words, recall and leak for each row. Accept a new SOS Text automatically only if `rubio_recall_new ≥ rubio_recall_old` **and** `leak_new ≤ leak_old`. Review by hand every row where new < old in words; known cases are 17061, 16951, 17237, 17095, and the legacy-path rows 16996, 16997, 17347, 17512.

   Expected repairs:
   - about 68 final-turn rows (+about 13.1k words)
   - 17415 (+866), 17658 (+203), 17435 (+235), 17183 (+179)
   - the about 15 stale single-anchor rows, if confirmed
   - leak removal: 25 vocative rows (−about 2.27k), 4 Unicode rows (−about 1.13k)
   - 17095 rebuilt from Raw Text, with Date checked against the page's June 12
   - 17461 and 17463 retyped or excluded per D2
   - footnote errata and "(Via translation)" cleanup
   - recompute `SOS Word Count`
6. **Metadata re-derivation.**
   - Location for the about 10 legacy D.C. rows plus the 8 quarantine-prone rows, with the new normalizer and the `Location Provisional` flag.
   - Speakers Present for 17610, 17538, 17194.
   - New columns `Modality`, `Event Date`, `Untranscribed Rubio Segments`, `Source Host`.
   - `pre_tenure` flag on 17464, per D3.
7. **Fetch the missing items in CI.** A dispatch of `rubio-update.yml` with a new input `supplemental=true` runs `rubio_backfill.py --urls data/rubio_supplemental_urls.txt` with the fixed code into `rubio_new.csv`, then `corpus_rubio_update.py` (dedup, Row Id append, enrichment through `corpus_enrich_v1.py`, NER/GPE/people, `corpus_keys.py`, `corpus_location.py`), with **publish disabled** (new input `dry_run=true`) and the result uploaded as an artifact.
8. **Patch script.** `scripts/corpus_patch_v2026_10_xx_rubio_coverage.py`, modelled on `corpus_patch_v2026_10_03`. It applies step 5 and 6 changes by Row Id and appends step 7 rows. Run the verifier with `--baseline v2026.10.19 --allow-changed-rows <explicit Row Id list>` (Check Q), plus the new checks AC-AG and `rubio_coverage_reconcile.py --ground-truth`. Both must pass with exit 0.
9. **Release.** A `v2026.10.2x` GitHub release with these assets: the corpus CSV, `rubio_coverage_report.md`, `coverage_items.csv`, `coverage_turns.csv`, `ground_truth.csv` (also published as `gt-YYYY.MM.DD`), `rubio_pending.csv` and `rubio_discovery_state.json`. Release notes: rows added (by class), rows changed (with reasons), Rubio words recovered, leak removed, and the documented out-of-scope classes. Then publish to Hugging Face and check that the CI requirements still install cleanly.
10. **Switch the weekly job** to P3, P4 and P5, AC-AI enforcement. Full rescans and ground-truth crawls are manual dispatches only (decided 2026-10-21: weekly-only schedule, lowest load on state.gov). Update the paper datasheet and verifier counts to describe the new coverage definition and its documented exclusions (hearing Q&A until P6, audio/video-only, White House video, untranscribed Spanish).

**Exit criteria for "100%":** reconciler exit 0 against the latest ground truth (item recall = 1.0 over in-scope classes, with every exclusion justified in `rubio_coverage_exclusions.csv`), plus turn-level recall ≥ 0.98 and leak = 0 on every labelled Rubio row, sustained across two consecutive weekly runs.