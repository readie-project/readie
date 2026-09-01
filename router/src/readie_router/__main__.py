"""Allow `python -m readie_router`."""

from __future__ import annotations

import sys

from readie_router.main import main

if __name__ == "__main__":
    sys.exit(main())
