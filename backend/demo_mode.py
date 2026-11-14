"""Phase 8 demo mode: serves pre-recorded REAL run outputs instead of a
live graph run, either because `DEMO_MODE` forces it (so a reviewer can
run `docker compose --profile local up` with no API keys at all and see a
working app) or because the daily LLM budget is exhausted at run-creation
time (so a live demo never shows a raw error -- see api.py's `run_briefing`).

The fixtures in `backend/fixtures/demo_briefings.json` are copied
verbatim from genuine prior runs already present in this repo/session --
`backend/output.txt` (a real `run_seeded.py` run) and the briefing in
`frontend/src/data/sample.ts` (itself documented there as mirroring real
output) -- never invented text. See that file's header comment for exact
provenance of each entry.

`DEMO_MODE` values:
  - "true"  -> always demo mode, regardless of API keys/budget.
  - "false" -> never demo mode (quota exhaustion still falls back per-run
               via api.py's explicit check, independent of this flag).
  - "auto" (default) -> demo mode is active only when GEMINI_API_KEY is
               unset/empty, which is exactly the state docker-compose's
               `local` profile is in with no `.env` configured.
"""

import hashlib
import itertools
import json
import os
from pathlib import Path
from typing import Optional

_FIXTURES_PATH = Path(__file__).parent / "fixtures" / "demo_briefings.json"
_fixtures_cache: Optional[list] = None
_fixture_cycle = None


def _load_fixtures() -> list:
    global _fixtures_cache, _fixture_cycle
    if _fixtures_cache is None:
        with open(_FIXTURES_PATH, encoding="utf-8") as f:
            _fixtures_cache = json.load(f)
        _fixture_cycle = itertools.cycle(range(len(_fixtures_cache)))
    return _fixtures_cache


def is_force_demo_mode() -> bool:
    """True only when DEMO_MODE=true is explicitly set -- NOT the "auto,
    no key configured" case (that's checked separately by
    `should_use_demo_data` and callers that need the distinction, e.g.
    GET /healthz reporting the configured mode honestly)."""
    return os.environ.get("DEMO_MODE", "auto").strip().lower() == "true"


def configured_mode() -> str:
    """The raw DEMO_MODE setting, normalized to one of true/false/auto --
    for GET /healthz, which reports configuration, not a live decision."""
    raw = os.environ.get("DEMO_MODE", "auto").strip().lower()
    return raw if raw in ("true", "false", "auto") else "auto"


def should_use_demo_data() -> bool:
    """True when a `/run` call should be served from a fixture instead of
    the real graph: DEMO_MODE=true, or DEMO_MODE=auto (default) with no
    GEMINI_API_KEY configured. DEMO_MODE=false disables this unconditionally
    -- quota-exhaustion fallback (a separate, narrower decision) is checked
    explicitly by the caller, not through this function."""
    mode = configured_mode()
    if mode == "true":
        return True
    if mode == "false":
        return False
    return not os.environ.get("GEMINI_API_KEY", "").strip()


def pick_fixture(seed: Optional[str] = None) -> dict:
    """Returns one demo fixture dict ({content, citations, status,
    complianceViolations, source}). With `seed` (e.g. a client id), the
    same client consistently gets the same fixture across repeated demo
    runs rather than a random one each time; without it, fixtures are
    handed out round-robin."""
    fixtures = _load_fixtures()
    if not fixtures:
        raise RuntimeError("No demo fixtures available -- backend/fixtures/demo_briefings.json is empty")
    if seed:
        # hashlib (not the built-in hash()) so this is stable across
        # process restarts, not just within one process's lifetime --
        # Python's str hash() is salted per-process by default.
        digest = hashlib.md5(seed.encode("utf-8"), usedforsecurity=False).hexdigest()  # fixture pick, not security
        return fixtures[int(digest, 16) % len(fixtures)]
    return fixtures[next(_fixture_cycle)]
