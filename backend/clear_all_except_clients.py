from prisma import Prisma


def clear():
    db = Prisma()
    db.connect()
    try:
        # Citation and Briefing are truncated together in one statement so
        # Postgres handles the Citation -> Briefing foreign key automatically.
        # ClientProfile is intentionally excluded so those rows survive.
        db.execute_raw('TRUNCATE TABLE "Citation", "Briefing", "SearchCache" RESTART IDENTITY;')
        print("Successfully cleared Briefing, Citation, and SearchCache. ClientProfile rows were kept.")
    except Exception as e:
        print(f"Error clearing tables: {e}")
    finally:
        db.disconnect()


if __name__ == "__main__":
    clear()
