"""Phase 10: Clerk JWT verification -- forged / expired / wrong-issuer /
wrong-audience / alg-confusion tokens, JWKS caching + refresh-on-kid-miss,
and 401-vs-403 semantics. No network: the JWKS fetch is injected."""

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

import auth

ISSUER = "https://test.clerk.accounts.dev"
JWKS_URL = "https://test.clerk.accounts.dev/.well-known/jwks.json"


def _new_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(private_key, kid):
    pub = private_key.public_key()
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(pub))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return jwk


KEY_A = _new_key()
KEY_ROTATED = _new_key()
KEY_ATTACKER = _new_key()


def _token(key=KEY_A, kid="kid-a", **claims):
    now = int(time.time())
    payload = {"sub": "user_123", "iss": ISSUER, "iat": now, "exp": now + 300, **claims}
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def jwks(monkeypatch):
    """Configured Clerk + an injectable, call-counting JWKS 'server'."""
    monkeypatch.setenv("CLERK_JWKS_URL", JWKS_URL)
    monkeypatch.setenv("CLERK_ISSUER", ISSUER)
    for var in ("CLERK_AUDIENCE", "CLERK_AUTHORIZED_PARTIES", "ADMIN_USER_IDS", "ADMIN_ROLE_CLAIM"):
        monkeypatch.delenv(var, raising=False)
    state = {"keys": [_jwk(KEY_A, "kid-a")], "fetches": 0, "down": False}

    def fetch(url):
        state["fetches"] += 1
        if state["down"]:
            raise ConnectionError("jwks unreachable")
        return {"keys": state["keys"]}

    monkeypatch.setattr(auth, "_jwks", auth.JWKSCache(fetch=fetch))
    return state


def _status(token):
    try:
        auth.verify_token(token)
        return 200
    except HTTPException as e:
        return e.status_code


# ------------------------------ accept path ------------------------------


def test_valid_token_is_accepted(jwks):
    assert auth.verify_token(_token())["sub"] == "user_123"


# ---------------------------- reject: forged etc --------------------------


def test_token_signed_by_attacker_with_known_kid_is_rejected(jwks):
    assert _status(_token(key=KEY_ATTACKER, kid="kid-a")) == 401


def test_token_with_unknown_kid_is_rejected(jwks):
    assert _status(_token(key=KEY_ATTACKER, kid="kid-evil")) == 401


def test_expired_token_is_rejected(jwks):
    assert _status(_token(exp=int(time.time()) - 3600)) == 401


def test_leeway_tolerates_small_clock_skew_but_not_large(jwks, monkeypatch):
    monkeypatch.setenv("CLERK_LEEWAY_SECONDS", "10")
    assert _status(_token(exp=int(time.time()) - 5)) == 200
    assert _status(_token(exp=int(time.time()) - 60)) == 401


def test_not_yet_valid_token_is_rejected(jwks):
    assert _status(_token(nbf=int(time.time()) + 3600)) == 401


def test_wrong_issuer_is_rejected(jwks):
    assert _status(_token(iss="https://evil.example.com")) == 401


def test_missing_issuer_is_rejected(jwks):
    assert _status(_token(iss=None)) == 401


def test_missing_exp_or_sub_is_rejected(jwks):
    assert _status(_token(exp=None)) == 401
    assert _status(_token(sub=None)) == 401


def test_audience_enforced_only_when_configured(jwks, monkeypatch):
    assert _status(_token(aud="anything")) == 200  # not configured -> not checked
    monkeypatch.setenv("CLERK_AUDIENCE", "wealth-api")
    assert _status(_token(aud="wealth-api")) == 200
    assert _status(_token(aud="other-api")) == 401
    assert _status(_token()) == 401  # no aud claim at all


def test_authorized_party_enforced_when_configured(jwks, monkeypatch):
    monkeypatch.setenv("CLERK_AUTHORIZED_PARTIES", "https://app.example.com")
    assert _status(_token(azp="https://app.example.com")) == 200
    assert _status(_token(azp="https://evil.example.com")) == 401
    assert _status(_token()) == 401


def test_alg_none_token_is_rejected(jwks):
    forged = jwt.encode({"sub": "admin", "iss": ISSUER, "exp": int(time.time()) + 300}, key=None, algorithm="none")
    assert _status(forged) == 401


def test_hs256_key_confusion_is_rejected(jwks):
    """Classic attack: sign with HS256 using the (public) RSA key bytes as
    the HMAC secret. Modern PyJWT refuses to even CREATE that token, so it
    is hand-built here; the verifier's RS256-only allow-list must reject it
    before any key lookup."""
    import base64
    import hashlib
    import hmac

    from cryptography.hazmat.primitives import serialization

    def b64(b: bytes) -> bytes:
        return base64.urlsafe_b64encode(b).rstrip(b"=")

    pub_pem = KEY_A.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "kid-a"}).encode())
    body = b64(json.dumps({"sub": "admin", "iss": ISSUER, "exp": int(time.time()) + 300}).encode())
    signing_input = header + b"." + body
    sig = b64(hmac.new(pub_pem, signing_input, hashlib.sha256).digest())
    assert _status((signing_input + b"." + sig).decode()) == 401


def test_garbage_token_is_rejected(jwks):
    assert _status("not-a-jwt") == 401
    assert _status("a.b.c") == 401


