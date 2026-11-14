"""Locust load + abuse scenario against scripts/loadtest/mock_server.py.

    locust -f scripts/loadtest/locustfile.py --headless -u 20 -r 10 -t 30s \
           --host http://127.0.0.1:8011 --csv scripts/loadtest/results/s1

Two user types hit the SAME identity (every request comes from 127.0.0.1 in
demo-auth mode), which is exactly what a single abusive client looks like:
  - ReaderUser: realistic read traffic (client list, briefings, run history).
  - AbuserUser: hammers POST /run on the demo clients as fast as it can.

429 (rate limited), 409 (run already in flight) and 503 (capacity) on /run
are EXPECTED outcomes of the abuse controls and are recorded as successes of
the control, not failures. Anything else (500, connection error, a 202 past
the quota) fails the request. Per-status counts are written to
<csv prefix>_status_counts.json on exit.

Set LOADTEST_ABUSERS=0 to run readers only; combine with a very high
GLOBAL_API_LIMIT on the server to measure raw read latency instead of the
limiter."""

import json
import os
import random
from collections import Counter

from locust import HttpUser, between, events, task

CLIENT_IDS = [f"demo-{i}" for i in range(5)]
_counts: Counter = Counter()


@events.request.add_listener
def _count_status(request_type, name, response_time, response_length, response=None, exception=None, **kw):
    status = getattr(response, "status_code", 0) if response is not None else 0
    _counts[f"{request_type} {name} -> {status}"] += 1


@events.quitting.add_listener
def _dump(environment, **kw):
    prefix = environment.parsed_options.csv_prefix or "loadtest"
    path = f"{prefix}_status_counts.json"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(_counts.items())), f, indent=2)


class ReaderUser(HttpUser):
    weight = 8
    wait_time = between(0.1, 0.5)

    def _get(self, path, name):
        with self.client.get(path, name=name, catch_response=True) as r:
            # 429 is the limiter doing its job under single-identity load.
            if r.status_code in (200, 429):
                r.success()
            else:
                r.failure(f"unexpected {r.status_code}")

    @task(3)
    def list_clients(self):
        self._get("/api/clients", "GET /api/clients")

    @task(5)
    def briefings(self):
        self._get(f"/api/clients/{random.choice(CLIENT_IDS)}/briefings?limit=20", "GET /api/clients/{id}/briefings")

    @task(2)
    def run_history(self):
        self._get("/api/runs?limit=20", "GET /api/runs")

    @task(1)
    def healthz(self):
        self._get("/healthz", "GET /healthz")


class AbuserUser(HttpUser):
    weight = int(os.environ.get("LOADTEST_ABUSERS", "2"))
    wait_time = between(0.02, 0.1)

    @task
    def hammer_run(self):
        cid = random.choice(CLIENT_IDS)
        with self.client.post(f"/api/clients/{cid}/run", name="POST /api/clients/{id}/run", catch_response=True) as r:
            if r.status_code in (202, 409, 429, 503):
                r.success()
            else:
                r.failure(f"unexpected {r.status_code}")
