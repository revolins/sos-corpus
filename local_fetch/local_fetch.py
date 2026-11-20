#!/usr/bin/env python3
"""Run the weekly Rubio fetch from your own computer and open a PR with the result.

state.gov refuses GitHub's runners and the lab server (since about 2026-10-26). This
runs the weekly job's own scrape command, unchanged, from a maintainer's machine:
  * the crawler's honest User-Agent (sos-corpus-research/1.0 +repo URL);
  * robots.txt obeyed; at least --rate-limit seconds between requests (default 2);
  * it stops at the first refusal (exit 3) and commits nothing;
  * no browser impersonation, proxies or retries from elsewhere: a refusal is the answer.

Every HTTP exchange is recorded. Before anything is committed, the alignment tests
(local_fetch/tests) replay the recording offline and require identical output; CI
runs them again on the PR. Merging the PR builds, verifies and publishes the release.

Steps: preflight -> download the latest release's corpus and run state -> fetch ->
bundle -> alignment tests -> commit local_fetch/bundles/<date>/ to branch
local-fetch/<date> -> push -> open a PR.

  python local_fetch/local_fetch.py              # the weekly run
  python local_fetch/local_fetch.py --no-push    # commit locally only, to inspect first

Needs: git, the GitHub CLI (`gh auth login`, write access), and
`pip install -r local_fetch/requirements.txt`. Exit codes: 0 done, 3 refused by
state.gov (nothing committed), 1 anything else (nothing committed).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bundle as B  # noqa: E402

REPO = B.REPO
WORK = HERE / 'work'              # gitignored: corpus cache, run directories
EXIT_REFUSED = 3
GUARD_DAYS = 6


class Stop(Exception):
    """Preflight or a step failed: explain and exit without committing."""


def sh(*cmd, check=True, capture=True, cwd=REPO) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=capture, text=True)
    if check and r.returncode != 0:
        raise Stop(f'`{" ".join(cmd)}` failed ({r.returncode}): '
                   f'{(r.stderr or r.stdout or "").strip()[:500]}')
    return (r.stdout or '').strip()


def log(msg: str) -> None:
    print(f'[local_fetch] {msg}', flush=True)


# --- preflight --------------------------------------------------------------

def _dates(names) -> list[date]:
    out = []
    for n in names:
        try:
            out.append(date.fromisoformat(n.rstrip('/').rsplit('/', 1)[-1][:10]))
        except ValueError:
            continue
    return out


def newest_bundle_date(root: Path = B.BUNDLES, branches: list[str] = ()) -> date | None:
    """Newest bundle on main, or on a local-fetch/<date> branch (an unmerged PR)."""
    days = _dates(p.name for p in Path(root).glob('*')) + _dates(branches)
    return max(days) if days else None


def fetch_branches() -> list[str]:
    local = sh('git', 'branch', '--list', 'local-fetch/*', '--format=%(refname:short)', check=False)
    remote = sh('git', 'ls-remote', '--heads', 'origin', 'local-fetch/*', check=False)
    return local.split() + [l.split()[-1] for l in remote.splitlines() if l.strip()]


def guard_once_a_week(today: date, force: bool, root: Path = B.BUNDLES,
                      branches: list[str] = ()) -> None:
    last = newest_bundle_date(root, branches)
    if last is not None and (today - last).days < GUARD_DAYS and not force:
        raise Stop(f'a bundle from {last} already exists; the fetch runs at most once '
                   f'every {GUARD_DAYS} days (pass --force to override, e.g. after a '
                   f'failed import).')


def preflight(today: date, force: bool) -> str:
    env = B.env_problems()
    if env:
        raise Stop('this Python environment is not the pinned one CI replays with:\n  '
                   + '\n  '.join(env) + '\nSet it up as local_fetch/README.md describes.')
    sh('gh', 'auth', 'status')
    if sh('git', 'branch', '--show-current') != 'main':
        raise Stop('check out main first (git checkout main)')
    dirty = [l for l in sh('git', 'status', '--porcelain').splitlines() if l.strip()]
    if dirty:
        raise Stop('the working tree has uncommitted changes:\n  ' + '\n  '.join(dirty[:10]))
    sh('git', 'fetch', '-q', 'origin', 'main')
    head, origin = sh('git', 'rev-parse', 'HEAD'), sh('git', 'rev-parse', 'origin/main')
    if head != origin:
        raise Stop('main is not up to date with origin/main (git pull --ff-only)')
    guard_once_a_week(today, force, branches=fetch_branches())
    return head


# --- staging ----------------------------------------------------------------

def stage_release(release: Path) -> str:
    """Download what the weekly job downloads, into release/ (never modified after)."""
    if release.exists():
        shutil.rmtree(release)
    release.mkdir(parents=True)
    tag = sh('gh', 'release', 'view', '--json', 'tagName', '-q', '.tagName')
    cache = WORK / 'cache' / tag
    corpus = cache / 'corpus.csv'
    if not corpus.exists():
        log(f'downloading the {tag} corpus (about 0.5 GB, cached for later runs) ...')
        cache.mkdir(parents=True, exist_ok=True)
        sh('gh', 'release', 'download', tag, '--pattern', 'corpus_v*.csv', '--dir', str(cache),
           '--clobber')
        got = sorted(cache.glob('corpus_v*.csv'))
        if len(got) != 1:
            raise Stop(f'expected one corpus_v*.csv on {tag}, found {len(got)}')
        got[0].rename(corpus)
    shutil.copyfile(corpus, release / 'corpus.csv')
    for name in ('rubio_pending.csv', 'rubio_held.csv', 'rubio_discovery_state.json'):
        r = subprocess.run(['gh', 'release', 'download', tag, '--pattern', name, '--dir',
                            str(release), '--clobber'], cwd=REPO, capture_output=True, text=True)
        log(f'{name}: {"restored from " + tag if r.returncode == 0 else "not on the release"}')
    for name in B.CHECKED_IN:
        shutil.copyfile(REPO / 'data' / name, release / name)
    # Same ground-truth gap list the weekly job derives before scraping.
    gts = json.loads(sh('gh', 'release', 'list', '--limit', '100', '--json',
                        'tagName,createdAt') or '[]')
    gts = sorted((g for g in gts if g['tagName'].startswith('gt-')), key=lambda g: g['createdAt'])
    if gts:
        gt = gts[-1]['tagName']
        tmp = WORK / 'gt'
        shutil.rmtree(tmp, ignore_errors=True)
        sh('gh', 'release', 'download', gt, '--pattern', 'ground_truth.csv', '--dir', str(tmp))
        args = [sys.executable, str(B.SCRIPTS / 'rubio_coverage_reconcile.py'),
                '--corpus', str(release / 'corpus.csv'), '--ground-truth', str(tmp / 'ground_truth.csv'),
                '--exclusions', str(release / 'rubio_coverage_exclusions.csv'), '--items-only',
                '--missing-urls-out', str(release / 'rubio_gt_missing.txt'),
                '--out', str(WORK / 'coverage_pre')]
        for opt, name in (('--pending', 'rubio_pending.csv'), ('--held', 'rubio_held.csv')):
            if (release / name).exists():
                args += [opt, str(release / name)]
        subprocess.run(args, cwd=REPO, capture_output=True)
        log(f'ground truth {gt}: gap list {"written" if (release / "rubio_gt_missing.txt").exists() else "empty"}')
    return tag


# --- the fetch --------------------------------------------------------------

def fetch(release: Path, bundle: Path, mode: str, rate_limit: float) -> int:
    run_dir = WORK / 'run'
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True)
    (bundle / 'inputs').mkdir(parents=True)
    for name in B.INPUTS:
        if (release / name).exists():
            shutil.copyfile(release / name, bundle / 'inputs' / name)
            shutil.copyfile(release / name, run_dir / name)
    B._link_or_copy(release / 'corpus.csv', run_dir / 'corpus.csv')
    log(f'fetching ({mode}; at least {rate_limit:g}s between requests) ...')
    rc = B.run_backfill(run_dir, mode, ['--rate-limit', f'{rate_limit:g}',
                                        '--record', str(bundle / 'http')])
    if rc == 0:
        (bundle / 'outputs').mkdir()
        for name in B.OUTPUTS + B.OPTIONAL_OUTPUTS:
            if (run_dir / name).exists():
                shutil.copyfile(run_dir / name, bundle / 'outputs' / name)
    return rc


def write_manifest(bundle: Path, *, tag: str, commit: str, mode: str, rate_limit: float,
                   release: Path) -> dict:
    import rubio_backfill as rb
    run_dir = WORK / 'run'
    m = {
        'format': 1,
        'created': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'base_tag': tag,
        'corpus_sha256': B.sha256(release / 'corpus.csv'),
        'commit': commit,
        'mode': mode,
        'ci_args': B.relative_args(rb.ci_args(run_dir, mode), run_dir),
        'extra_args': ['--rate-limit', f'{rate_limit:g}'],
        'pythonhashseed': B.HASH_SEED,
        'env': B.env_fingerprint(),
        'inputs': {p.name: B.sha256(p) for p in sorted((bundle / 'inputs').iterdir())},
        'outputs': {p.name: B.sha256(p) for p in sorted((bundle / 'outputs').iterdir())},
    }
    (bundle / 'manifest.json').write_text(json.dumps(m, indent=1, sort_keys=True) + '\n',
                                          encoding='utf-8')
    return m


def alignment_tests(bundle: Path, release: Path, tag: str) -> int:
    env = dict(os.environ, SOS_BUNDLE=str(bundle), SOS_DATA=str(release), SOS_LATEST_TAG=tag)
    return subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                           str(HERE / 'tests')], cwd=REPO, env=env).returncode


# --- commit, push, PR ---------------------------------------------------------

def pr_body(bundle: Path, m: dict) -> str:
    s = json.loads((bundle / 'outputs' / 'rubio_backfill_summary.json').read_text(encoding='utf-8'))
    meta = json.loads((bundle / 'http' / 'meta.json').read_text(encoding='utf-8'))
    with open(bundle / 'outputs' / 'rubio_new.csv', newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    lines = [f'Local fetch of {bundle.name}, against **{m["base_tag"]}** (mode: {m["mode"]}).', '',
             f'- **New rows:** {len(rows)}',
             f'- **Requests to state.gov:** {meta.get("requests_total", 0)} '
             f'(by status: {meta.get("requests_by_status", {})})',
             f'- **Fetched / failed / pending:** {s.get("fetched", 0)} / {s.get("failed", 0)} / '
             f'{s.get("pending", 0)}',
             f'- **Discovery complete:** {s.get("discovery_complete")}',
             f'- **Commit:** {m["commit"][:12]}', '']
    if rows:
        lines += ['| Date | Title | URL |', '|---|---|---|']
        for r in rows[:60]:
            title = (r.get('Title') or '').replace('|', '/')[:90]
            lines.append(f'| {r.get("Date", "")} | {title} | {r.get("url", "")} |')
        if len(rows) > 60:
            lines.append(f'| | ... and {len(rows) - 60} more | |')
        lines.append('')
    lines += ['The alignment tests passed locally: replaying the recorded HTTP exchanges '
              'reproduced every output byte for byte. CI re-runs them on this PR together '
              'with a dry-run merge and the verifier.', '',
              '**Merging this PR publishes a new release** (GitHub + Hugging Face).']
    return '\n'.join(lines) + '\n'


def commit_and_push(bundle: Path, m: dict, push: bool) -> None:
    login = sh('gh', 'api', 'user', '-q', '.login')
    uid = sh('gh', 'api', 'user', '-q', '.id')
    email = f'{uid}+{login}@users.noreply.github.com'     # never a personal address
    branch = f'local-fetch/{bundle.name}'
    rel = bundle.relative_to(REPO).as_posix()
    n = json.loads((bundle / 'outputs' / 'rubio_backfill_summary.json').read_text())['written']
    sh('git', 'checkout', '-q', '-b', branch)
    try:
        sh('git', 'add', '--', rel)
        sh('git', '-c', f'user.name={login}', '-c', f'user.email={email}', 'commit', '-q',
           '-m', f'Local fetch {bundle.name}: {n} new Rubio row(s)\n\n'
                 f'Base release {m["base_tag"]}; replay-verified locally.')
        if push:
            sh('git', 'push', '-q', '-u', 'origin', branch)
            body = WORK / 'pr_body.md'
            body.write_text(pr_body(bundle, m), encoding='utf-8')
            url = sh('gh', 'pr', 'create', '--base', 'main', '--head', branch, '--title',
                     f'Local fetch {bundle.name}: {n} new Rubio row(s)', '--body-file', str(body))
            log(f'opened {url}')
        else:
            log(f'committed on {branch} (not pushed). Inspect it, then: '
                f'git push -u origin {branch} && gh pr create --fill --base main')
    finally:
        sh('git', 'checkout', '-q', 'main', check=False)


# --- main -----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import rubio_backfill as rb
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--mode', default='incremental', choices=rb.CI_MODES)
    ap.add_argument('--rate-limit', type=float, default=2.0,
                    help='seconds between requests (default %(default)s, minimum 1); '
                         'a robots.txt Crawl-delay raises it')
    ap.add_argument('--force', action='store_true', help='skip the once-a-week guard')
    ap.add_argument('--no-push', action='store_true', help='commit locally, do not push or open a PR')
    args = ap.parse_args(argv)
    if args.rate_limit < B.MIN_RATE_LIMIT:
        ap.error(f'--rate-limit must be at least {B.MIN_RATE_LIMIT:g}')
    today = datetime.now(timezone.utc).date()
    bundle = B.BUNDLES / today.isoformat()
    release = WORK / 'release'
    try:
        commit = preflight(today, args.force)
        if bundle.exists():
            raise Stop(f'{bundle.relative_to(REPO)} already exists')
        tag = stage_release(release)
        rc = fetch(release, bundle, args.mode, args.rate_limit)
        if rc != 0:
            summ = WORK / 'run' / 'rubio_backfill_summary.json'
            shutil.rmtree(bundle, ignore_errors=True)
            if rc == EXIT_REFUSED:
                info = json.loads(summ.read_text()) if summ.exists() else {}
                log(f'state.gov refused the crawler ({info.get("refused_status")} at '
                    f'{info.get("refused_url")}). Nothing was committed. The crawler does '
                    f'not try to get around a refusal.')
                return EXIT_REFUSED
            raise Stop(f'the fetch exited {rc}; nothing was committed (see the output above)')
        m = write_manifest(bundle, tag=tag, commit=commit, mode=args.mode,
                           rate_limit=args.rate_limit, release=release)
        log('running the alignment tests (replay + checks) ...')
        if alignment_tests(bundle, release, tag) != 0:
            failed = WORK / 'failed' / bundle.name
            shutil.rmtree(failed, ignore_errors=True)
            failed.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(bundle), str(failed))
            raise Stop(f'alignment tests failed; nothing was committed. The bundle was moved '
                       f'to {failed.relative_to(REPO)} for inspection.')
        commit_and_push(bundle, m, push=not args.no_push)
    except Stop as e:
        log(f'STOPPED: {e}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
