# Title column repair and lost-sentence restore (2026-10-22/23)

**Rule.** Title is the verbatim state.gov headline of the item, with the secretary's name kept, e.g. "Secretary Antony J. Blinken and Qatari Foreign Minister … Before Their Meeting". It is normalised the same way as every other Title: ftfy runs over it, so quotes are straight, there are no NBSPs and no mojibake.

## What was wrong (v2026.10.19, 17,708 rows)

The one-off v4.4–v4.11 finalize scripts in `Project2/` filled empty titles from sources other than the headline, and later stages carried those titles forward. The headline itself survives in Raw Text.

| Reason (a row can have several) | Rows | Example (old → headline) |
|---|---:|---|
| `slug_derived` (v4.5 title-cased URL slug; the greedy name strip cut words) | 4,445 | "And Greet With Mission Kuwait" → "Secretary Antony J. Blinken Meet and Greet with Mission Kuwait"; "Kazakhstans Republic Day" → "Kazakhstan's Republic Day" |
| `speech_sentence` (v4.4 took a line of the speech) | 740 | "Glad to be with you." → "Interview With Juan Williams of National Public Radio" |
| `split_heading` (1997-2001 heading split over lines; only the second half kept) | 276 | "Italian Foreign Minister Lamberto Dini" → "Secretary of State Madeleine K. Albright and Italian Foreign Minister Lamberto Dini" |
| `fragment_start` (a cut slug that is a suffix of the headline) | 146 | "To The Press" → "Secretary Michael R. Pompeo Remarks to the Press" |
| `truncated` (heading cut at a line break) | 116 | "Statement by" → "Statement on International Women's Day"; "Op-Ed on the Crisis in Kosovo in" → "… in The Washington Post" |
| `headline_mismatch` (other differences on 2017+ pages, e.g. "On Otto Warmbier" copied onto three other rows) | 37 | "On Otto Warmbier" on the Mozambique National Day row |
| `mojibake` / `site_chrome` | 18 | "Indian Independence DayÂ"; "U.S. Department of State: Secretary Powell Interview … U.S. Department of State" |

**Total: 5,612 titles.**

| Secretary | Titles |
|---|---:|
| Blinken | 2,979 |
| Pompeo | 1,626 |
| Albright | 337 |
| Tillerson | 283 |
| Rice | 195 |
| Kerry | 73 |
| Clinton | 44 |
| Powell | 39 |
| Rubio | 36 |

**Where the new titles come from:**
- 5,174 are recovered from Raw Text by `corpus_title.page_title`, which tries three sources in turn:
  1. the 2021+ "hide" block headline;
  2. the `<title>` minus the site suffix;
  3. on flattened pages, the text before the site chrome.
- 303 join a split or truncated heading with its next line.
- 135 rows have no page header in Raw Text. They take the title the original scrape recorded (`data/title_overrides.csv`, status `override`).
- 3 rows are `unresolved` and keep their old title: 15480, 15848, 16364.

`page_title` never accepts any of these as a headline: a bare type ("Remarks"), a stage direction ("(Via interpreter.)"), or a first line that is the item's opening sentence.

**Old archive hosts** (1997-2001, 2001-2009, 2009-2017): a title that isn't flagged is kept even when it differs from `<title>`. On these hosts `<title>` is a terse catalogue string, while the stored Title is usually the fuller in-page heading.

## Text the bad titles took with them

`corpus_finalize_v4_30.strip_leading_title` (Pass 4 of `strip_boilerplate`) deletes the leading lines of a text that repeat its Title. Wherever the Title was a line of the speech, that pass deleted the line.

| | Rows |
|---|---:|
| Text restored | 682 |
| Blinken | 387 |
| Rice | 122 |
| Kerry | 65 |
| Clinton | 37 |
| Powell | 27 |
| Pompeo | 23 |
| Tillerson | 13 |
| Albright | 8 |
| Rubio | 0 |

What was lost:
- **Written statements** lost their opening sentence (or bold lead line) from both SOS and Natural Text, often leaving a `<sup>` ordinal fragment ("nd\nanniversary of your independence").
- **Interviews and remarks** lost the Secretary's first answer from SOS Text.

How `corpus_title.restore_row` puts it back:
- **Natural Text** is restored from Raw Text.
- **SOS Text** is restored from the restored Natural Text, keeping only the Secretary's turns. Turns are joined by a blank line, and finalize's `strip_stage_directions` runs over the result.
- **Ordinals** split at the seam are rejoined ("45th anniversary").
- **The restore only runs when the old title was speech:** found in the page body, not the header, and flagged `speech_sentence`.

19 rows where the restore declined are listed in the audit as `field=review` and left unchanged: 54, 853, 3582, 3734, 4261, 4742, 4845, 5149, 5602, 7157, 8372, 8920, 9964, 9979, 10740, 13717, 14238, 15675, 16788.

`strip_leading_title` now skips any title that reads like speech, so the weekly path cannot repeat this.

## Pipeline changes

- **`scripts/corpus_title.py`:** `page_title`, `bad_title_reasons`, `repair_title`, `title_problems`, `restore_row`. Stdlib only, with ftfy used when present.
- **`scripts/corpus_patch_titles.py`:** the catch-up patch. It restores text first (that needs the old title), then repairs titles. Changed rows get word counts, Row Hash, Speakers Present and NER recomputed. It writes an audit CSV for `--allow-changed-rows`, which now accepts `field=Title`.
- **Weekly path:**
  - `rubio_backfill.page_headline` takes the Title by the same rule, then the JSON-LD headline, og:title and `<title>`. It never uses the slug or the speech; if nothing is found the Title stays empty, and the per-row gate's Empty Title check holds the row.
  - `corpus_rubio_update` normalises the Title.
- **Verifier Check AJ** (`--titles` / `--strict-titles`) fails a new or changed row whose Title has a `bad_title_reasons` hit, or that differs from a recoverable headline on a 2017+ host. Unchanged legacy titles only warn, and `unresolved` overrides warn. The weekly per-row gate and the full verifier run pass `--titles`.

## Verification (offline, on v2026.10.19)

```
python corpus_patch_titles.py --input corpus_v2026.10.19.csv --output patched.csv
python corpus_verify_research_grade.py --input patched.csv --baseline corpus_v2026.10.19.csv \
    --allow-changed-rows patched_title_audit.csv --strict-titles --rubio-coverage
```

Result: PASSED.
- Check Q: 6,692 audited changes accepted.
- Check AJ: passes on every row; 3 unresolved rows warn.
- Rubio coverage checks: unchanged.

**Second, independent audit.** It flags titles by shape (utterance openers, first-person sentences, truncated endings, one word, generic type only, mojibake, chrome, slug casing, not found in Raw Text, disagreement with the URL slug).

| Flag | Before | After |
|---|---:|---:|
| Utterance openers | 534 | 7 |
| First-person sentences | 486 | 1 |
| Slug disagreement | 289 | 10 |
| Slug casing | 220 | 1 |
| Truncated endings | 147 | 9 |
| Mojibake | 16 | 0 |

Every remaining flag was reviewed by hand, and all are genuine headlines:
- first-person op-ed titles ("I Was In The Room Too");
- country statements ("Grenada");
- long multi-party meeting titles;
- venues that contain "Department of State";
- 79 generic headlines such as "Remarks to the Press", which is what state.gov published for those pages.
