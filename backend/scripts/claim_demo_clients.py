"""Idempotent, non-destructive: assigns ClientProfile rows that currently
have clerkUserId=None to a given real Clerk user id, so legacy/seeded demo
clients stop being globally visible to every demo-mode request and become
owned by a real tenant instead.

Only UPDATEs rows that are currently NULL; never touches a row that
already has an owner, never deletes anything. Safe to re-run -- a second
run finds nothing left to claim and does nothing.

Usage:
    python scripts/claim_demo_clients.py <clerk_user_id> [--dry-run]
"""

import argparse
import sys

from prisma import Prisma


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clerk_user_id", help="The real Clerk user id (JWT 'sub') to assign unowned clients to.")
    parser.add_argument("--dry-run", action="store_true", help="List what would change without writing anything.")
    args = parser.parse_args()

    db = Prisma()
    db.connect()
    try:
        unowned = db.clientprofile.find_many(where={"clerkUserId": None})
        print(f"Found {len(unowned)} unowned (clerkUserId=None) client row(s).")
        for c in unowned:
            print(f"  - {c.id}: {c.name!r}")

        if not unowned:
            return 0

        if args.dry_run:
            print("Dry run: no changes made. Re-run without --dry-run to apply.")
            return 0

        for c in unowned:
            db.clientprofile.update(where={"id": c.id}, data={"clerkUserId": args.clerk_user_id})

        print(f"Assigned {len(unowned)} client row(s) to clerkUserId={args.clerk_user_id}.")
        return 0
    finally:
        db.disconnect()


if __name__ == "__main__":
    sys.exit(main())
