# Local fetch

Since about 2026-10-26, state.gov's firewall has refused automated clients from
GitHub's servers and the lab server. While that lasts, a maintainer can run the weekly
Rubio fetch from their own computer. The script records what state.gov sent and opens a
pull request. CI checks that pull request, and merging it publishes the release as
usual.

## What it does and doesn't do

- **Same command as the weekly job.** It runs the weekly job's scrape command
  (`rubio_backfill.ci_args()`) with the crawler's own User-Agent,
  `sos-corpus-research/1.0 (+https://github.com/revolins/sos-corpus)`. It doesn't
  pretend to be a browser.
- **Follows robots.txt.** It never requests a disallowed URL, and it honours any
  `Crawl-delay`.
- **Waits between requests.** The default is 5 seconds, and it won't go below 1. If
  state.gov answers 429 or 503 with `Retry-After`, it waits that long (up to 2 minutes)
  before trying again.
- **Fetches only what's new.** It covers the dates from 7 days before the newest speech
  in the corpus up to today. It starts earlier only when the last complete run started
  earlier, so a missed or refused week leaves no hole. Inside that range it reads the
  remarks listing, plus only the sitemaps and sitemap entries changed since the range
  began. It fetches only pages not already in the corpus, plus any queued URLs.
  That's usually a few dozen requests. The first run after an outage reaches further
  back.
- **Runs at most once every 6 days.** That counts bundles already on `main` and unmerged
  `local-fetch/*` branches. `--force` overrides it.
- **Stops at the first refusal.** On a 401/403/405/407 from state.gov it stops, commits
  nothing and exits 3. Don't work around a refusal from another network, a VPN or a
  proxy: a refusal means stop.

## One-time setup

You need git, the [GitHub CLI](https://cli.github.com/) and Python **3.11**.

```sh
git clone https://github.com/revolins/sos-corpus && cd sos-corpus
gh auth login                                     # an account with write access
python3.11 -m venv local_fetch/work/venv          # local_fetch/work/ is gitignored
local_fetch/work/venv/bin/pip install -r local_fetch/requirements.txt
```

On Windows the venv's Python is `local_fetch\work\venv\Scripts\python.exe`.

The environment must match `requirements.txt` exactly. CI replays your fetch with the
same packages, and a different parser version could change a row. The script checks
this and stops if anything differs, including if lxml is installed.

## Each week

```sh
git checkout main && git pull --ff-only
local_fetch/work/venv/bin/python local_fetch/local_fetch.py
```

The script:
1. checks your setup;
2. downloads the latest release's corpus (about 0.5 GB the first time, then cached in
   `local_fetch/work/`) and run state;
3. fetches;
4. writes the bundle;
5. runs the alignment tests;
6. commits the bundle to a new branch `local-fetch/<date>`, pushes it and opens a PR.

Use `--no-push` to stop after the local commit and look at it first.

**Scheduling it.** It isn't installed for you. Here are examples for Mondays at 08:00:

- **macOS / Linux** (`crontab -e`):
  ```
  0 8 * * 1  cd /path/to/sos-corpus && git checkout -q main && git pull -q --ff-only && local_fetch/work/venv/bin/python local_fetch/local_fetch.py >> local_fetch/work/cron.log 2>&1
  ```
- **Windows** (Task Scheduler, from cmd):
  ```
  schtasks /Create /SC WEEKLY /D MON /ST 08:00 /TN "SOS local fetch" /TR "cmd /c cd /d C:\path\to\sos-corpus && git checkout -q main && git pull -q --ff-only && local_fetch\work\venv\Scripts\python.exe local_fetch\local_fetch.py >> local_fetch\work\cron.log 2>&1"
  ```

The computer must be on and online at that time. A missed week is caught up by the
next run.

## When state.gov refuses the crawler: pages saved by hand

If state.gov refuses the crawler (exit 3), a person can still read the pages in a
browser. Save each new Rubio page (File > Save Page As; "Webpage, Complete" or "HTML
only") into one folder, then run:

```sh
local_fetch/work/venv/bin/python local_fetch/local_fetch.py --offline-pages path/to/folder
```

- **No request is sent.** The run reads only the saved files, matched to their
  canonical URL (`<link rel="canonical">`, else `og:url`, else the browser's "saved
  from" comment). Files without a state.gov URL are ignored and named in the log.
- **Same parsing.** The unchanged scraper parses each page, and the bundle records
  the saved HTML in `http/`, so CI's replay checks it the same way. A browser saves
  the page after its scripts ran. Even so, 16 pages already in the corpus, rebuilt
  from saved copies through the merge, matched the live-scraped rows in every column
  except Raw Text (and so Row Hash). Raw Text differed only where state.gov had since
  changed the page itself: a footer link and one job title.
- **Labelled honestly.** The bundle's mode is `offline`, and `manifest.json` and
  `http/meta.json` say `source: offline_pages` and record no User-Agent. Its inputs
  add `rubio_offline_urls.txt`, the saved pages' URLs (no file names, no paths). The
  PR and the release note say the rows came from saved pages.
- **Discovers nothing.** Only pages you saved are read. Pending URLs you didn't save
  stay pending, and the discovery floor doesn't move, so the next live run still
  walks the whole gap. A page you missed is missing until then.
- Saved pages are only valid for the latest release: save, then run, before the next
  release.

## What gets committed

`local_fetch/bundles/<YYYY-MM-DD>/` contains:

| Path | Contents |
|---|---|
| `manifest.json` | base release and corpus sha256, commit, mode, arguments, parsing environment, sha256 of every file |
| `inputs/` | the run's starting state (`rubio_pending.csv`, `rubio_held.csv`, `rubio_discovery_state.json`, `rubio_gt_missing.txt`) and the two checked-in seed files; for an offline bundle, also `rubio_offline_urls.txt` |
| `http/` | every request and response, in order (`requests.jsonl` plus gzipped bodies), with the run's id, date, User-Agent and request counts (`meta.json`) |
| `outputs/` | what the scraper wrote: `rubio_new.csv`, the updated state, `rubio_skipped.csv`, `rubio_listing_snapshot.csv`, the summary |

A bundle doesn't include your machine's name, IP address, username or local paths.
The tests check this.

## How a bundle is checked

`local_fetch/tests/` runs on your computer before the commit and again in CI on the PR
(see `bundle.py`). A bundle is accepted only if:

- **It's current.** Its base is still the latest release, and its inputs match that
  release's state and the seed files on `main`.
- **Its code is on `main`.** The commit it ran with is on `main`.
- **It ran the weekly command.** It used the weekly job's arguments and the honest
  User-Agent, and its parsing environment matches. An offline bundle must instead say
  `source: offline_pages` and claim no User-Agent.
- **The replay matches.** Re-running the same command offline over `http/` reproduces
  every file in `outputs/` byte for byte. That shows the rows came from what state.gov
  sent and from the committed code, not from a hand edit.
- **State only moves forward.** The discovery floor doesn't move back, and no pending
  URL vanishes without a row, a skip reason or a corpus match.

On the PR, CI also runs a dry-run merge and the verifier, with the same checks as a
normal weekly release. Merging the PR re-checks the bundle against the then-latest
release, then builds, verifies and publishes it (GitHub release and Hugging Face).

If `main` gets a new release or new seed files first, the bundle is stale and is
rejected. Run the script again with `--force`.
