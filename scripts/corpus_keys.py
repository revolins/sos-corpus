#!/usr/bin/env python3
"""Shared URL identity keys for the SOS corpus pipeline.

Three scripts used to carry three drifting copies of "canonicalize a URL":
hybrid_corpus_recovery.canonical_url_key (strips the scheme but KEEPS www.),
corpus_rubio_update._canonical_url_key (strips www. too), and
corpus_verify_research_grade.canonical_url. They agreed for every URL in the
corpus by luck -- everything is https://www.state.gov/... -- but the asymmetry
was a live landmine: the scraper and the merge script compared candidate URLs
against two different keyspaces.

This module is the single definition. It deliberately has NO third-party
imports so corpus_verify_research_grade.py can use it while keeping its
"the verifier must not depend on the enrichment stack" property.

── Why (slug, Date) exists ──────────────────────────────────────────────────
Between 2026-09-14 and 2026-09-21 state.gov renamed a path segment:

    /releases/office-of-the-spokesperson/YYYY/MM/<slug>/
 -> /releases/office-of-the-spokesman/YYYY/MM/<slug>/

URL-key dedup went blind and the weekly job queued 609 already-present
speeches for append. Nothing caught it: Row Hash includes URL, so the
duplicate-content check passed too. Only an unrelated Location failure
stopped the release.

The slug and the date survive that rename, so (slug, Date) does not. Measured
over all 17,617 rows of corpus_v2026.09.03-enriched.csv, (slug, Date) has
ZERO collisions -- while the slug alone has 424 colliding groups, because the
annual "<country>-national-day" statements reuse a slug every year.

Before a page is fetched its Date is unknown, but the current-era URL carries
the month. (slug, YYYY, MM) is the pre-fetch form: verified to match the row's
Date month for 567/567 corpus URLs that have a /YYYY/MM/ segment, zero
exceptions.
"""

from __future__ import annotations

import re

__all__ = [
    'canonical_url_key', 'url_slug', 'url_year_month',
    'slug_date_key', 'slug_ym_key', 'canonicalize_live_url',
    'LIVE_HOST', 'PATH_RENAMES',
]

_SCHEME_RE = re.compile(r'^https?://', re.IGNORECASE)
_WWW_RE = re.compile(r'^www\.', re.IGNORECASE)
_URL_YM_RE = re.compile(r'/((?:19|20)\d{2})/(\d{2})/')

# Keys are joined with ASCII unit separator, matching corpus_enrich_v1's
# HASH_DELIM convention -- it cannot appear in a URL or an ISO date.
_DELIM = '\x1f'


def canonical_url_key(url: str) -> str:
    """Lowercase, scheme-less, www-less, no trailing slash.

    This is the strict form (it strips www.), matching what
    corpus_rubio_update.py has always used for the merge-side dedup.
    """
    key = (url or '').strip().lower()
    key = _SCHEME_RE.sub('', key)
    key = _WWW_RE.sub('', key)
    return key.rstrip('/')


def url_slug(url: str) -> str:
    """Last non-empty path segment, lowercased.

    Invariant under a path-segment rename; NOT unique on its own (annual
    "<country>-national-day" statements reuse slugs), so always pair it with
    a date -- see slug_date_key / slug_ym_key.
    """
    key = canonical_url_key(url)
    if not key:
        return ''
    # Drop query/fragment before taking the segment.
    key = key.split('#', 1)[0].split('?', 1)[0].rstrip('/')
    segments = [s for s in key.split('/') if s]
    # segments[0] is the host; a bare host has no slug.
    return segments[-1] if len(segments) > 1 else ''


def url_year_month(url: str) -> tuple[int, int] | None:
    """The /YYYY/MM/ pair a current-era state.gov release URL carries.

    Returns None for the legacy flat-slug form (state.gov migrated to the
    dated path in mid-2025; 157 of the corpus's 724 Rubio rows predate it).
    A None here must never cause a candidate to be dropped -- treat "no
    parseable month" as "keep and decide after fetching".
    """
    m = _URL_YM_RE.search(canonical_url_key(url))
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not (1 <= month <= 12):
        return None
    return year, month


def slug_date_key(url: str, date_str: str) -> str:
    """Full-strength rename-proof key. Requires a known speech Date."""
    slug = url_slug(url)
    day = (date_str or '').strip()
    if not slug or not day:
        return ''
    return f'{slug}{_DELIM}{day}'


def slug_ym_key(url: str, date_str: str = '') -> str:
    """Pre-fetch key: (slug, year, month), derived from the URL alone.

    date_str is an optional fallback for building the corpus-side set, where
    the Date is known but the URL may be the legacy undated form.
    """
    slug = url_slug(url)
    if not slug:
        return ''
    ym = url_year_month(url)
    if ym is None:
        day = (date_str or '').strip()
        if len(day) >= 7 and day[4] == '-':
            try:
                ym = (int(day[:4]), int(day[5:7]))
            except ValueError:
                return ''
        else:
            return ''
    return f'{slug}{_DELIM}{ym[0]:04d}-{ym[1]:02d}'


# ── Live-URL rename map ──────────────────────────────────────────────────────
# Host-scoped ON PURPOSE. office-of-the-spokesperson appears on 565 Rubio rows
# at www.state.gov AND on 16 Blinken rows at 2021-2025.state.gov -- a frozen
# archive host that still serves the old path. An unscoped rewrite would
# corrupt those 16 rows.
#
# Hardcoded rather than derived: following redirects would need 17,617 HTTP
# requests on the critical path of a weekly job, and state.gov 403s any
# non-browser client anyway. One line per rename event, auditable in review.
# A rename that is NOT in this map shows up as a loud duplicate-detection
# failure in the verifier, not as silently published duplicate rows.
LIVE_HOST = 'state.gov'  # canonical_url_key has already stripped "www."

PATH_RENAMES: tuple[tuple[str, str], ...] = (
    # Observed between the 2026-09-14 and 2026-09-21 weekly runs.
    ('/releases/office-of-the-spokesperson/', '/releases/office-of-the-spokesman/'),
)


def canonicalize_live_url(url: str) -> str:
    """Map a stored URL to the page's current live location.

    Returns the URL unchanged when the host is not the live www.state.gov, so
    frozen archive hosts (2021-2025.state.gov, 2017-2021.state.gov, ...) keep
    the paths they actually serve.

    Idempotent: applying it twice gives the same result as applying it once.
    """
    raw = (url or '').strip()
    if not raw:
        return ''
    key = canonical_url_key(raw)
    host = key.split('/', 1)[0] if key else ''
    if host != LIVE_HOST:
        return raw
    out = raw
    for old, new in PATH_RENAMES:
        if old in out:
            out = out.replace(old, new)
    return out
