"""RETIRED historical one-off queue helper (`sync_capcut_queue.py`).

Importing this module has zero side effects. Running it directly exits with
code 1 and directs operators to the supported Dashboard/API queue controls.
"""
from __future__ import annotations

import sys

RETIRED_REASON = (
    "[RETIRED] backend/sync_capcut_queue.py has been retired because the "
    "historical script overwrote telegram_queue.json with hardcoded URLs and "
    "wrote queue trigger/reorder flags at import time. Use the Dashboard queue "
    "UI or the supported /api/queue endpoints instead."
)


def main(argv: list[str] | None = None) -> int:
    _ = argv
    print(RETIRED_REASON, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
