"""Phase 10: centralized settings, fail-fast validation, dev/prod profiles."""

import pytest

import settings as S

SECRET = "postgresql://user:" + "SUPERSECRETPW@host/db"

PROD_OK = {
    "APP_ENV": "prod",
    "DATABASE_URL": SECRET,
    "CORS_ORIGINS": "https://app.example.com",
    "CLERK_JWKS_URL": "https://x.clerk.accounts.dev/.well-known/jwks.json",
    "CLERK_ISSUER": "https://x.clerk.accounts.dev",
    "GEMINI_API_KEY": "k1",
    "TAVILY_API_KEY": "k2",
    "GROQ_API_KEY": "k3",
    "HUGGINGFACE_API_KEY": "k4",
    "METRICS_TOKEN": "tok",
    "ADMIN_USER_IDS": "user_admin1",
}


def test_dev_defaults_are_permissive():
    s = S.load_settings({"MOCK_DB": "1"})
    assert s.app_env == "dev" and s.is_prod is False
    assert s.auth_configured is False  # demo auth is allowed in dev


def test_valid_prod_profile_loads():
    s = S.load_settings(PROD_OK)
    assert s.is_prod and s.auth_configured


@pytest.mark.parametrize(
    "override,expected_name",
    [
        ({"CORS_ORIGINS": "*"}, "CORS_ORIGINS"),
        ({"CORS_ORIGINS": "http://evil.example.com"}, "CORS_ORIGINS"),
        ({"DEBUG": "true"}, "DEBUG"),
        ({"CLERK_JWKS_URL": "", "CLERK_ISSUER": ""}, "CLERK_JWKS_URL/CLERK_ISSUER"),
        ({"METRICS_TOKEN": ""}, "METRICS_TOKEN"),
        ({"GEMINI_API_KEY": ""}, "GEMINI_API_KEY"),
        ({"DATABASE_URL": ""}, "DATABASE_URL"),
        ({"LOG_LEVEL": "DEBUG"}, "LOG_LEVEL"),
        ({"ADMIN_USER_IDS": ""}, "ADMIN_USER_IDS"),
    ],
)
def test_prod_refuses_unsafe_config(override, expected_name):
    with pytest.raises(S.SettingsError) as exc:
        S.load_settings({**PROD_OK, **override})
    assert expected_name in str(exc.value)


def test_hf_key_only_required_when_hf_is_the_embedding_provider():
    env = {k: v for k, v in PROD_OK.items() if k != "HUGGINGFACE_API_KEY"}
    assert S.load_settings(env).is_prod  # default embedding provider is gemini
    with pytest.raises(S.SettingsError) as exc:
        S.load_settings({**env, "EMBEDDING_PROVIDER": "huggingface"})
    assert "HUGGINGFACE_API_KEY" in str(exc.value)


def test_prod_demo_mode_true_does_not_require_provider_keys():
    env = {k: v for k, v in PROD_OK.items() if not k.endswith("_API_KEY")}
    assert S.load_settings({**env, "DEMO_MODE": "true"}).is_prod


def test_errors_list_names_never_values():
    env = {**PROD_OK, "CORS_ORIGINS": "*", "RUN_LIMIT": "not-a-number", "DEBUG": "true"}
    with pytest.raises(S.SettingsError) as exc:
        S.load_settings(env)
    msg = str(exc.value)
    assert "RUN_LIMIT" in msg
    for leaked in ("SUPERSECRETPW", "not-a-number", "k1", "tok"):
        assert leaked not in msg


def test_invalid_types_and_ranges_are_rejected():
    for bad in ({"RUN_LIMIT": "0"}, {"DB_POOL_SIZE": "x"}, {"APP_ENV": "staging"}, {"LOG_LEVEL": "loud"}):
        with pytest.raises(S.SettingsError):
            S.load_settings({"MOCK_DB": "1", **bad})


def test_half_configured_clerk_is_rejected():
    with pytest.raises(S.SettingsError) as exc:
        S.load_settings({"MOCK_DB": "1", "CLERK_ISSUER": "https://x.clerk.accounts.dev"})
    assert "CLERK_JWKS_URL" in str(exc.value)


def test_langsmith_project_is_per_environment():
    assert S.load_settings({"MOCK_DB": "1"}).langsmith_project == "wealth-agent-dev"
    assert S.load_settings({"MOCK_DB": "1", "LANGCHAIN_PROJECT": "acme"}).langsmith_project == "acme-dev"
    assert S.load_settings(PROD_OK).langsmith_project == "wealth-agent-prod"
    already = S.load_settings({"MOCK_DB": "1", "LANGCHAIN_PROJECT": "acme-dev"})
    assert already.langsmith_project == "acme-dev"


def test_database_url_pool_params_added_without_clobbering():
    out = S.database_url_with_pool("postgresql://u:p@h/db?sslmode=require&connection_limit=2", 5, 10, 7)
    assert "connection_limit=2" in out  # operator's explicit value wins
    assert "pool_timeout=10" in out and "connect_timeout=7" in out and "sslmode=require" in out
    assert "pgbouncer" not in out


def test_neon_pooler_host_gets_pgbouncer_flag():
    out = S.database_url_with_pool("postgresql://u:p@ep-x-pooler.eu.neon.tech/db", 5, 10, 10)
    assert "pgbouncer=true" in out


def test_trace_payloads_hidden_by_default_only_in_prod(monkeypatch):
    assert S.load_settings({"MOCK_DB": "1"}).hide_trace_payloads is False
    assert S.load_settings(PROD_OK).hide_trace_payloads is True
    assert S.load_settings({**PROD_OK, "TRACE_HIDE_PAYLOADS": "false"}).hide_trace_payloads is False
    # setenv first so monkeypatch restores (removes) these after the test --
    # apply_runtime_env writes into the real process environment.
    for k in (
        "LANGSMITH_HIDE_INPUTS",
        "LANGCHAIN_HIDE_INPUTS",
        "LANGSMITH_HIDE_OUTPUTS",
        "LANGCHAIN_HIDE_OUTPUTS",
        "LANGCHAIN_PROJECT",
    ):
        monkeypatch.setenv(k, "")
    S.apply_runtime_env(S.load_settings(PROD_OK))
    import os

    assert os.environ["LANGSMITH_HIDE_INPUTS"] == "true" and os.environ["LANGCHAIN_HIDE_OUTPUTS"] == "true"
    assert os.environ["LANGCHAIN_PROJECT"] == "wealth-agent-prod"


def test_production_is_an_alias_for_prod_and_unknown_profiles_are_rejected():
    assert S.load_settings({**PROD_OK, "APP_ENV": "production"}).is_prod
    assert S.load_settings({"MOCK_DB": "1", "APP_ENV": "development"}).app_env == "dev"
    with pytest.raises(S.SettingsError):
        S.load_settings({**PROD_OK, "APP_ENV": "prodution"})  # a typo must never fall back to dev
