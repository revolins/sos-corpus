# Title column repair (2026-10-22)

**Rule.** Title is the verbatim state.gov headline of the item, with the secretary's name kept, e.g. "Secretary Antony J. Blinken and Qatari Foreign Minister … Before Their Meeting". It is normalised the same way as every other Title: ftfy runs over it, so quotes are straight and there are no NBSPs.

## What was wrong (v2026.10.19, 17,708 rows)

The one-off v4.4–v4.11 finalize scripts in `Project2/` filled empty titles from sources other than the headline, and later stages carried those titles forward. The headline itself survives in Raw Text.

| Reason | Rows | Example (old → headline) |
|---|---:|---|
| `slug_derived` (v4.5 title-cased URL slug; the greedy name strip `^secretary-(?:[a-z]+-){2,4}` cut words) | 4,447 | "And Greet With Mission Kuwait" → "Secretary Antony J. Blinken Meet and Greet with Mission Kuwait"; "Kazakhstans Republic Day" → "Kazakhstan's Republic Day" |
| `speech_sentence` (v4.4 took the first line of the speech) | 706 (+2 counted above) | "Glad to be with you." → "Interview With Juan Williams of National Public Radio" |
| `split_heading` (1997-2001 heading split over lines; only the second half kept) | 276 | "Italian Foreign Minister Lamberto Dini" → "Secretary of State Madeleine K. Albright and Italian Foreign Minister Lamberto Dini" |
| `fragment_start` (cut slug that is a suffix of the headline) | 24 | "To The Press" → "Secretary Michael R. Pompeo Remarks to the Press" |
| `headline_mismatch` (other differences on 2017+ pages: another item's title copied onto the row, a shortened `<title>`, NBSP/curly quotes on weekly rows) | 59 | "On Otto Warmbier" on the Mozambique National Day row |

**Total: 5,512 titles.**

| Secretary | Titles |
|---|---:|
| Blinken | 2,978 |
| Pompeo | 1,626 |
| Albright | 309 |
| Tillerson | 283 |
| Rice | 137 |
| Kerry | 71 |
| Clinton | 42 |
| Rubio | 36 |
| Powell | 30 |

The Rubio rows are 11 legacy slug titles (16914 … 17451), plus a shortened `<title>` and unnormalised quotes on weekly rows.

**Where the new titles come from:**
- 5,385 are recovered from Raw Text by `corpus_title.page_title`, which tries three sources in turn:
  1. the 2021+ "hide" block headline;
  2. the `<title>` minus the site suffix;
  3. on flattened pages, the text before the site chrome.
- 128 rows have no page header in Raw Text (127 of them change). They take the title the original scrape recorded (`data/title_overrides.csv`, status `override`, sourced from `Project2/hybrid_corpus_recovery.state.sqlite` and `secstate_naive_corpus*.csv`).
- 3 rows are `unresolved` and keep their old title until a fetch recovers the headline: 15480, 15848, 16364.

**Old archive hosts** (1997-2001, 2001-2009, 2009-2017): a title that isn't flagged is kept even when it differs from `<title>`. On these hosts `<title>` is a terse catalogue string ("8/10/98 Albright on CBS Evening News"), while the stored Title is usually the fuller in-page heading.

## Related text defect, not repaired here

On about 350 rows (mostly Blinken-era written statements; no Rubio rows), the old title pass **moved** the first sentence out of the text: SOS Text starts at sentence two, or mid-word ("nd\nConstitution Day"). The repaired Title no longer hides this, but the sentence is still missing from SOS and Natural Text. Restoring it from Raw Text is a separate text patch.

## Pipeline changes

- **`scripts/corpus_title.py`:** `page_title`, `bad_title_reasons`, `repair_title`, `title_problems`. Stdlib only, so the verifier can import it.
- **`scripts/corpus_patch_titles.py`:** the catch-up patch. Only Title changes; Row Hash is recomputed. It writes an audit CSV for `--allow-changed-rows`, which now accepts `field=Title`.
- **Weekly path:**
  - `rubio_backfill.page_headline` takes the Title by the same rule, then the JSON-LD headline, og:title and `<title>`. It never uses the slug or the speech; if nothing is found the Title stays empty, and the per-row gate's Empty Title check holds the row.
  - `corpus_rubio_update` normalises the Title like finalize does.
- **Verifier Check AJ** (`--titles` / `--strict-titles`) fails a new or changed row whose Title has a `bad_title_reasons` hit, or that differs from a recoverable headline on a 2017+ host. Unchanged legacy titles only warn, and `unresolved` overrides warn. The weekly per-row gate and the full verifier run pass `--titles`, so a bad new title is held, not published.

**Verification** (offline, on v2026.10.19, with `--strict-titles --rubio-coverage`):

```
python corpus_patch_titles.py --input corpus_v2026.10.19.csv --output patched.csv
python corpus_verify_research_grade.py --input patched.csv --baseline corpus_v2026.10.19.csv \
    --allow-changed-rows patched_title_audit.csv --strict-titles --rubio-coverage
```

Result: PASSED.
- Check Q: 5,512 audited changes accepted.
- Check AJ: passes on every row; 3 unresolved rows warn.
