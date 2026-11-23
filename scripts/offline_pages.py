"""Serve state.gov pages a person saved in a browser to rubio_backfill, as if fetched.

Why: since about 2026-10-26 state.gov refuses automated clients, including the
honest crawler from a maintainer's home connection. A person can still read the
pages in a browser and save them (File > Save Page As). This module lets the
unchanged scraper parse those saved files, so the rows come out exactly as a
fetch would have produced them. Nothing here touches the network.

  index_pages(DIR)      canonical URL -> saved file, for every *.htm / *.html in DIR
                        (not recursive: a browser's "<name>_files" folders are
                        ignored). The URL is the page's own <link rel="canonical">,
                        else its og:url, else the "saved from url" comment browsers
                        write. Files with none of these, or not from state.gov, are
                        listed as skipped and never served.
  write_url_list(...)   the canonical URLs, one per line: the run's candidate list
                        (rubio_backfill --offline-urls), committed in a bundle.
  OfflineSession(DIR)   a requests.Session stand-in: a saved URL answers 200 with
                        the file's text; anything else (robots.txt included) answers
                        404, which the scraper treats as "not there".

A page saved by a browser is the page after its scripts ran, not the bytes the
server sent. Checked on 2026-11-23 on 16 pages already in the corpus, rebuilt from
saved copies through backfill and merge: every column matched the live-scraped row
except Raw Text (and so Row Hash), which differed only where state.gov itself had
since changed the page (a removed footer link, one updated job title).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import requests

_CANONICAL_RE = re.compile(
    r'<link\b[^>]*\brel=["\']?canonical["\']?[^>]*>', re.IGNORECASE)
_OG_URL_RE = re.compile(
    r'<meta\b[^>]*\bproperty=["\']?og:url["\']?[^>]*>', re.IGNORECASE)
_HREF_RE = re.compile(r'\b(?:href|content)=["\']([^"\']+)["\']', re.IGNORECASE)
_SAVED_FROM_RE = re.compile(r'<!--\s*saved from url=\(\d+\)(\S+?)\s*-->', re.IGNORECASE)
_HEAD_BYTES = 400_000          # the <head> is well inside this on state.gov pages
SUFFIXES = ('.htm', '.html')


def page_key(url: str) -> str:
    """How a saved page and a requested URL are matched: scheme and host lower-cased,
    no query or fragment, no trailing slash."""
    u = urlsplit((url or '').strip())
    return f'{u.scheme.lower()}://{u.netloc.lower()}{u.path.rstrip("/")}'


def _is_state_gov(url: str) -> bool:
    host = (urlsplit(url or '').hostname or '').lower()
    return host == 'state.gov' or host.endswith('.state.gov')


def read_text(path: Path) -> str:
    """The saved file as text. Browsers save state.gov pages as UTF-8 (they declare
    it); anything that is not valid UTF-8 is decoded the way requests would."""
    data = _long(path).read_bytes()
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        from charset_normalizer import from_bytes
        best = from_bytes(data).best()
        return str(best) if best is not None else data.decode('utf-8', errors='replace')


def _long(path: Path) -> Path:
    """Windows refuses paths over 260 characters unless written as \\\\?\\C:\\...;
    a browser happily saves a page under its full (long) title."""
    p = Path(path)
    if sys.platform != 'win32' or str(p).startswith('\\\\?\\'):
        return p
    return Path('\\\\?\\' + str(p.resolve()))


def page_url(html: str) -> str:
    """The page's own URL as the page states it ('' when it does not)."""
    head = html[:_HEAD_BYTES]
    for rx in (_CANONICAL_RE, _OG_URL_RE):
        tag = rx.search(head)
        if tag:
            href = _HREF_RE.search(tag.group(0))
            if href and href.group(1).startswith('http'):
                return href.group(1).strip()
    m = _SAVED_FROM_RE.search(html[:4096])
    return m.group(1).strip() if m and m.group(1).startswith('http') else ''


class OfflineIndex:
    def __init__(self, pages: dict[str, tuple[str, Path]], skipped: list[tuple[str, str]]):
        self.pages = pages          # page_key -> (canonical URL, file)
        self.skipped = skipped      # (file name, reason)

    @property
    def urls(self) -> list[str]:
        return sorted(url for url, _ in self.pages.values())


def index_pages(root: Path) -> OfflineIndex:
    """Every saved page in root, by canonical URL. Two files claiming one URL must be
    the same page text, else it is an error (which one is right is a person's call)."""
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f'offline pages directory not found: {root}')
    pages: dict[str, tuple[str, Path]] = {}
    texts: dict[str, str] = {}
    skipped: list[tuple[str, str]] = []
    # Listed through the long-path form: otherwise Windows reports a file with a long
    # title as "not a file" and it would be dropped without a word.
    for p in sorted(_long(root).iterdir(), key=lambda q: q.name):
        if p.suffix.lower() not in SUFFIXES or p.is_dir():
            continue
        html = read_text(p)
        url = page_url(html)
        if not url:
            skipped.append((p.name, 'no canonical URL (not a saved web page?)'))
            continue
        if not _is_state_gov(url):
            skipped.append((p.name, f'not a state.gov page ({urlsplit(url).hostname})'))
            continue
        key = page_key(url)
        if key in pages:
            if texts[key] != html:
                raise ValueError(f'{p.name} and {pages[key][1].name} are both saved copies of '
                                 f'{url} but differ: keep one of them')
            skipped.append((p.name, f'duplicate of {pages[key][1].name}'))
            continue
        pages[key] = (url, p)
        texts[key] = html
    return OfflineIndex(pages, skipped)


def write_url_list(index: OfflineIndex, path: Path) -> int:
    """The run's candidate list: canonical URLs only (no file names, no local paths)."""
    lines = ['# state.gov pages saved by hand in a browser (local_fetch --offline-pages);',
             '# rubio_backfill --offline-urls reads this list.', *index.urls]
    Path(path).write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\n')
    return len(index.urls)


class _OfflineResponse:
    def __init__(self, url: str, status: int, text: str):
        self.url, self.status_code, self.text = url, status, text
        self.headers: dict = {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} Client Error: not among the saved '
                                     f'pages for url: {self.url}', response=self)


class OfflineSession:
    """Answers GETs from the saved pages. Never opens a connection."""

    def __init__(self, root: Path):
        self.index = index_pages(root)
        self.served: list[str] = []

    def get(self, url, **kw):
        hit = self.index.pages.get(page_key(url))
        if hit is None:
            return _OfflineResponse(url, 404, '')
        canonical, path = hit
        self.served.append(canonical)
        return _OfflineResponse(canonical, 200, read_text(path))

    def close(self) -> None:
        pass
