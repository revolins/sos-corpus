fixture <- test_path("fixtures", "mini_corpus.csv")

test_that("sos_load reads a release CSV into the documented schema", {
  x <- sos_load(path = fixture)
  expect_s3_class(x, "data.frame")
  expect_identical(names(x), c(
    "row_id", "secretary", "date", "title", "speech_type", "location", "url",
    "sos_text", "natural_text", "raw_text", "sos_word_count", "natural_word_count",
    "row_hash", "speakers_present", "people_mentioned_natural", "people_mentioned_sos",
    "countries_mentioned", "country_iso_codes_mentioned", "canonical_url"))
  expect_s3_class(x$date, "Date")
  expect_type(x$row_id, "double")
  expect_type(x$sos_word_count, "double")
  expect_true(is.na(x$people_mentioned_natural[x$secretary == "Albright"]))
  expect_match(x$sos_text[x$secretary == "Albright"], "\n")   # embedded newline survives
  expect_identical(attr(x, "release"), "mini_corpus.csv")
})

test_that("sos_load filters by year", {
  expect_identical(sos_load(years = 2025, path = fixture)$secretary, "Rubio")
  expect_error(sos_load(years = 1990, path = fixture), "No speeches")
})

test_that("sos_load rejects a file that is not a corpus CSV", {
  bad <- tempfile(fileext = ".csv")
  writeLines("a,b\n1,2", bad)
  expect_error(sos_load(path = bad), "missing columns")
})

test_that("sos_load uses a cached release when GitHub cannot be reached", {
  cache <- tempfile("cache")
  dir.create(cache)
  saveRDS(sos_load(path = fixture), file.path(cache, "speeches_v2099.01.01.rds"))
  local_mocked_bindings(sos_release_info = function(version) stop("offline"))
  expect_message(x <- sos_load(cache_dir = cache), "using cached speeches_v2099.01.01.rds")
  expect_equal(nrow(x), 2L)
})
