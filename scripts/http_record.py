"""Record every HTTP exchange of a rubio_backfill run, and replay it offline.

Why: a fetch run on a maintainer's own computer (local_fetch/) is checked in CI by
replaying exactly what state.gov sent and re-running the same code over it. If the
replayed run does not produce the same rows and state files byte for byte, the
bundle is rejected. That proves the committed rows came from the recorded pages
and the committed code, not from an edit.

A recording directory holds:
  requests.jsonl      one line per request, in order: seq, url, status, final_url,
                      body (path of a gzipped text file) or error {type, message}
  bodies/NNNNN.txt.gz the response text (resp.text), gzip with mtime 0
  clock.json          every read of the discovery time-budget clock, in order
  meta.json           written by the caller (run id, date, User-Agent, counts)

Replay serves responses per URL in recorded order and returns the recorded clock
values, so retry and time-budget decisions repeat exactly. A request that was never
recorded raises ReplayMiss, a BaseException, so no broad `except Exception` in the
scraper can turn it into an ordinary fetch failure; the replay fails loudly instead.
"""

from __future__ import annotations

import gzip
import json
import time
from collections import defaultdict, deque
from pathlib import Path
from types import SimpleNamespace

import requests


class ReplayMiss(BaseException):
    """The replayed run asked for something the recording does not have."""


def _gzip_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'wb') as raw:
        with gzip.GzipFile(fileobj=raw, mode='wb', mtime=0, filename='') as gz:
            gz.write(text.encode('utf-8'))


def _final_url_of(exc: Exception) -> str:
    for attr in ('response', 'request'):
        obj = getattr(exc, attr, None)
        u = getattr(obj, 'url', None) if obj is not None else None
        if u:
            return str(u)
    return ''


class RecordingSession:
    """Wraps a requests.Session; everything it is asked for is written to `root`."""

    def __init__(self, root: Path, session: requests.Session | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.session = session or requests.Session()
        self.seq = 0
        self.clock_reads: list[float] = []
        self._log = open(self.root / 'requests.jsonl', 'w', encoding='utf-8')

    def get(self, url, **kw):
        self.seq += 1
        entry = {'seq': self.seq, 'url': url}
        try:
            resp = self.session.get(url, **kw)
        except Exception as e:  # noqa: BLE001 -- recorded, then re-raised unchanged
            entry.update(status=None, final_url=_final_url_of(e), body=None,
                         error={'type': type(e).__name__, 'message': str(e)[:500]})
            self._write(entry)
            raise
        body = f'bodies/{self.seq:05d}.txt.gz'
        _gzip_text(self.root / body, resp.text)
        # The exact raise_for_status() message, so a replayed HTTP error carries the
        # same text (it ends up in rubio_pending.csv's last_error).
        http_error = None
        if resp.status_code >= 400:
            try:
                resp.raise_for_status()
            except requests.HTTPError as e:
                http_error = str(e)
        entry.update(status=resp.status_code, final_url=str(resp.url or url), body=body,
                     error=None, http_error=http_error)
        self._write(entry)
        return resp

    def _write(self, entry: dict) -> None:
        self._log.write(json.dumps(entry, sort_keys=True) + '\n')
        self._log.flush()

    def clock(self) -> float:
        t = time.monotonic()
        self.clock_reads.append(t)
        return t

    def close(self) -> None:
        self._log.close()
        (self.root / 'clock.json').write_text(json.dumps(self.clock_reads) + '\n',
                                              encoding='utf-8')


class _ReplayResponse:
    def __init__(self, url: str, status: int, text: str, http_error: str | None = None):
        self.url, self.status_code, self.text = url, status, text
        self.http_error = http_error

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(self.http_error or f'{self.status_code} Error for url: '
                                     f'{self.url}', response=self)


class ReplaySession:
    """Serves a recording made by RecordingSession; never touches the network."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.queues: dict[str, deque] = defaultdict(deque)
        with open(self.root / 'requests.jsonl', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    e = json.loads(line)
                    self.queues[e['url']].append(e)
        clock_path = self.root / 'clock.json'
        self.clock_reads = deque(json.loads(clock_path.read_text(encoding='utf-8'))
                                 if clock_path.exists() else [])
        self.served = 0

    def get(self, url, **kw):
        q = self.queues.get(url)
        if not q:
            raise ReplayMiss(f'not in the recording (or requested more often than '
                             f'recorded): {url}')
        e = q.popleft()
        self.served += 1
        if e.get('error'):
            name = e['error']['type']
            cls = getattr(requests.exceptions, name, None)
            if not (isinstance(cls, type) and issubclass(cls, requests.RequestException)):
                # Same class name (it is what the run tallies), a RequestException
                # so the scraper's handlers treat it as the network error it was.
                cls = type(name, (requests.RequestException,), {})
            req = SimpleNamespace(url=e['final_url']) if e.get('final_url') else None
            raise cls(e['error']['message'], request=req)
        with gzip.open(self.root / e['body'], 'rt', encoding='utf-8') as f:
            text = f.read()
        return _ReplayResponse(e['final_url'] or url, int(e['status']), text,
                               e.get('http_error'))

    def clock(self) -> float:
        if not self.clock_reads:
            raise ReplayMiss('the replayed run read the time-budget clock more often '
                             'than the recorded one')
        return self.clock_reads.popleft()

    def unconsumed(self) -> list[str]:
        """Recorded requests the replay never asked for (should be none)."""
        return [e['url'] for q in self.queues.values() for e in q]
