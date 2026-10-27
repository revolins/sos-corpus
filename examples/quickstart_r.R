# U.S. Secretary of State Speech Corpus: quick start (R)
#
# A first look at the corpus: who speaks, when, where, how much, about which
# countries and people, and how to follow a word over time. Run it top to
# bottom in RStudio, or `Rscript quickstart_r.R`. Figures are shown in RStudio
# and saved to ./eda_figures/.
#
# Packages:
#   install.packages(c("readr", "dplyr", "tidyr", "ggplot2", "stringr", "forcats", "jsonlite"))
#
# How a row is organised. One row is one public statement by a Secretary of
# State: a speech, remarks, an interview, testimony, a press statement or an
# op-ed. The two text columns differ in who is speaking:
#   - `SOS Text` holds only the Secretary's own words. Use it to study what the
#     Secretary said.
#   - `Natural Text` holds the full transcript, every speaker included.
# The full codebook is a PDF attached to each GitHub release:
#   https://github.com/revolins/sos-corpus/releases

suppressPackageStartupMessages({
  library(readr); library(dplyr); library(tidyr); library(ggplot2)
  library(stringr); library(forcats)
})
dir.create("eda_figures", showWarnings = FALSE)
show_and_save <- function(plot, file, width = 9, height = 4) {
  print(plot)
  ggsave(file.path("eda_figures", file), plot, width = width, height = height)
}

# ---- 1. Load the data ---------------------------------------------------------
# Either point SOS_CORPUS_CSV at a local copy of the CSV, or let this download
# the latest GitHub release once (it is cached for later runs).
#
# The sosspeech R package (r-package/) currently bundles an older snapshot of
# the corpus, so this script reads the latest release directly.
csv_path <- Sys.getenv("SOS_CORPUS_CSV")
if (csv_path == "") {
  release <- jsonlite::fromJSON("https://api.github.com/repos/revolins/sos-corpus/releases/latest")
  asset <- release$assets[grepl("^corpus_.*\\.csv$", release$assets$name), ]
  cache <- tools::R_user_dir("sosspeech", which = "cache")
  dir.create(cache, recursive = TRUE, showWarnings = FALSE)
  csv_path <- file.path(cache, asset$name[1])
  if (!file.exists(csv_path)) {
    options(timeout = 3600)                       # the CSV is about 480 MB
    message("Downloading ", asset$name[1], " from ", release$tag_name, " ...")
    download.file(asset$browser_download_url[1], csv_path, mode = "wb")
  }
}

# Raw Text (the full scraped page) is large and rarely needed for analysis, so
# it is left out here. Remove the col_select argument to keep it.
x <- read_csv(csv_path, col_types = cols(.default = col_character()),
              col_select = -`Raw Text`, na = character(), progress = FALSE)
x <- x |>
  mutate(Date = as.Date(Date), Year = as.integer(format(Date, "%Y")),
         across(c(`SOS Word Count`, `Natural Word Count`), as.integer))
cat(sprintf("%s rows, %d columns, %s to %s (%s)\n", format(nrow(x), big.mark = ","),
            ncol(x), min(x$Date), max(x$Date), basename(csv_path)))

# Secretaries in order of tenure. The corpus also keeps one row for Warren
# Christopher (his farewell testimony, 1997-01-08); it is left out below.
ORDER <- c("Albright", "Powell", "Rice", "Clinton", "Kerry", "Tillerson",
           "Pompeo", "Blinken", "Rubio")
d <- x |> filter(Secretary %in% ORDER) |>
  mutate(Secretary = factor(Secretary, levels = ORDER))

# One row, in full
set.seed(7)
row <- d |> slice_sample(n = 1)
print(t(row |> select(Secretary, Date, Title, `Speech Type`, Location, URL, `Speakers Present`)))
cat("\nSOS Text (first 600 characters):\n", substr(row$`SOS Text`, 1, 600), "\n")

# ---- 2. Who and when ----------------------------------------------------------
print(count(d, Secretary))
show_and_save(
  ggplot(count(d, Year), aes(Year, n)) + geom_col(fill = "#3b6ea5") +
    labs(title = "Statements per year", x = NULL, y = "rows") + theme_minimal(),
  "statements_per_year.png")

# What kind of statement each Secretary gives (share of their rows, %)
type_share <- d |> count(Secretary, `Speech Type`) |>
  group_by(Secretary) |> mutate(pct = round(100 * n / sum(n), 1)) |> ungroup() |>
  select(-n) |> pivot_wider(names_from = `Speech Type`, values_from = pct, values_fill = 0)
print(type_share, width = Inf)

# ---- 3. How much the Secretary says --------------------------------------------
# `SOS Word Count` counts the Secretary's own words; `Natural Word Count` the
# whole transcript. Their ratio shows how much of an event is the Secretary
# talking: close to 1 for a speech, much lower for a press conference.
print(d |> group_by(Secretary) |>
        summarise(statements = n(), words = sum(`SOS Word Count`),
                  median_words = median(`SOS Word Count`)))
