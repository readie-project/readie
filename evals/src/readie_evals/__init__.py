"""An LLM-driven evaluation harness for Readie.

The agent freshly fetches code from Kaggle, HuggingFace and its own
generation, adapts each snippet into a self-contained, CPU-only, picklable remote
function, runs it through the ``readie`` client, and records the workload's
imports and execution time. The harness is resumable: fetching and running can be
paused and re-invoked, and each picks up where it left off.
"""

from __future__ import annotations

__version__ = "0.1.0"
