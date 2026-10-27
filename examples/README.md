# Quick-start examples

Two starter scripts load the latest corpus release and take a first look at the data. They contain the same steps; use whichever language you work in.

| File | Language | Run with |
|---|---|---|
| [`quickstart_python.ipynb`](quickstart_python.ipynb) | Python | Jupyter, VS Code or Colab. Needs `pip install sos-speeches pandas matplotlib` |
| [`quickstart_r.R`](quickstart_r.R) | R | RStudio, or `Rscript quickstart_r.R`. Needs `install.packages(c("readr", "dplyr", "tidyr", "ggplot2", "stringr", "forcats", "jsonlite"))` |

Each one:
1. downloads the latest release CSV once and caches it, or reads a local copy you point `SOS_CORPUS_CSV` at;
2. shows what a row holds, and how `SOS Text` (the Secretary's own words) differs from `Natural Text` (the full transcript);
3. counts statements by Secretary, year and Speech Type;
4. measures how much each Secretary says, and where statements were given;
5. lists the countries and people mentioned, from the list columns;
6. follows terms over time per 10,000 words, with a keyword-in-context helper;
7. ends with notes to read before analysing: citing rows by `Row Hash`, `URL` against `Canonical URL`, and what dates mean.

On a laptop, each runs in under 30 seconds once the data is downloaded; the CSV is about 480 MB. Both skip the large `Raw Text` column; the code shows how to keep it.

The R script reads the release directly, because the `sosspeech` package in `r-package/` bundles an older snapshot of the corpus.
