"""Retention purge CLI. DRY RUN by default: prints how many rows WOULD be
deleted per table. Pass --execute to actually delete.

    python scripts/purge_old_data.py            # dry run (read-only counts)
    python scripts/purge_old_data.py --execute  # delete

Only DocumentChunk / SearchCache / ApiCallLog / RateLimitHit are ever
touched (see retention.py). Run from backend/."""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import retention  # noqa: E402
import settings as settings_module  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="actually delete (default: dry run)")
    args = ap.parse_args()

    s = settings_module.load_or_exit()
    db = settings_module.make_prisma()
    db.connect()
    try:
        report = retention.purge(db, s, dry_run=not args.execute)
    finally:
        db.disconnect()
    mode = "EXECUTED" if args.execute else "DRY RUN (nothing deleted)"
    print(mode)
    for table, n in report.items():
        print(f"  {table}: {n}")
    return 1 if any(n < 0 for n in report.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
