"""Archiving State's GovDelivery emails (scripts/rubio_email_fetch.py), offline."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import rubio_email_fetch as ef  # noqa: E402


def eml(sender, mid, date='Mon, 09 Nov 2026 15:04:00 -0500', subject='Secretary Rubio remarks'):
    return (f'From: {sender}\r\nTo: project@example.org\r\nSubject: {subject}\r\n'
            f'Date: {date}\r\nMessage-ID: <{mid}>\r\n\r\nBody text.\r\n').encode()


class FakeIMAP:
    """Just enough of imaplib.IMAP4 for fetch(): a mailbox of {uid: raw}."""

    def __init__(self, box, validity=b'777'):
        self.box, self.validity, self.readonly = box, validity, None

    def select(self, mailbox, readonly=False):
        self.readonly = readonly
        return 'OK', [str(len(self.box)).encode()]

    def response(self, code):
        return code, [self.validity]

    def uid(self, cmd, *args):
        if cmd == 'SEARCH':
            lo = int(args[1].split()[1].split(':')[0])
            return 'OK', [b' '.join(str(u).encode() for u in sorted(self.box) if u >= lo)]
        if cmd == 'FETCH':
            u = int(args[0])
            return 'OK', [(f'{u} (RFC822 {{n}}'.encode(), self.box[u]), b')']
        raise AssertionError(cmd)


BOX = {
    3: eml('U.S. Department of State <usstatebpa@public.govdelivery.com>', 'a1@govdelivery'),
    4: eml('Someone <someone@example.org>', 'fwd@example'),               # not GovDelivery
    5: eml('State <x@govdelivery.com>', 'a2@govdelivery', date='Tue, 10 Nov 2026 09:00:00 +0000'),
    6: eml('Evil <x@notgovdelivery.com>', 'spoof@x'),                      # look-alike domain
}


def test_archives_only_govdelivery_mail_readonly(tmp_path):
    imap = FakeIMAP(BOX)
    saved, state = ef.fetch(imap, tmp_path, {})
    assert imap.readonly is True                                    # EXAMINE, never modifies
    assert sorted(p.relative_to(tmp_path).as_posix() for p in saved) == [
        '2026/11/2026-11-09_a1_govdelivery.eml', '2026/11/2026-11-10_a2_govdelivery.eml']
    assert saved[0].read_bytes() == BOX[3]                          # verbatim
    assert state == {'uidvalidity': '777', 'last_uid': 6}


def test_incremental_and_uidvalidity_reset(tmp_path):
    imap = FakeIMAP(BOX)
    _, state = ef.fetch(imap, tmp_path, {})
    imap.box[9] = eml('State <y@public.govdelivery.com>', 'a3@govdelivery')
    saved, state = ef.fetch(imap, tmp_path, state)
    assert [p.name for p in saved] == ['2026-11-09_a3_govdelivery.eml'] and state['last_uid'] == 9
    # A new UIDVALIDITY means UIDs were renumbered: rescan all, but files dedup by name.
    saved, state = ef.fetch(FakeIMAP(imap.box, validity=b'888'), tmp_path, state)
    assert saved == [] and state == {'uidvalidity': '888', 'last_uid': 9}


def test_unconfigured_mailbox_is_a_no_op(monkeypatch, tmp_path, capsys):
    for k in ('SOS_IMAP_HOST', 'SOS_IMAP_USER', 'SOS_IMAP_PASSWORD'):
        monkeypatch.delenv(k, raising=False)
    assert ef.main(['--archive-dir', str(tmp_path / 'a'), '--state', str(tmp_path / 's.json')]) == 0
    assert 'not configured' in capsys.readouterr().out
    assert not (tmp_path / 's.json').exists()
