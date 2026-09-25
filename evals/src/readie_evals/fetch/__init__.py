"""Fetchers that source real code for a domain.

Each satisfies the ``Fetcher`` protocol and talks to an external platform, so both
are optional (``pip install readie-evals[fetch]``) and imported on use. Their output
is raw, unadapted code; the agent turns it into runnable tasks.
"""

from __future__ import annotations


class FetchError(Exception):
    """A fetcher could not reach its source or its client is not installed."""
