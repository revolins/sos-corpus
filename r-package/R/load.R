#' Load the US Secretary of State speech corpus
#'
#' The corpus is stored in the package as compressed `.rds` shards (one per
#' year, split further if a year is large) under `inst/extdata`. This function
#' reads and row-binds them.
#'
#' @param years Optional integer vector of years to load. Default loads all.
#'
#' @return A data frame with one row per speech and columns `row_id`,
#'   `secretary`, `date` (`Date`), `title`, `speech_type`, `location`, `url`,
#'   `sos_text`, `natural_text`, `raw_text`, `sos_word_count`,
#'   `natural_word_count`, `row_hash`, `speakers_present`,
#'   `people_mentioned_natural`, `people_mentioned_sos`,
#'   `countries_mentioned`, `country_iso_codes_mentioned`, `canonical_url`.
#' @export
#' @examples
#' \dontrun{
#' x <- sos_load()
#' x_2010 <- sos_load(years = 2010)
#' }
sos_load <- function(years = NULL) {
  dir <- system.file("extdata", package = "sosspeech")
  files <- list.files(dir, pattern = "^speeches_[0-9]{4}.*\\.rds$",
                      full.names = TRUE)
  if (length(files) == 0L) {
    stop("No corpus files found. Run data-raw/build_data.R first.", call. = FALSE)
  }
  file_year <- as.integer(sub("^speeches_([0-9]{4}).*$", "\\1", basename(files)))
  if (!is.null(years)) {
    files <- files[file_year %in% years]
    if (length(files) == 0L) {
      stop("No corpus files for the requested years.", call. = FALSE)
    }
  }
  out <- do.call(rbind, lapply(files, readRDS))
  out[order(out$date, out$row_id), , drop = FALSE]
}
