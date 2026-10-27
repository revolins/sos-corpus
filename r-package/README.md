# sosspeech

US Secretary of State speech corpus for R. `sos_load()` downloads a corpus release from GitHub, by default the latest one, which the weekly update keeps current. It caches the release locally, so only the first call for a release downloads (about 480 MB).

## Install

```r
install.packages("remotes")
remotes::install_github("revolins/sos-corpus", subdir = "r-package")
```

## Use

```r
library(sosspeech)
x <- sos_load()                            # latest release, all years
x <- sos_load(years = 2010)                # one or more years
x <- sos_load(version = "v2026.10.27")     # a fixed release, for replication
attr(x, "release")                         # which release you loaded
x <- sos_load(path = "corpus_v2026.10.27.csv")   # a CSV you already have
```

Without a network connection, `sos_load()` uses the newest release in its cache. `readr`, if installed, makes the first load faster.

## Citation

Yoel, Benjamin, and Jay Revolinsky. 2026. "Introducing the US Secretary of State Speech Corpus." ArXiv.
