"""Local-fetch bundles: what a fetch on a maintainer's computer commits, and the
checks that decide whether CI may build a release from it.

A bundle is local_fetch/bundles/<YYYY-MM-DD>/:
  manifest.json  base release, sha256 of every input and output, repo commit,
                 the scrape arguments, the mode
  inputs/        the run's starting state: rubio_pending.csv, rubio_held.csv,
                 rubio_discovery_state.json and rubio_gt_missing.txt (whichever existed),
                 plus the checked-in rubio_coverage_exclusions.csv and
                 rubio_supplemental_urls.txt
  http/          every HTTP exchange with state.gov (scripts/http_record.py)
  outputs/       what rubio_backfill.py wrote: rubio_new.csv, the state files, sidecars

The corpus itself is not in the bundle. It is the base release's corpus asset,
identified by tag and sha256.

The checks (verify()) are used by local_fetch/tests/test_bundle_alignment.py, which
the driver runs before committing, and by `bundle.py import` in the weekly workflow,
which runs them again before merging:
  * the base release is still the latest one, and the bundle's inputs are byte for
    byte what that release and the repository hold now (otherwise it is stale);
  * the commit it was fetched with is on main;
  * it ran exactly the weekly job's scrape command (rubio_backfill.ci_args) with the
    honest User-Agent;
  * REPLAY: re-running that command offline over the recorded HTTP exchanges
    reproduces every output file byte for byte. The rows therefore come from what
    state.gov sent and from the committed code, not from an edit;
  * the run state only moves forward, and the bundle has nothing it should not
    (stray files, local paths, the machine's name).

  python local_fetch/bundle.py import --bundle local_fetch/bundles/2026-11-23 --data data
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'scripts'
BUNDLES = REPO / 'local_fetch' / 'bundles'
sys.path.insert(0, str(SCRIPTS))

csv.field_size_limit(10 ** 9)

# Starting state that rides on the latest release (or is derived from it).
STATE_INPUTS = ('rubio_pending.csv', 'rubio_held.csv', 'rubio_discovery_state.json',
                'rubio_gt_missing.txt')
# Checked-in seed files, read from the repository's data/.
CHECKED_IN = ('rubio_coverage_exclusions.csv', 'rubio_supplemental_urls.txt')
INPUTS = STATE_INPUTS + CHECKED_IN
OUTPUTS = ('rubio_new.csv', 'rubio_pending.csv', 'rubio_discovery_state.json',
           'rubio_skipped.csv', 'rubio_listing_snapshot.csv', 'rubio_backfill_summary.json')
OPTIONAL_OUTPUTS = ('rubio_stuck.csv',)
TOP_LEVEL = {'manifest.json', 'inputs', 'outputs', 'http'}
MAX_BUNDLE_BYTES = 20 * 1024 * 1024
MIN_RATE_LIMIT = 1.0
HASH_SEED = '0'           # PYTHONHASHSEED for the fetch and the replay alike
REQUIREMENTS = REPO / 'local_fetch' / 'requirements.txt'
# What decides how a page parses: these must be the same for the fetch and the replay.
PARSE_PACKAGES = ('beautifulsoup4', 'soupsieve', 'lxml', 'html5lib', 'ftfy', 'wcwidth',
                  'requests', 'charset-normalizer')


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def env_fingerprint() -> dict:
    """Python minor version and the parsing packages' versions (None = not installed)."""
    from importlib import metadata
    pkgs = {}
    for name in PARSE_PACKAGES:
        try:
            pkgs[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pkgs[name] = None
    return {'python': '%d.%d' % sys.version_info[:2], 'packages': pkgs}


def pinned_requirements(path: Path = REQUIREMENTS) -> dict:
    pins = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.split('#', 1)[0].strip()
        if '==' in line:
            name, ver = line.split('==', 1)
            pins[name.strip().lower()] = ver.strip()
    return pins


def env_problems(fp: dict | None = None) -> list[str]:
    """This environment against local_fetch/requirements.txt (Python 3.11, no lxml)."""
    fp = fp or env_fingerprint()
    pins, out = pinned_requirements(), []
    if fp['python'] != '3.11':
        out.append(f'Python {fp["python"]}; the fetch and CI\'s replay use 3.11')
    for name, ver in fp['packages'].items():
        want = pins.get(name)
        if ver != want:
            out.append(f'{name} {ver or "not installed"}, local_fetch/requirements.txt has '
                       f'{want or "none"}')
    return out


def load_manifest(bundle: Path) -> dict:
    return json.loads((Path(bundle) / 'manifest.json').read_text(encoding='utf-8'))


def newest_bundle(root: Path = BUNDLES) -> Path | None:
    dirs = sorted(p for p in Path(root).glob('*') if (p / 'manifest.json').exists())
    return dirs[-1] if dirs else None


def relative_args(args: list[str], data_dir: Path) -> list[str]:
    """ci_args with the data directory written as 'data', so runs compare anywhere."""
    prefix = str(data_dir)
    return [('data' + a[len(prefix):]).replace(os.sep, '/') if a.startswith(prefix) else a
            for a in args]


def expected_ci_args(bundle: Path, mode: str) -> list[str]:
    """What rubio_backfill.ci_args gives for the bundle's inputs, as 'data/...'."""
    import rubio_backfill as rb
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / 'data'
        d.mkdir()
        for name in INPUTS:
            if (Path(bundle) / 'inputs' / name).exists():
                (d / name).write_bytes(b'')
        return relative_args(rb.ci_args(d, mode), d)


def _link_or_copy(src: Path, dst: Path) -> None:
    try:
        os.symlink(Path(src).resolve(), dst)
    except (OSError, NotImplementedError):      # e.g. Windows without the privilege
        shutil.copyfile(src, dst)


def run_backfill(data_dir: Path, mode: str, extra: list[str], *,
                 log: Path | None = None) -> int:
    """Run the weekly job's scrape command over data_dir; returns the exit code."""
    import rubio_backfill as rb
    cmd = [sys.executable, str(SCRIPTS / 'rubio_backfill.py'), *rb.ci_args(data_dir, mode),
           *extra]
    env = dict(os.environ, PYTHONHASHSEED=HASH_SEED)
    for k in ('GITHUB_OUTPUT', 'GITHUB_RUN_ID'):
        env.pop(k, None)
    if log is None:
        return subprocess.run(cmd, cwd=REPO, env=env).returncode
    with open(log, 'w', encoding='utf-8') as fh:
        return subprocess.run(cmd, cwd=REPO, env=env, stdout=fh,
                              stderr=subprocess.STDOUT).returncode


# --- checks -----------------------------------------------------------------
# Each returns a list of problems; empty means it passed.

def check_hygiene(bundle: Path) -> list[str]:
    bundle = Path(bundle)
    problems = []
    extra = {p.name for p in bundle.iterdir()} - TOP_LEVEL
    if extra:
        problems.append(f'unexpected entries in the bundle: {sorted(extra)}')
    m = load_manifest(bundle)
    for sub, names in (('inputs', INPUTS), ('outputs', OUTPUTS + OPTIONAL_OUTPUTS)):
        d = bundle / sub
        stray = sorted({p.name for p in d.iterdir()} - set(names)) if d.exists() else []
        if stray:
            problems.append(f'unexpected files in {sub}/: {stray}')
    for name in OUTPUTS:
        if not (bundle / 'outputs' / name).exists():
            problems.append(f'outputs/{name} is missing')
    total = sum(p.stat().st_size for p in bundle.rglob('*') if p.is_file())
    if total > MAX_BUNDLE_BYTES:
        problems.append(f'bundle is {total / 1e6:.1f} MB (limit {MAX_BUNDLE_BYTES / 1e6:.0f} MB)')
    needles = {str(Path.home())}
    host = socket.gethostname().split('.')[0]
    if len(host) >= 4:
        needles.add(host)
    for p in bundle.rglob('*'):
        if p.is_file() and p.suffix in ('.json', '.jsonl', '.csv', '.txt'):
            text = p.read_text(encoding='utf-8', errors='replace')
            for n in needles:
                if n and n in text:
                    problems.append(f'{p.relative_to(bundle)} contains a local path or host name')
                    break
    for sub, key in (('inputs', 'inputs'), ('outputs', 'outputs')):
        for name, digest in (m.get(key) or {}).items():
            p = bundle / sub / name
            if not p.exists() or sha256(p) != digest:
                problems.append(f'{sub}/{name} does not match its sha256 in manifest.json')
    return problems


def check_base(bundle: Path, data_dir: Path, latest_tag: str) -> list[str]:
    """The bundle starts from the current latest release and the current seed files.

    data_dir holds what CI (or the driver) downloaded for that release, untouched:
    corpus.csv, the state assets, rubio_gt_missing.txt from the reconcile step, and
    the checked-in seed files.
    """
    bundle, data_dir = Path(bundle), Path(data_dir)
    m = load_manifest(bundle)
    problems = []
    if m.get('base_tag') != latest_tag:
        problems.append(f'bundle was fetched against {m.get("base_tag")}, but the latest '
                        f'release is now {latest_tag}: re-run local_fetch.py')
    corpus = data_dir / 'corpus.csv'
    if not corpus.exists() or sha256(corpus) != m.get('corpus_sha256'):
        problems.append('corpus.csv is not the corpus the bundle was fetched against')
    for name in INPUTS:
        mine, now = bundle / 'inputs' / name, data_dir / name
        if mine.exists() != now.exists():
            problems.append(f'{name}: {"in" if mine.exists() else "not in"} the bundle, '
                            f'{"present" if now.exists() else "absent"} now')
        elif mine.exists() and sha256(mine) != sha256(now):
            problems.append(f'{name} has changed since the bundle was fetched')
    return problems


def check_commit(bundle: Path, ref: str = 'origin/main') -> list[str]:
    commit = load_manifest(bundle).get('commit', '')
    r = subprocess.run(['git', 'merge-base', '--is-ancestor', commit, ref], cwd=REPO,
                       capture_output=True)
    if r.returncode != 0:
        return [f'commit {commit[:12] or "(none)"} is not on {ref}']
    return []


def check_args(bundle: Path) -> list[str]:
    import rubio_backfill as rb
    bundle = Path(bundle)
    m = load_manifest(bundle)
    problems = []
    want = expected_ci_args(bundle, m.get('mode', ''))
    if m.get('ci_args') != want:
        problems.append(f'scrape arguments differ from the weekly job\'s: '
                        f'{m.get("ci_args")} != {want}')
    extra = m.get('extra_args') or []
    if len(extra) != 2 or extra[0] != '--rate-limit' or float(extra[1]) < MIN_RATE_LIMIT:
        problems.append(f'extra arguments must be exactly --rate-limit N with N >= '
                        f'{MIN_RATE_LIMIT:g}: {extra}')
    meta = json.loads((bundle / 'http' / 'meta.json').read_text(encoding='utf-8'))
    if meta.get('user_agent') != rb._HEADERS['User-Agent']:
        problems.append(f'User-Agent was {meta.get("user_agent")!r}, not the crawler\'s own')
    if not meta.get('completed'):
        problems.append('the recorded run did not complete')
    return problems


def check_env(bundle: Path) -> list[str]:
    """The replay runs with the parsing environment the fetch ran with."""
    had, now = load_manifest(bundle).get('env'), env_fingerprint()
    if had != now:
        return [f'parsing environment differs from the fetch: fetched with {had}, replaying '
                f'with {now} (install local_fetch/requirements.txt)']
    return []


def replay(bundle: Path, corpus: Path, workdir: Path | None = None) -> list[str]:
    """Re-run the bundle's command offline over its recording; compare every output."""
    bundle = Path(bundle)
    m = load_manifest(bundle)
    with tempfile.TemporaryDirectory(dir=workdir) as td:
        d = Path(td) / 'data'
        d.mkdir()
        for name in INPUTS:
            src = bundle / 'inputs' / name
            if src.exists():
                shutil.copyfile(src, d / name)
        _link_or_copy(Path(corpus), d / 'corpus.csv')
        # Any request that slipped past the replay session would hit a dead proxy.
        saved = {k: os.environ.get(k) for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY')}
        os.environ.update(HTTP_PROXY='http://127.0.0.1:9', HTTPS_PROXY='http://127.0.0.1:9',
                          NO_PROXY='')
        try:
            rc = run_backfill(d, m['mode'], ['--rate-limit', '0', '--replay',
                                             str(bundle / 'http')], log=Path(td) / 'replay.log')
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        if rc != 0:
            tail = (Path(td) / 'replay.log').read_text(encoding='utf-8', errors='replace')[-2000:]
            return [f'replay exited {rc} (the fetch exited 0):\n{tail}']
        problems = []
        for name in OUTPUTS + OPTIONAL_OUTPUTS:
            mine, again = bundle / 'outputs' / name, d / name
            if mine.exists() != again.exists():
                problems.append(f'outputs/{name}: present in only one of bundle and replay')
            elif mine.exists() and mine.read_bytes() != again.read_bytes():
                problems.append(f'outputs/{name} differs from what the replay produced')
        return problems


def check_forward(bundle: Path, corpus: Path) -> list[str]:
    """State moves forward: the floor never drops, and no pending URL just vanishes."""
    import rubio_backfill as rb
    bundle = Path(bundle)
    problems = []
    before_p, after_p = (bundle / 'inputs' / 'rubio_discovery_state.json',
                         bundle / 'outputs' / 'rubio_discovery_state.json')
    if before_p.exists():
        before = json.loads(before_p.read_text(encoding='utf-8'))
        after = json.loads(after_p.read_text(encoding='utf-8'))
        if (before.get('last_complete_floor') and
                (after.get('last_complete_floor') or '') < before['last_complete_floor']):
            problems.append(f'last_complete_floor moved back: {before["last_complete_floor"]}'
                            f' -> {after.get("last_complete_floor")}')

    def urls(path: Path) -> dict:
        if not path.exists():
            return {}
        with open(path, newline='', encoding='utf-8') as f:
            return {rb._live_key(r['url']): r['url'] for r in csv.DictReader(f) if r.get('url')}
    before_q = urls(bundle / 'inputs' / 'rubio_pending.csv')
    gone = set(before_q) - set(urls(bundle / 'outputs' / 'rubio_pending.csv'))
    accounted = (set(urls(bundle / 'outputs' / 'rubio_new.csv'))
                 | set(urls(bundle / 'outputs' / 'rubio_skipped.csv')))
    index = rb.load_existing_urls(Path(corpus)) if gone - accounted else None
    lost = [before_q[k] for k in sorted(gone - accounted) if not rb._in_corpus(before_q[k], index)]
    if lost:
        problems.append(f'{len(lost)} pending URL(s) dropped without a row, a skip reason '
                        f'or a corpus match, e.g. {lost[:3]}')
    return problems


def verify(bundle: Path, data_dir: Path, latest_tag: str, *, ref: str = 'origin/main',
           workdir: Path | None = None) -> list[str]:
    corpus = Path(data_dir) / 'corpus.csv'
    out = (check_hygiene(bundle) + check_base(bundle, data_dir, latest_tag)
           + check_commit(bundle, ref) + check_args(bundle) + check_env(bundle))
    if out:                     # replay only a bundle that is current and well formed
        return out
    return replay(bundle, corpus, workdir) + check_forward(bundle, corpus)


# --- CI import --------------------------------------------------------------

def _gh_output(**kv) -> None:
    path = os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path, 'a', encoding='utf-8') as f:
            for k, v in kv.items():
                f.write(f'{k}={v}\n')


def import_bundle(bundle: Path, data_dir: Path, latest_tag: str) -> int:
    """Verify, then copy the bundle's outputs into data/ as if the scrape had run."""
    bundle, data_dir = Path(bundle), Path(data_dir)
    problems = verify(bundle, data_dir, latest_tag, workdir=data_dir.parent)
    if problems:
        print(f'[bundle] {bundle.name}: REJECTED', file=sys.stderr)
        for p in problems:
            print(f'  - {p}', file=sys.stderr)
        return 1
    for name in OUTPUTS + OPTIONAL_OUTPUTS:
        src = bundle / 'outputs' / name
        if src.exists():
            shutil.copyfile(src, data_dir / name)
    s = json.loads((bundle / 'outputs' / 'rubio_backfill_summary.json').read_text(encoding='utf-8'))
    m = load_manifest(bundle)
    meta = json.loads((bundle / 'http' / 'meta.json').read_text(encoding='utf-8'))
    note = (f'Fetched from a maintainer\'s computer (local_fetch bundle {bundle.name}, '
            f'{meta.get("requests_total", 0)} requests to state.gov, commit '
            f'{m["commit"][:12]}); replay-verified in CI.')
    _gh_output(rows_written=s.get('written', 0), fetched=s.get('fetched', 0),
               fetch_failed=s.get('failed', 0),
               skipped=sum((s.get('skipped_by_reason') or {}).values()),
               pending=s.get('pending', 0), stuck=s.get('stuck', 0),
               discovery_complete=str(bool(s.get('discovery_complete'))).lower(),
               local_bundle=bundle.name, local_note=note)
    print(f'[bundle] {bundle.name}: verified (replay identical); '
          f'{s.get("written", 0)} new row(s) imported into {data_dir}')
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    imp = sub.add_parser('import', help='verify a bundle and copy its outputs into DATA')
    imp.add_argument('--bundle', default=None, help='default: the newest bundle')
    imp.add_argument('--data', default='data')
    imp.add_argument('--latest-tag', default=None, help='default: gh release view')
    args = ap.parse_args(argv)
    bundle = Path(args.bundle) if args.bundle else newest_bundle()
    if bundle is None:
        print('[bundle] no bundle under local_fetch/bundles/', file=sys.stderr)
        return 1
    tag = args.latest_tag or subprocess.run(
        ['gh', 'release', 'view', '--json', 'tagName', '-q', '.tagName'], cwd=REPO,
        capture_output=True, text=True, check=True).stdout.strip()
    return import_bundle(bundle, Path(args.data), tag)


if __name__ == '__main__':
    sys.exit(main())
