.PHONY: install test lint eval eval-live dev-api dev-web docker-up docker-up-local settings-check lock audit sbom loadtest-server loadtest purge-dry-run

install:
	cd backend && pip install -r requirements.txt && prisma generate
	cd frontend && npm install

test:
	cd backend && python -m py_compile agent.py api.py auth.py scheduler.py
	cd backend && pytest --cov=. --cov-report=term-missing
	cd frontend && npm run test:ci

lint:
	cd backend && python -m py_compile agent.py api.py auth.py scheduler.py
	cd backend && ruff check .
	cd backend && ruff format --check .
	cd backend && python -m mypy compliance api.py ratelimit.py llm_client.py
	cd frontend && npm run lint
	cd frontend && npx tsc -b

eval:
	cd eval && python build_compliance_set.py
	cd eval && python run_compliance_eval.py
	cd eval && python retrieval_eval.py
	cd eval && python faithfulness_eval.py
	cd eval && python generate_results.py
	cd eval && python check_thresholds.py

eval-live:
	cd eval && python build_compliance_set.py
	cd eval && python run_compliance_eval.py --live
	cd eval && python retrieval_eval.py
	cd eval && python faithfulness_eval.py
	cd eval && python generate_results.py
	cd eval && python check_thresholds.py

dev-api:
	cd backend && python -m uvicorn api:app --port 8010 --reload

dev-web:
	cd frontend && npm run dev

docker-up:
	docker compose up --build

docker-up-local:
	docker compose --profile local up --build

# --- Phase 10: production hardening ---------------------------------------
# Validate the current environment exactly as the API does at startup
# (prints variable NAMES only; exit 78 on a bad config).
settings-check:
	cd backend && python settings.py

# Regenerate the fully-pinned transitive lockfile after editing requirements*.txt.
lock:
	cd backend && python -m piptools compile requirements-dev.txt --output-file requirements.lock --no-header --annotation-style=line --strip-extras

audit:
	cd backend && python -m pip_audit -r requirements.lock --no-deps --disable-pip
	cd frontend && npm audit --audit-level=high && npm audit --omit=dev

sbom:
	cd backend && cyclonedx-py requirements requirements.lock --output-format JSON --output-file sbom-backend.cdx.json
	cd frontend && npm sbom --sbom-format cyclonedx --omit dev > ../sbom-frontend.cdx.json

# Mocked-agent load/abuse test (no live provider is ever contacted). Start the
# mock server in one terminal, run locust in another -- see
# backend/scripts/loadtest/README.md for the exact commands and results.
loadtest-server:
	cd backend && python scripts/loadtest/mock_server.py

loadtest:
	cd backend && locust -f scripts/loadtest/locustfile.py --headless -u 20 -r 20 -t 30s --host http://127.0.0.1:8011 --csv scripts/loadtest/results/s1_default_limits --only-summary

# Dry run (read-only counts) of the data-retention purge. Add --execute to delete.
purge-dry-run:
	cd backend && python scripts/purge_old_data.py
