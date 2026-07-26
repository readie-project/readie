"""Allow `python -m crfs_router`."""

from __future__ import annotations

import sys

from crfs_router.main import main

if __name__ == "__main__":
    sys.exit(main())
