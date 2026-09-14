"""Allow ``python -m motionless``."""

from __future__ import annotations

import sys

from motionless.cli import main

if __name__ == "__main__":
    sys.exit(main())
