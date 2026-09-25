"""The agent: fetching's counterpart that turns snippets into runnable tasks.

Generation writes fresh code, adaptation rewrites fetched code, and repair fixes a
task that failed to run. All three go through the ``LLM`` seam so tests inject a
fake and never reach the network.
"""

from __future__ import annotations