show_and_save(
  ggplot(d, aes(Secretary, `SOS Word Count`)) +
    geom_boxplot(outlier.shape = NA, fill = "#dbe6f2") + scale_y_log10() +
    labs(title = "Secretary's own words per statement", x = NULL, y = "words (log scale)") +
    theme_minimal(),
  "words_per_statement.png")
print(d |> mutate(share = pmin(`SOS Word Count` / pmax(`Natural Word Count`, 1), 1)) |>
        group_by(`Speech Type`) |> summarise(median_share_spoken_by_secretary = median(share)) |>
        arrange(median_share_spoken_by_secretary))

# ---- 4. Where -----------------------------------------------------------------
# `Location` is always "City, Region"; written statements issued by the
# department carry "Washington, D.C.".
print(d |> count(Location, sort = TRUE) |> head(15))
print(d |> group_by(Secretary) |>
        summarise(pct_outside_washington = round(100 * mean(Location != "Washington, D.C."), 1)))

# ---- 5. Which countries and people come up -------------------------------------
# Four columns hold lists, joined by "; ": Speakers Present, People Mentioned
# (Natural Text), People Mentioned (SOS Text) and Countries Mentioned. Country
# ISO Codes Mentioned holds the matching codes in the same order. People
# Mentioned comes from automatic named-entity recognition; its frequent values
# are reviewed, but treat the long tail as approximate.
explode <- function(frame, column) {
  frame |> select(`Row Id`, Secretary, Year, value = all_of(column)) |>
    separate_longer_delim(value, delim = "; ") |> filter(value != "")
}
countries <- explode(d, "Countries Mentioned")
print(countries |> count(value, sort = TRUE) |> head(15))
# The five countries each Secretary mentions in the most statements
# (the United States itself left out)
print(countries |> filter(value != "United States") |> count(Secretary, value) |>
        group_by(Secretary) |> slice_max(n, n = 5, with_ties = FALSE) |>
        summarise(most_mentioned = paste(value, collapse = ", ")), width = Inf)
print(explode(d, "People Mentioned (SOS Text)") |> count(value, sort = TRUE) |> head(20))

# ---- 6. Following a word over time ----------------------------------------------
# Mentions per 10,000 of the Secretary's own words, by year; normalising by
# words matters because the number of statements changes a lot from year to
# year. Edit TERMS to follow anything.
TERMS <- c(China = "\\bchin(a|ese)\\b", Russia = "\\bruss(ia|ian)\\b",
           Iran = "\\biran(ian)?\\b", climate = "\\bclimate\\b")
text <- str_to_lower(d$`SOS Text`)
rates <- bind_rows(lapply(names(TERMS), function(term) {
  tibble(Year = d$Year, words = d$`SOS Word Count`, hits = str_count(text, TERMS[[term]])) |>
    group_by(Year) |> summarise(rate = 1e4 * sum(hits) / sum(words)) |> mutate(term = term)
}))
show_and_save(
  ggplot(rates, aes(Year, rate, colour = term)) + geom_line() + geom_point(size = 0.8) +
    labs(title = "Terms in the Secretary's own words", x = NULL,
         y = "mentions per 10,000 words", colour = NULL) + theme_minimal(),
  "terms_over_time.png")

# Keyword in context: each match shown with the words around it
kwic <- function(pattern, secretary = NULL, width = 70, n = 8) {
  rows <- if (is.null(secretary)) d else filter(d, Secretary == secretary)
  rows <- arrange(rows, Date)
  shown <- 0
  for (i in seq_len(nrow(rows))) {
    s <- rows$`SOS Text`[i]
    m <- str_locate_all(s, regex(pattern, ignore_case = TRUE))[[1]]
    for (j in seq_len(nrow(m))) {
      left  <- str_replace_all(substr(s, max(1, m[j, 1] - width), m[j, 1] - 1), "\n", " ")
      right <- str_replace_all(substr(s, m[j, 2] + 1, m[j, 2] + width), "\n", " ")
      cat(sprintf("%s %-9s ...%s[%s]%s...\n", rows$Date[i], rows$Secretary[i], left,
                  substr(s, m[j, 1], m[j, 2]), right))
      shown <- shown + 1
      if (shown >= n) return(invisible(NULL))
    }
  }
}
kwic("\\bArctic\\b", n = 6)

# ---- 7. Before you analyse -----------------------------------------------------
# - Cite rows by `Row Hash`. It changes only when the content changes; `Row Id`
#   is renumbered when rows are added or removed between releases.
# - Use `URL` for provenance and `Canonical URL` to open the page today
#   (state.gov has moved some paths).
# - `SOS Text` has speaker labels and stage directions such as "(Applause.)"
#   removed; interviewers' questions are in `Natural Text` only.
# - `Date` is the publication date; a statement released the evening after an
#   event may be dated a day later.
# - New Rubio statements are added every Monday as a new versioned release.
#   Note which release you used (the file name, e.g. corpus_v2026.10.27.csv).
