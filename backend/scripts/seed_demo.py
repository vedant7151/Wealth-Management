"""Tracked, idempotent demo-data seed (Phase 8) -- replaces the old,
gitignored `seed.py`/`run_seeded.py` for the purpose of "a fresh clone
works": creates 2 unowned (clerkUserId=None) demo `ClientProfile` rows
with realistic portfolio themes, and one `Briefing` per client built from
the same genuine pre-recorded real-run fixtures demo mode uses
(`backend/fixtures/demo_briefings.json` -- see that file and
`backend/demo_mode.py` for provenance; never invented text), so a fresh
`docker compose --profile local up` (or any fresh DB) has visible content
immediately, without needing to click "Run" first.

Only 2 demo clients, not 3: this repo has exactly 2 genuine pre-recorded
real-run outputs saved (`backend/output.txt` and the briefing in
`frontend/src/data/sample.ts`) -- a 3rd client would need either
duplicating one of those two or inventing new text, and this script does
neither (see docs/PROGRESS.md's Phase 8 log for this call).

Idempotent: matched by name among unowned (clerkUserId=None) rows; a
client that already exists (by name) is left untouched, including its
existing briefings -- re-running this script never creates duplicates.

Usage:
    python scripts/seed_demo.py [--dry-run]
"""

import argparse
import json
import sys
from pathlib import Path

from prisma import Json, Prisma

DEMO_CLIENTS = [
    {
        "name": "Jordan Chen",
        "portfolio_themes": ["Semiconductors", "AI Infrastructure", "Clean Energy"],
        "fixture_index": 0,
    },
    {
        "name": "Morgan Lee",
        "portfolio_themes": ["Semiconductor Supply Chain", "Industrial Logistics"],
        "fixture_index": 1,
    },
]

_FIXTURES_PATH = Path(__file__).parent.parent / "fixtures" / "demo_briefings.json"


def _load_fixtures() -> list:
    with open(_FIXTURES_PATH, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print what would be created without writing anything.")
    args = parser.parse_args()

    fixtures = _load_fixtures()

    db = Prisma()
    db.connect()
    try:
        created_clients = 0
        created_briefings = 0

        for spec in DEMO_CLIENTS:
            existing = db.clientprofile.find_many(where={"name": spec["name"], "clerkUserId": None})
            if existing:
                print(f"Skipping {spec['name']!r} -- already exists ({existing[0].id}).")
                client = existing[0]
            else:
                if args.dry_run:
                    print(f"Would create client {spec['name']!r} with themes {spec['portfolio_themes']}.")
                    continue
                client = db.clientprofile.create(
                    data={"name": spec["name"], "portfolio_themes": spec["portfolio_themes"], "clerkUserId": None}
                )
                created_clients += 1
                print(f"Created client {spec['name']!r} ({client.id}).")

            has_briefing = db.briefing.find_first(where={"clientProfileId": client.id})
            if has_briefing:
                print(f"  Skipping seed briefing for {spec['name']!r} -- already has at least one.")
                continue

            fixture = fixtures[spec["fixture_index"]]
            if args.dry_run:
                print(f"  Would create one seed briefing for {spec['name']!r} from fixture {fixture['source']!r}.")
                continue

            briefing = db.briefing.create(
                data={
                    "content": fixture["content"],
                    "clientProfileId": client.id,
                    "status": fixture.get("status", "compliant"),
                    "complianceViolations": Json(fixture.get("complianceViolations", [])),
                    "isDemo": True,
                }
            )
            for cit in fixture.get("citations", []):
                db.citation.create(
                    data={
                        "url": cit.get("url"),
                        "title": cit.get("title"),
                        "publishedDate": cit.get("publishedDate"),
                        "briefingId": briefing.id,
                    }
                )
            created_briefings += 1
            print(f"  Seeded one briefing for {spec['name']!r} from {fixture['source']!r}.")

        if args.dry_run:
            print("Dry run: no changes made.")
        else:
            print(f"Done: {created_clients} client(s), {created_briefings} briefing(s) created this run.")
        return 0
    finally:
        db.disconnect()


if __name__ == "__main__":
    sys.exit(main())
