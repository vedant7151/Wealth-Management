"""Exercises the REAL Postgres rate-limit SQL under concurrency.

Run manually against a database you are happy to write throwaway rows to
(the default DATABASE_URL in backend/.env). It:
  1. fires N threads at once at a limit-3 key through the strict
     (advisory-lock) path and prints how many were admitted -- must be 3;
  2. does the same through the non-strict single-statement CTE path, as a
     contrast (this path may admit MORE than 3 under contention -- that is
     why `run`/`llm_budget` use the strict path);
  3. races N threads for one per-client run lock -- must be exactly 1 winner;
  4. deletes ONLY the throwaway rows it created (keys are prefixed with a
     fresh random token), never anything else.

No LLM/Tavily/HF/Groq call is made. Not run in CI (needs a database)."""

import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ratelimit  # noqa: E402
import settings as settings_module  # noqa: E402

N_THREADS = int(os.environ.get("VERIFY_THREADS", "12"))
LIMIT = 3


def race(n, fn):
    barrier = threading.Barrier(n)

    def wrapped(i):
        barrier.wait()
        return fn(i)

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(wrapped, range(n)))


def main() -> int:
    settings_module.load_or_exit()
    db = settings_module.make_prisma()
    db.connect()
    token = f"verify-{uuid.uuid4().hex[:12]}"
    store = ratelimit._PostgresStore(lambda: db)
    ok = True
    try:
        strict = race(N_THREADS, lambda i: store.hit(f"{token}:strict", 3600, LIMIT, strict=True).allowed)
        print(f"strict (advisory lock) : {sum(strict)} of {N_THREADS} admitted, limit {LIMIT}")
        ok &= sum(strict) == LIMIT

        loose = race(N_THREADS, lambda i: store.hit(f"{token}:loose", 3600, LIMIT, strict=False).allowed)
        print(f"non-strict single CTE  : {sum(loose)} of {N_THREADS} admitted, limit {LIMIT} (contrast; >3 = race)")

        locks = race(N_THREADS, lambda i: store.try_lock(f"{token}:client", 300))
        print(f"per-client run lock    : {sum(locks)} winner(s) of {N_THREADS} (must be 1)")
        ok &= sum(locks) == 1
    finally:
        removed = db.execute_raw('DELETE FROM "RateLimitHit" WHERE key LIKE $1', f"%{token}%")
        print(f"cleanup: removed {removed} throwaway row(s) (key contains {token})")
        db.disconnect()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