def test_401_body_does_not_leak_the_reason(jwks):
    with pytest.raises(HTTPException) as exc:
        auth.verify_token(_token(iss="https://evil.example.com"))
    assert exc.value.detail == "Invalid or expired token"
    assert exc.value.headers["WWW-Authenticate"] == "Bearer"


# ------------------------------ JWKS handling -----------------------------


def test_jwks_is_cached_across_requests(jwks):
    for _ in range(5):
        assert _status(_token()) == 200
    assert jwks["fetches"] == 1


def test_key_rotation_refreshes_on_kid_miss(jwks, monkeypatch):
    monkeypatch.setenv("CLERK_JWKS_REFRESH_MIN_INTERVAL_SECONDS", "1")
    assert _status(_token()) == 200
    jwks["keys"] = [_jwk(KEY_A, "kid-a"), _jwk(KEY_ROTATED, "kid-b")]
    auth._jwks._last_refresh_attempt -= 5  # pretend the last refresh was a while ago
    assert _status(_token(key=KEY_ROTATED, kid="kid-b")) == 200
    assert jwks["fetches"] == 2


def test_forged_kid_flood_cannot_hammer_the_jwks_endpoint(jwks, monkeypatch):
    monkeypatch.setenv("CLERK_JWKS_REFRESH_MIN_INTERVAL_SECONDS", "3600")
    _status(_token())  # prime the cache (1 fetch)
    for i in range(50):
        assert _status(_token(key=KEY_ATTACKER, kid=f"fake-{i}")) == 401
    assert jwks["fetches"] == 1  # unknown-kid refreshes are rate limited


def test_jwks_outage_with_warm_cache_still_verifies(jwks):
    assert _status(_token()) == 200
    jwks["down"] = True
    auth._jwks._fetched_at = 0  # force "stale" so a refresh is attempted and fails
    assert _status(_token()) == 200  # serves cached keys


def test_jwks_outage_with_cold_cache_fails_closed_503(jwks):
    jwks["down"] = True
    assert _status(_token()) == 503


# --------------------------- dependency + roles ---------------------------


def test_demo_auth_when_clerk_unconfigured(monkeypatch):
    monkeypatch.delenv("CLERK_JWKS_URL", raising=False)
    monkeypatch.delenv("CLERK_ISSUER", raising=False)
    assert auth.get_current_user(request=None, authorization=None) == auth.DEMO_USER_ID


def test_get_current_user_requires_bearer_scheme(jwks):
    with pytest.raises(HTTPException) as exc:
        auth.get_current_user(request=None, authorization=None)
    assert exc.value.status_code == 401
    with pytest.raises(HTTPException) as exc:
        auth.get_current_user(request=None, authorization="Basic abc")
    assert exc.value.status_code == 401
    assert auth.get_current_user(request=None, authorization=f"Bearer {_token()}") == "user_123"
    assert auth.get_current_user(request=None, authorization=f"bearer {_token()}") == "user_123"


def test_admin_by_id_allowlist_or_signed_role_claim(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "user_admin")
    assert auth.is_admin("user_admin")
    assert not auth.is_admin("user_x")
    assert auth.is_admin("user_x", {"role": "admin"})
    assert auth.is_admin("user_x", {"role": "ADMIN"})
    assert not auth.is_admin("user_x", {"role": "member"})
    monkeypatch.setenv("ADMIN_ROLE_CLAIM", "metadata.role")
    assert auth.is_admin("user_x", {"metadata": {"role": "admin"}})
    assert not auth.is_admin("user_x", {"role": "admin"})  # different claim path
    monkeypatch.setenv("ADMIN_ROLE_CLAIM", "roles")
    assert auth.is_admin("user_x", {"roles": ["viewer", "admin"]})


def test_authenticated_non_admin_gets_403_not_401(api_client, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "someone-else")
    res = api_client(user_id="user_x").get("/api/admin/usage")
    assert res.status_code == 403
    assert res.json()["error"] == "forbidden"


def test_end_to_end_via_http_forged_and_valid_tokens(jwks, api_client, fake_db):
    """Real dependency (no override) through the real FastAPI stack."""
    client = api_client(user_id=None)
    assert client.get("/api/clients").status_code == 401
    forged = _token(key=KEY_ATTACKER, kid="kid-a")
    res = client.get("/api/clients", headers={"Authorization": f"Bearer {forged}"})
    assert res.status_code == 401 and res.headers["www-authenticate"] == "Bearer"
    expired = _token(exp=int(time.time()) - 3600)
    assert client.get("/api/clients", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    wrong_iss = _token(iss="https://evil.example.com")
    assert client.get("/api/clients", headers={"Authorization": f"Bearer {wrong_iss}"}).status_code == 401
    assert client.get("/api/clients", headers={"Authorization": f"Bearer {_token()}"}).status_code == 200


def test_signed_admin_role_claim_grants_admin_route_via_http(jwks, api_client, fake_db, monkeypatch):
    client = api_client(user_id=None)
    tok = _token(role="admin")
    res = client.get("/api/admin/usage", headers={"Authorization": f"Bearer {tok}"})
    assert res.status_code == 200  # FakeDB.query_raw returns canned rows
    plain = _token()
    assert client.get("/api/admin/usage", headers={"Authorization": f"Bearer {plain}"}).status_code == 403
    # An UNSIGNED/forged role claim is worthless: signature check fails first.
    forged = _token(key=KEY_ATTACKER, kid="kid-a", role="admin")
    assert client.get("/api/admin/usage", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
