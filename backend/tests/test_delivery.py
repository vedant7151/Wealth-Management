"""Phase 7 delivery tests: PDF rendering (non-empty, contains the
disclaimer) and the email-delivery route's disabled-by-default / approved-
only / allowlist / idempotency gates. No real email provider is ever
called (EMAIL_DELIVERY_ENABLED defaults to false, and tests that enable it
monkeypatch delivery.send_email directly)."""

import os

os.environ.setdefault("MOCK_DB", "1")

import delivery  # noqa: E402
from tests.conftest import FakeBriefing, FakeClientProfile  # noqa: E402


def test_render_briefing_pdf_is_nonempty_and_contains_disclaimer():
    briefing = {
        "content": "## Macro View\nMarkets were mixed today.",
        "status": "approved",
        "createdAt": "2026-11-12T00:00:00Z",
        "citations": [{"url": "https://example.com/a", "title": "Example Article"}],
    }
    pdf_bytes = delivery.render_briefing_pdf(briefing, "Jane Client")

    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 200
    assert pdf_bytes.startswith(b"%PDF")
    # The disclaimer text is embedded as plain content streams inside the
    # PDF; reportlab doesn't compress text streams by default, so a
    # substring of it is findable directly in the raw bytes.
    assert b"informational purposes only" in pdf_bytes


def test_recipient_domain_allowlist(monkeypatch):
    monkeypatch.setenv("EMAIL_ALLOWED_RECIPIENT_DOMAINS", "ourfirm.com, partner.org")
    assert delivery.recipient_domain_allowed("advisor@ourfirm.com") is True
    assert delivery.recipient_domain_allowed("someone@partner.org") is True
    assert delivery.recipient_domain_allowed("someone@gmail.com") is False
    assert delivery.recipient_domain_allowed("not-an-email") is False


def test_email_delivery_disabled_by_default_501(api_client, fake_db, monkeypatch):
    monkeypatch.delenv("EMAIL_DELIVERY_ENABLED", raising=False)
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    fake_db.briefing._rows.append(FakeBriefing("b1", "c1", status="approved"))
    client = api_client(user_id="owner-A")

    res = client.post("/api/briefings/b1/deliver", json={"recipient": "advisor@ourfirm.com"})
    assert res.status_code == 501


def test_email_delivery_requires_approved_status(api_client, fake_db, monkeypatch):
    monkeypatch.setenv("EMAIL_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("EMAIL_ALLOWED_RECIPIENT_DOMAINS", "ourfirm.com")
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    fake_db.briefing._rows.append(FakeBriefing("b1", "c1", status="needs_human_review"))
    client = api_client(user_id="owner-A")

    res = client.post("/api/briefings/b1/deliver", json={"recipient": "advisor@ourfirm.com"})
    assert res.status_code == 400
    assert "approved" in res.json()["detail"]


def test_email_delivery_rejects_non_allowlisted_domain(api_client, fake_db, monkeypatch):
    monkeypatch.setenv("EMAIL_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("EMAIL_ALLOWED_RECIPIENT_DOMAINS", "ourfirm.com")
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    fake_db.briefing._rows.append(FakeBriefing("b1", "c1", status="approved"))
    client = api_client(user_id="owner-A")

    res = client.post("/api/briefings/b1/deliver", json={"recipient": "advisor@gmail.com"})
    assert res.status_code == 400
    assert "allowlist" in res.json()["detail"]


def test_email_delivery_is_idempotent_per_briefing_and_recipient(api_client, fake_db, monkeypatch):
    import api

    monkeypatch.setenv("EMAIL_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("EMAIL_ALLOWED_RECIPIENT_DOMAINS", "ourfirm.com")
    monkeypatch.setattr(api.delivery, "send_email", lambda *a, **k: delivery.DeliveryResult(sent=True))
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    fake_db.briefing._rows.append(FakeBriefing("b1", "c1", status="approved"))
    client = api_client(user_id="owner-A")

    first = client.post("/api/briefings/b1/deliver", json={"recipient": "advisor@ourfirm.com"})
    second = client.post("/api/briefings/b1/deliver", json={"recipient": "advisor@ourfirm.com"})

    assert first.status_code == 200
    assert first.json() == {"sent": True}
    assert second.status_code == 200
    assert second.json()["already_sent"] is True
    assert len(fake_db.deliverylog._rows) == 1


def test_pdf_download_is_owner_only(api_client, fake_db):
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    fake_db.briefing._rows.append(FakeBriefing("b1", "c1", status="compliant"))
    client = api_client(user_id="attacker-B")

    res = client.get("/api/briefings/b1/pdf")
    assert res.status_code == 404


def test_pdf_download_returns_pdf_bytes(api_client, fake_db):
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    fake_db.briefing._rows.append(FakeBriefing("b1", "c1", content="Hello briefing.", status="compliant"))
    client = api_client(user_id="owner-A")

    res = client.get("/api/briefings/b1/pdf")

    assert res.status_code == 200
    assert res.headers["content-type"] == "application/pdf"
    assert res.content.startswith(b"%PDF")
