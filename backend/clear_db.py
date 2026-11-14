from prisma import Prisma


def clear():
    db = Prisma()
    db.connect()
    try:
        db.execute_raw('TRUNCATE TABLE "SearchCache";')
        print("Successfully cleared all 768-dimensional rows from SearchCache.")
    except Exception as e:
        print(f"Error clearing cache: {e}")
    finally:
        db.disconnect()


if __name__ == "__main__":
    clear()
