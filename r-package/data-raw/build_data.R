# Builds compressed shards in inst/extdata/ from the raw corpus, sized to fit
# in plain git (GitHub rejects files >100 MB; shards are kept under max_mb).
# Run from the package root: source("data-raw/build_data.R")

raw_path <- "/Users/benyoel/Dropbox/SOS_Speech_Dataset/corpus_v2026.10.12.csv"
out_dir  <- "inst/extdata"
max_mb   <- 50

x <- readr::read_csv(raw_path, show_col_types = FALSE)

# new_name = "Original Name"
nm <- c(
  row_id                      = "Row Id",
  secretary                   = "Secretary",
  date                        = "Date",
  title                       = "Title",
  speech_type                 = "Speech Type",
  location                    = "Location",
  url                         = "URL",
  sos_text                    = "SOS Text",
  natural_text                = "Natural Text",
  raw_text                    = "Raw Text",
  sos_word_count              = "SOS Word Count",
  natural_word_count          = "Natural Word Count",
  row_hash                    = "Row Hash",
  speakers_present            = "Speakers Present",
  people_mentioned_natural    = "People Mentioned (Natural Text)",
  people_mentioned_sos        = "People Mentioned (SOS Text)",
  countries_mentioned         = "Countries Mentioned",
  country_iso_codes_mentioned = "Country ISO Codes Mentioned",
  canonical_url               = "Canonical URL"
)
stopifnot(setequal(names(x), nm))
names(x) <- names(nm)[match(names(x), nm)]

x$date <- as.Date(x$date)
stopifnot(!anyDuplicated(x$row_id), !anyNA(x$date))

dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)
unlink(list.files(out_dir, pattern = "^speeches_.*\\.rds$", full.names = TRUE))

write_shard <- function(d, name) {
  path <- file.path(out_dir, paste0(name, ".rds"))
  saveRDS(d, path, compress = "xz")
  if (file.size(path) / 1e6 > max_mb && nrow(d) > 1L) {
    unlink(path)
    half <- ceiling(nrow(d) / 2)
    write_shard(d[seq_len(half), ], paste0(name, "a"))
    write_shard(d[-seq_len(half), ], paste0(name, "b"))
  }
}

yr <- format(x$date, "%Y")
for (y in unique(yr)) write_shard(x[yr == y, ], paste0("speeches_", y))

sizes <- file.size(list.files(out_dir, pattern = "\\.rds$", full.names = TRUE)) / 1e6
message(length(sizes), " shards; total ", round(sum(sizes), 1),
        " MB; largest ", round(max(sizes), 1), " MB")
