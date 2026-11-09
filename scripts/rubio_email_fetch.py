#!/usr/bin/env python3
"""Archive the State Department's official email releases (GovDelivery) from the
project mailbox, as the source for new Rubio statements while state.gov refuses
automated clients (since about 2026-10-26).

This step only FETCHES and ARCHIVES: every new message from a GovDelivery sender is
saved verbatim as an .eml file. Turning those emails into corpus rows is a separate
step (rubio_email_ingest.py), written against real samples once a few weeks of them
exist. Nothing here fetches anything from state.gov.

Read-only on the mailbox: the INBOX is opened with EXAMINE, so nothing is deleted
or flagged. Progress is tracked by IMAP UID (plus UIDVALIDITY) in a small JSON file.

Credentials come from the environment (repository secrets in CI):
  SOS_IMAP_HOST, SOS_IMAP_USER, SOS_IMAP_PASSWORD   [SOS_IMAP_PORT=993]
With any of them unset the script prints a notice and exits 0, so the weekly run
works before the mailbox exists.

  python scripts/rubio_email_fetch.py --archive-dir data/email_archive \\
         --state data/rubio_email_state.json
"""

from __future__ import annotations

import argparse
import email
import email.policy
import email.utils
import hashlib
import imaplib
import json
import os
import re
import sys
from pathlib import Path

# GovDelivery sends on the Department's behalf from *.govdelivery.com addresses.
_SENDER_DOMAINS = ('govdelivery.com',)
_SAFE = re.compile(r'[^A-Za-z0-9._-]+')


def is_state_release(msg: email.message.Message) -> bool:
    """True for a message sent by GovDelivery (forwarded or other mail is ignored)."""
    _, addr = email.utils.parseaddr(str(msg.get('From', '')))
    domain = addr.rsplit('@', 1)[-1].lower() if '@' in addr else ''
    return any(domain == d or domain.endswith('.' + d) for d in _SENDER_DOMAINS)


def archive_path(root: Path, msg: email.message.Message, raw: bytes) -> Path:
    """data/email_archive/YYYY/MM/<date>_<msgid-or-hash>.eml (stable, so re-runs dedup)."""
    try:
        dt = email.utils.parsedate_to_datetime(str(msg.get('Date')))
        ym, day = dt.strftime('%Y/%m'), dt.strftime('%Y-%m-%d')
    except (TypeError, ValueError):
        ym, day = 'undated', 'undated'
    mid = str(msg.get('Message-ID', '')).strip('<> ')
    key = _SAFE.sub('_', mid)[:80] if mid else hashlib.sha256(raw).hexdigest()[:24]
    return root / ym / f'{day}_{key}.eml'


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def fetch(imap, archive: Path, state: dict) -> tuple[list[Path], dict]:
    """Archive every new GovDelivery message; return (new files, new state)."""
    typ, _ = imap.select('INBOX', readonly=True)
    if typ != 'OK':
        raise RuntimeError('cannot open INBOX')
    validity = (imap.response('UIDVALIDITY')[1] or [b''])[0]
    validity = validity.decode() if isinstance(validity, bytes) else str(validity or '')
    last = int(state.get('last_uid', 0)) if state.get('uidvalidity') == validity else 0
    typ, data = imap.uid('SEARCH', None, f'UID {last + 1}:*')
    uids = [int(u) for u in (data[0] or b'').split() if int(u) > last]
    saved: list[Path] = []
    for uid in uids:
        typ, parts = imap.uid('FETCH', str(uid), '(RFC822)')
        raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
        if typ != 'OK' or raw is None:
            raise RuntimeError(f'cannot fetch UID {uid}')
        msg = email.message_from_bytes(raw, policy=email.policy.compat32)
        if is_state_release(msg):
            path = archive_path(archive, msg, raw)
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
                saved.append(path)
        last = max(last, uid)
    return saved, {'uidvalidity': validity, 'last_uid': last}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--archive-dir', default='data/email_archive')
    ap.add_argument('--state', default='data/rubio_email_state.json')
    args = ap.parse_args(argv)
    host, user, pw = (os.environ.get(k, '') for k in
                      ('SOS_IMAP_HOST', 'SOS_IMAP_USER', 'SOS_IMAP_PASSWORD'))
    if not (host and user and pw):
        print('[rubio_email_fetch] mailbox not configured (SOS_IMAP_* unset); skipping.')
        return 0
    state_path = Path(args.state)
    imap = imaplib.IMAP4_SSL(host, int(os.environ.get('SOS_IMAP_PORT', '993')))
    try:
        imap.login(user, pw)
        saved, state = fetch(imap, Path(args.archive_dir), load_state(state_path))
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001 -- best effort
            pass
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=1) + '\n', encoding='utf-8')
    print(f'[rubio_email_fetch] archived {len(saved)} new State email(s); last UID {state["last_uid"]}')
    for p in saved:
        print(f'  {p}')
    out = os.environ.get('GITHUB_OUTPUT')
    if out:
        with open(out, 'a', encoding='utf-8') as f:
            f.write(f'emails_new={len(saved)}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
