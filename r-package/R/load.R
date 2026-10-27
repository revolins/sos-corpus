#' Load the US Secretary of State speech corpus
#'
#' Downloads a corpus release from GitHub (by default the latest, which the
#' weekly update keeps current) and returns it as a data frame. The first call
#' for a release downloads the CSV (about 480 MB) and caches it as a compressed
#' `.rds` file; later calls read the cache. Without a network connection the
#' newest cached release is used.
#'
#' @param years Optional integer vector of years to keep. Default keeps all.
#' @param version Release tag, e.g. `"v2026.10.27"`, or `"latest"`.
#' @param path Optional path to a local corpus CSV; when given, nothing is
#'   downloaded or cached.
#' @param cache_dir Directory for cached releases. Defaults to
#'   `tools::R_user_dir("sosspeech", "cache")`.
#' @param refresh If `TRUE`, download the release again even if it is cached.
#'
#' @return A data frame with one row per speech and columns `row_id`,
#'   `secretary`, `date` (`Date`), `title`, `speech_type`, `location`, `url`,
#'   `sos_text`, `natural_text`, `raw_text`, `sos_word_count`,
#'   `natural_word_count`, `row_hash`, `speakers_present`,
#'   `people_mentioned_natural`, `people_mentioned_sos`,
#'   `countries_mentioned`, `country_iso_codes_mentioned`, `canonical_url`.
#'   The release tag is stored in `attr(x, "release")`.
#' @export
#' @examples
#' \dontrun{
#' x <- sos_load()
#' x_2010 <- sos_load(years = 2010)
#' x_fixed <- sos_load(version = "v2026.10.27")   # a fixed release, for replication
#' }
sos_load <- function(years = NULL, version = "latest", path = NULL,
                     cache_dir = NULL, refresh = FALSE) {
  if (!is.null(path)) {
    out <- sos_read_csv(path)
    attr(out, "release") <- basename(path)
  } else {
    if (is.null(cache_dir)) cache_dir <- tools::R_user_dir("sosspeech", which = "cache")
    dir.create(cache_dir, recursive = TRUE, showWarnings = FALSE)
    out <- sos_cached_release(version, cache_dir, refresh)
  }
  if (!is.null(years)) {
    out <- out[as.integer(format(out$date, "%Y")) %in% years, , drop = FALSE]
    if (nrow(out) == 0L) stop("No speeches for the requested years.", call. = FALSE)
  }
  out[order(out$date, out$row_id), , drop = FALSE]
}

# Column names: CSV header -> R name.
sos_columns <- c(
  "Row Id" = "row_id", "Secretary" = "secretary", "Date" = "date",
  "Title" = "title", "Speech Type" = "speech_type", "Location" = "location",
  "URL" = "url", "SOS Text" = "sos_text", "Natural Text" = "natural_text",
  "Raw Text" = "raw_text", "SOS Word Count" = "sos_word_count",
  "Natural Word Count" = "natural_word_count", "Row Hash" = "row_hash",
  "Speakers Present" = "speakers_present",
  "People Mentioned (Natural Text)" = "people_mentioned_natural",
  "People Mentioned (SOS Text)" = "people_mentioned_sos",
  "Countries Mentioned" = "countries_mentioned",
  "Country ISO Codes Mentioned" = "country_iso_codes_mentioned",
  "Canonical URL" = "canonical_url"
)

# Read a release CSV into the package's data frame (snake_case names, Date,
# numeric ids and counts, NA for empty fields).
sos_read_csv <- function(path) {
  if (requireNamespace("readr", quietly = TRUE)) {
    x <- readr::read_csv(path, col_types = readr::cols(.default = readr::col_character()),
                         na = "", progress = FALSE)
    x <- as.data.frame(x)
  } else {
    x <- utils::read.csv(path, colClasses = "character", na.strings = "",
                         check.names = FALSE, encoding = "UTF-8")
  }
  missing <- setdiff(names(sos_columns), names(x))
  if (length(missing)) stop("Not a corpus CSV; missing columns: ",
                            paste(missing, collapse = ", "), call. = FALSE)
  x <- x[, names(sos_columns)]
  names(x) <- unname(sos_columns)
  x$date <- as.Date(x$date)
  for (col in c("row_id", "sos_word_count", "natural_word_count")) x[[col]] <- as.numeric(x[[col]])
  x
}

# The release as a data frame, from the cache when possible.
sos_cached_release <- function(version, cache_dir, refresh) {
  release <- tryCatch(sos_release_info(version), error = function(e) e)
  if (inherits(release, "error")) {
    cached <- sos_cached_files(cache_dir, version)
    if (!length(cached)) stop("Could not reach GitHub (", conditionMessage(release),
                              ") and no release is cached.", call. = FALSE)
    message("Could not reach GitHub; using cached ", basename(cached[1]))
    out <- readRDS(cached[1])
    return(out)
  }
  rds <- file.path(cache_dir, paste0("speeches_", release$tag, ".rds"))
  if (file.exists(rds) && !refresh) return(readRDS(rds))
  csv <- file.path(cache_dir, release$asset_name)
  old_timeout <- getOption("timeout")
  on.exit(options(timeout = old_timeout), add = TRUE)
  options(timeout = max(3600, old_timeout))
  message("Downloading ", release$asset_name, " (", release$tag, "); this happens once per release ...")
  utils::download.file(release$asset_url, csv, mode = "wb", quiet = TRUE)
  out <- sos_read_csv(csv)
  attr(out, "release") <- release$tag
  saveRDS(out, rds, compress = "xz")
  unlink(csv)
  out
}

sos_cached_files <- function(cache_dir, version) {
  pattern <- if (identical(version, "latest")) "^speeches_v.*\\.rds$" else
    paste0("^speeches_", gsub(".", "\\.", version, fixed = TRUE), "\\.rds$")
  files <- list.files(cache_dir, pattern = pattern, full.names = TRUE)
  files[order(basename(files), decreasing = TRUE)]
}

# Tag and CSV asset of a GitHub release.
sos_release_info <- function(version) {
  api <- "https://api.github.com/repos/revolins/sos-corpus/releases/"
  url <- if (identical(version, "latest")) paste0(api, "latest") else paste0(api, "tags/", version)
  rel <- jsonlite::fromJSON(url)
  assets <- rel$assets
  i <- which(grepl("^corpus_.*\\.csv$", assets$name))
  if (!length(i)) stop("Release ", rel$tag_name, " has no corpus CSV.", call. = FALSE)
  list(tag = rel$tag_name, asset_name = assets$name[i[1]],
       asset_url = assets$browser_download_url[i[1]])
}
