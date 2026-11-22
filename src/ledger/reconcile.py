"""Command line check that the books balance: ``python -m ledger.reconcile``.

Exit status (so it can be used from cron, CI or a shell ``&&`` chain):

* ``0``  every balance matches the transaction history
* ``1``  at least one discrepancy (printed to stderr)
* ``2``  the check could not run (e.g. no database at ``LEDGER_DB_PATH``)
"""

from __future__ import annotations

import sqlite3
import sys

from ledger import db


def main() -> int:
    try:
        problems = db.reconcile()
    except sqlite3.Error as exc:
        print(f"cannot reconcile {db.DB_PATH}: {exc}", file=sys.stderr)
        return 2
    if problems:
        for problem in problems:
            print(f"MISMATCH: {problem}", file=sys.stderr)
        return 1
    print(f"OK: balances match the transaction history ({db.DB_PATH})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
