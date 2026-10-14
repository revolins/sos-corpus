test_that("sos_load returns the documented schema", {
  skip_if(length(list.files(system.file("extdata", package = "sosspeech"),
                            pattern = "\\.rds$")) == 0L,
          "corpus shards not built")
  x <- sos_load()
  expect_s3_class(x, "data.frame")
  expect_true(all(c("row_id", "secretary", "date", "title", "speech_type",
                    "location", "url", "sos_text", "natural_text", "raw_text")
                  %in% names(x)))
  expect_s3_class(x$date, "Date")
  expect_false(anyDuplicated(x$row_id) > 0)
})
