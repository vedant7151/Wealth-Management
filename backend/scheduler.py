import datetime

import fastapi
import inngest
from agent import app, new_input_state, close_db
from inngest.fast_api import serve

import retention
import runs
import settings as settings_module

# Initialize the Inngest client
inngest_client = inngest.Inngest(app_id="wealth-agent")


def run_agent(profile: dict, run_log_id: str) -> str:
    input_state = new_input_state(profile["id"], profile["themes"], run_id=run_log_id)

    accumulated = dict(input_state)
    timeline = []
    try:
        for output in app.stream(input_state):
            for key, value in output.items():
                print(f"Finished node for {profile['id']}: {key}")
                if value:
                    accumulated.update(value)
                timeline.append({"node": key, "reason": (value or {}).get("reason")})
    finally:
        close_db()

    return accumulated.get("final_briefing", "Failed to generate briefing.")


def _idempotency_key(client_id: str) -> str:
    """One key per client per UTC calendar day, so a cron event that fires
    twice for the same day (Inngest retry, duplicate webhook delivery,
    etc.) is recognized as a repeat instead of generating a second
    briefing for that client on that day."""
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    return f"{client_id}:{today}"


async def run_all_clients(profiles: list, step: inngest.Step, db=None) -> dict:
    """Runs the agent once per client profile via `step.run` (so each
    client's run is independently retried/memoized by Inngest), isolating
    failures per client: one client's agent run raising (a Tavily/Gemini/
    DB error, a bad portfolio_themes value, etc.) must not prevent every
    other client's scheduled briefing from running. Without the try/except
    here, an exception from `step.run()` would propagate out of the loop
    and abort every remaining client for the rest of this scheduled run.

    Each client's run gets its own RunLog row (trigger="cron") before
    `step.run` is invoked, keyed by an idempotency key
    (`clientId:utcDate`) -- if a RunLog with that key already exists in a
    non-failed state (success, or still running), the client is skipped
    rather than run a second time for the same day, which is how this
    guards against a double-fired cron event. A client whose run raises
    gets its RunLog updated to status=failed with the exception recorded
    as errorCode, instead of leaving it stuck at "running".

    Extracted from `generate_morning_briefing` so it's directly testable
    with a fake `step`/`db` object, without needing to drive Inngest's own
    function-call machinery in tests."""
    results = []
    failures = []
    skipped = []

    for p in profiles:
        idempotency_key = _idempotency_key(p["id"])

        if db is not None:
            existing = db.runlog.find_many(where={"idempotencyKey": idempotency_key}, take=1)
            if existing and str(existing[0].status) != "failed":
                skipped.append({"client_id": p["id"], "run_id": existing[0].id})
                continue

        run_log = (
            db.runlog.create(
                data={
                    "clientProfileId": p["id"],
                    "userId": None,
                    "trigger": "cron",
                    "status": "running",
                    "idempotencyKey": idempotency_key,
                }
            )
            if db is not None
            else None
        )
        run_log_id = run_log.id if run_log is not None else idempotency_key

        try:
            briefing = await step.run(f"run_agent_{p['id']}", lambda p=p, rid=run_log_id: run_agent(p, rid))
            results.append(len(briefing))
            if db is not None:
                saved = db.briefing.find_first(where={"runId": run_log_id})
                db.runlog.update(
                    where={"id": run_log_id},
                    data={
                        "status": "success",
                        "finishedAt": datetime.datetime.now(datetime.timezone.utc),
                        "briefingId": saved.id if saved is not None else None,
                    },
                )
        except Exception as e:
            print(f"Client {p['id']} failed: {e}")
            failures.append({"client_id": p["id"], "error": str(e)})
            if db is not None:
                db.runlog.update(
                    where={"id": run_log_id},
                    data={
                        "status": "failed",
                        "finishedAt": datetime.datetime.now(datetime.timezone.utc),
                        "errorCode": type(e).__name__,
                    },
                )

    return {
        "status": "success",
        "briefings_processed": len(results),
        "failures": failures,
        "skipped_idempotent": skipped,
    }


@inngest_client.create_function(
    fn_id="morning-briefing-job",
    name="Generate Morning Briefing",
    trigger=inngest.TriggerCron(cron="0 6 * * 1-5"),
    # Caps Inngest's own step-level retry so one client's persistently
    # failing run doesn't retry indefinitely and delay the rest of this
    # scheduled job's steps -- see docs/inngest-local.md.
    retries=2,
)
async def generate_morning_briefing(ctx: inngest.Context, step: inngest.Step) -> dict:
    db = settings_module.make_prisma()
    db.connect()
    try:

        def fetch_profiles():
            profiles = db.clientprofile.find_many()
            return [{"id": p.id, "themes": p.portfolio_themes} for p in profiles]

        profiles = await step.run("fetch_profiles", fetch_profiles)
        runs.recover_stuck_runs(db)
        return await run_all_clients(profiles, step, db=db)
    finally:
        db.disconnect()


def run_retention(db=None, settings=None) -> dict:
    """Body of the retention cron. A no-op unless RETENTION_ENABLED=true
    (default false) so deploying this function never starts deleting rows
    on its own; see retention.py for exactly what is and is not purged."""
    settings = settings or settings_module.get_settings()
    if not settings.retention_enabled:
        return {"status": "disabled"}
    own_db = db is None
    db = db or settings_module.make_prisma()
    if own_db:
        db.connect()
    try:
        return {"status": "ok", "deleted": retention.purge(db, settings, dry_run=False)}
    finally:
        if own_db:
            db.disconnect()


@inngest_client.create_function(
    fn_id="retention-job",
    name="Purge old operational data",
    trigger=inngest.TriggerCron(cron="30 3 * * *"),
    retries=1,
)
async def retention_job(ctx: inngest.Context, step: inngest.Step) -> dict:
    return await step.run("purge", run_retention)


# Create an ASGI app to serve the Inngest endpoints (FastAPI)
api = fastapi.FastAPI(title="Wealth Agent Webhook Server")
serve(api, inngest_client, [generate_morning_briefing, retention_job])
