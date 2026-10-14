# sosspeech

US Secretary of State speech corpus as an R data package. The full corpus lives in this repo as compressed shards in `inst/extdata/` (plain git, no LFS or external hosting).

## Install

```r
# install.packages("pak")
pak::pak("GITHUB_USER/sosspeech")
```

## Use

```r
library(sosspeech)
x <- sos_load()               # full corpus
x <- sos_load(years = 2010)   # one or more years
```

## Citation

Yoel, Benjamin, and Jay Revolinsky. 2026. "Introducing the US Secretary of State Speech Corpus." ArXiv.
