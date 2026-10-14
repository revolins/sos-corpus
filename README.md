# The United States Secretary of State Speech Dataset (USSOS)

[![Latest release](https://img.shields.io/github/v/release/revolins/sos-corpus?label=corpus)](https://github.com/revolins/sos-corpus/releases/latest)
[![Weekly update](https://github.com/revolins/sos-corpus/actions/workflows/rubio-update.yml/badge.svg)](https://github.com/revolins/sos-corpus/actions/workflows/rubio-update.yml)
[![PyPI](https://img.shields.io/pypi/v/sos-speeches)](https://pypi.org/project/sos-speeches/)
[![PyPI package](https://github.com/revolins/sos-corpus/actions/workflows/check-pypi.yml/badge.svg)](https://github.com/revolins/sos-corpus/actions/workflows/check-pypi.yml)
[![R package](https://github.com/revolins/sos-corpus/actions/workflows/check-r.yml/badge.svg)](https://github.com/revolins/sos-corpus/actions/workflows/check-r.yml)
[![Hugging Face dataset](https://github.com/revolins/sos-corpus/actions/workflows/check-hf.yml/badge.svg)](https://github.com/revolins/sos-corpus/actions/workflows/check-hf.yml)
[![Code: MIT](https://img.shields.io/badge/code-MIT-blue)](https://github.com/revolins/sos-corpus/blob/main/LICENSE)
[![Data: CC0](https://img.shields.io/badge/data-CC0%201.0-lightgrey)](https://github.com/revolins/sos-corpus/blob/main/DATA_LICENSE)

The US Secretary of State Speech Dataset (USSOS Dataset) is a corpus of U.S. Secretary of State public communication from Madeline Albright (January 1997) through the present, with Rubio-era speeches updated automatically each week. Public communication that are featured in the dataset includes major addresses, public statements, interviews, and press conferences, among others. 

The dataset is at the speech-level and captures the location of the remarks, the date, the type of speech, the URL, the raw scraped text, the full cleaned text, a column that captures only the remarks of the Secretary of State, as well as a suite of additional variables.

## Download

The full corpus is available via four channels:

### GitHub Release (canonical)
```bash
# Download the latest release using the gh CLI
gh release download --pattern "corpus_v*.csv" --dir .
```
Or browse **Releases** on the right sidebar to download a specific version.


### Python package loader (recommended)
```bash
pip install sos-speeches
```

```python
import sos_speeches

# Download and cache
csv_path = sos_speeches.load()

# Load directly as a Pandas DataFrame
df = sos_speeches.load(as_dataframe=True)
print(df.shape)  # e.g. (17701, 19)
```

### R package (recommended)

The [`sosspeech`](https://github.com/revolins/sos-corpus/tree/main/r-package) R package provides a convenient way to download and load the Secretary of State speech corpus directly into R. The package includes compressed data files, so you do not need to download the CSV separately.

Install the package directly from GitHub:

```r
install.packages("remotes")
remotes::install_github(
  "revolins/sos-corpus",
  subdir = "r-package"
)

library(sosspeech)

# Load the full corpus
speeches <- sos_load()

# Load specific years (optional)
speeches_2020 <- sos_load(years = 2020)

# Inspect the data
print(dim(speeches))

```

### Hugging Face Hub
```python
# Using the datasets library
from datasets import load_dataset
ds = load_dataset("JayRevolinsky/sos-secretary-speeches")

# Or with a direct hf_hub_download
from huggingface_hub import hf_hub_download
csv_path = hf_hub_download(
    repo_id="JayRevolinsky/sos-secretary-speeches",
    filename="corpus.csv",
    repo_type="dataset",
)
```

## Dataset columns

Each row is one public communication by a sitting Secretary of State. Every release has 19 columns: 11 core columns and 8 derived columns added by the enrichment stage. The table follows the grouping in the paper (Table A1); in the CSV itself, `Canonical URL` is the last column.

| Column | Description |
|--------|-------------|
| `Row Id` | Sequential integer (0-based); renumbered on each release |
| `Secretary` | Last name: Albright, Powell, Rice, Clinton, Kerry, Tillerson, Pompeo, Blinken, Rubio |
| `Date` | ISO `YYYY-MM-DD` |
| `Title` | Normalized page title (site suffix removed) |
| `Speech Type` | Controlled vocabulary: Remarks, Speech, Interview, Testimony, Press Statement, Statement, Press Availability, Joint Press Availability, Press Briefing, Op-Ed, Town Hall, Remarks to the Press |
| `Location` | Strict `City, Region` — exactly one comma. `Washington, D.C.` for a newsroom dateline (Office of the Spokesperson/Spokesman, teleconference) or when unspecified. A row whose location cannot be resolved to this form is quarantined rather than published |
| `URL` | Source URL exactly as originally scraped. Immutable — it is hashed into `Row Hash`, so it is never rewritten even when the page moves |
| `SOS Text` | Secretary-only utterances — speaker labels and boilerplate stripped |
| `Natural Text` | Full transcript or document body — all speakers, boilerplate stripped |
| `Raw Text` | Full page text as scraped, unmodified |
| `Canonical URL` | Where the page lives *now*. Equal to `URL` unless state.gov has since reorganized its paths (e.g. `/releases/office-of-the-spokesperson/` → `/releases/office-of-the-spokesman/`, renamed September 2026). Not hashed, so it can be updated in place when the site moves again. Use `URL` for provenance and citation; use `Canonical URL` to fetch the live page |
| `SOS Word Count` | Whitespace-token count of `SOS Text` |
| `Natural Word Count` | Whitespace-token count of `Natural Text`. Nearly equal counts mean the row is effectively a monologue; a large gap flags dialogue |
| `Row Hash` | SHA-256 of the nine content fields (`Secretary` through `Raw Text`, joined with the ASCII unit separator). Excludes `Row Id` and `Canonical URL`, so it changes if and only if the content changes — a stable citation key across releases |
| `Speakers Present` | Everyone holding the floor in `Natural Text`, in order of first appearance. Named participants in readable form (e.g. `Foreign Minister Lavrov`); anonymous roles in brackets (e.g. `[QUESTION]`, `[MODERATOR]`). Always includes the row's own Secretary |
| `People Mentioned (Natural Text)` | Third parties spoken about, extracted by named-entity recognition (spaCy) over `Natural Text` |
| `People Mentioned (SOS Text)` | The subset of `People Mentioned (Natural Text)` that appears in the Secretary's own utterances |
| `Countries Mentioned` | Place references resolved to canonical country names through a curated gazetteer |
| `Country ISO Codes Mentioned` | ISO 3166-1 alpha-3 codes, position-matched to `Countries Mentioned` (the *i*-th code belongs to the *i*-th name). Entities without a code carry `N/A` (e.g. Kosovo) |

The five list-valued columns (`Speakers Present`, both `People Mentioned` columns, and both `Countries` columns) share one convention: entries are joined by `"; "`, appear in first-appearance order, are deduplicated case-insensitively, and are empty strings when nothing was found.

## Live update pipeline

Every Monday at 06:00 UTC, a GitHub Actions workflow:

1. Downloads the latest corpus release
2. Runs `scripts/rubio_backfill.py` to discover new Rubio speeches on state.gov
3. If new rows are found, runs `scripts/corpus_rubio_update.py` to merge, clean, and verify
4. Publishes a new versioned release only if the verifier passes all checks

The workflow can also be triggered manually via **Actions → Rubio Corpus Update → Run workflow**.

If a release reached GitHub but the Hugging Face upload failed, run **Actions → Publish to Hugging Face → Run workflow**. It re-publishes an existing release (the latest by default) without scraping.

The `sos-speeches` Python package is released separately from the data: it always fetches the latest corpus release, so it only needs a new version when the loader itself changes. To release it, bump `version` in `pyproject.toml` and `__version__` in `sos_speeches/__init__.py`, then push a matching tag (e.g. `git tag py-v0.1.1 && git push origin py-v0.1.1`). The **Publish to PyPI** workflow builds and uploads it via PyPI Trusted Publishing.

The **PyPI package**, **R package** and **Hugging Face dataset** badges above come from check workflows that run every Monday at 12:00 UTC, after the update. Each one installs or downloads the data the way a user would and validates it: the PyPI check runs `sos_speeches.load()`, the R check runs `remotes::install_github()` + `sos_load()` + the package tests, and the Hugging Face check confirms the mirror matches the latest GitHub release row for row.

## Verification

Each release passes 85 checks in `scripts/corpus_verify_research_grade.py` (A through Z):

```bash
python scripts/corpus_verify_research_grade.py --input corpus_v*.csv --max-gap-days 90
```

## Manual Rubio update

```bash
# Pipeline dependencies (scraping, enrichment, verification, publishing).
# The offline STM / analysis stack is deliberately separate -- it pins
# numpy<2, which is incompatible with the parquet engine used to publish.
pip install -r scripts/requirements.txt
# For the topic-modeling and stance scripts only:
#   pip install -r scripts/requirements-analysis.txt

# Discover new speeches (deduplicates against existing corpus)
python scripts/rubio_backfill.py --enriched corpus_v*.csv --output rubio_new.csv

# Merge, clean, verify
python scripts/corpus_rubio_update.py \
    --corpus  corpus_v*.csv \
    --backfill rubio_new.csv \
    --output  corpus_updated.csv
```

## Citation

If you use this corpus in research, please cite:

**BibTeX:**
```bibtex
@dataset{yoel2026sos,
  author={Yoel, Benjamin and Revolinsky, Jay},
  title={Introducing the United States Secretary of State Speech Corpus},
  year={2026},
  howpublished={\url{https://github.com/revolins/sos-corpus}},
  note={Under Review}
}
```

**APA:**
Yoel B., Revolinsky,  J. (2026).Introducing the United States Secretary of State Speech Corpus. Retrieved from https://github.com/revolins/sos-corpus

---

*Note: The underlying speeches are works of the U.S. Government and are in the public domain. Please cite this curated corpus when reusing the dataset.*

## License

- **Code** (Python scripts, pipeline, package): [MIT License](https://github.com/revolins/sos-corpus/blob/main/LICENSE)
- **Dataset** (corpus CSVs and Parquet mirrors): [CC0 1.0 Universal](https://github.com/revolins/sos-corpus/blob/main/DATA_LICENSE) — public domain dedication

The underlying content of the corpus (speeches and remarks by U.S. Secretaries of State) are works of the United States Government and are in the public domain.

## Contact

Benjamin Yoel - Postdoctoral Fellow, CDDRL, Stanford University (PhD, Michigan State University): benyoel@stanford.edu

Jay Revolinsky - Computer Science PhD Student, Michigan State University: revolins@msu.edu
