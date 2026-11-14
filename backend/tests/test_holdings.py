"""Phase 7 holdings tests: CSV parsing/validation (backend/holdings.py,
pure, no DB) + the upload/list API routes (fake_db, no real Prisma)."""

import os

os.environ.setdefault("MOCK_DB", "1")

import pytest  # noqa: E402

import holdings as holdings_module  # noqa: E402
from tests.conftest import FakeClientProfile  # noqa: E402


def _csv(text: str) -> bytes:
    return text.encode("utf-8")


def test_parse_valid_csv():
    rows = holdings_module.parse_and_validate_csv(_csv("ticker,sector,weight\nAAPL,Technology,40\nJPM,Financials,30\n"))
    assert [r.ticker for r in rows] == ["AAPL", "JPM"]
    assert rows[0].sector == "Technology"
    assert rows[0].weight == 40.0


def test_parse_rejects_missing_columns():
    with pytest.raises(holdings_module.HoldingsValidationError):
        holdings_module.parse_and_validate_csv(_csv("name,amount\nApple,40\n"))


def test_parse_rejects_bad_ticker():
    with pytest.raises(holdings_module.HoldingsValidationError, match="not a valid ticker"):
        holdings_module.parse_and_validate_csv(_csv("ticker,weight\nnot a ticker!,40\n"))


def test_parse_rejects_non_numeric_weight():
    with pytest.raises(holdings_module.HoldingsValidationError, match="not a number"):
        holdings_module.parse_and_validate_csv(_csv("ticker,weight\nAAPL,not-a-number\n"))


def test_parse_rejects_weight_sum_over_100():
    csv_text = "ticker,weight\nAAPL,60\nJPM,60\n"
    with pytest.raises(holdings_module.HoldingsValidationError, match="exceeds 100"):
        holdings_module.parse_and_validate_csv(_csv(csv_text))


def test_parse_rejects_duplicate_ticker():
    with pytest.raises(holdings_module.HoldingsValidationError, match="duplicate"):
        holdings_module.parse_and_validate_csv(_csv("ticker,weight\nAAPL,10\nAAPL,10\n"))


def test_parse_rejects_formula_injection_in_sector():
    with pytest.raises(holdings_module.HoldingsValidationError, match="formula"):
        holdings_module.parse_and_validate_csv(_csv('ticker,sector,weight\nAAPL,"=cmd()",40\n'))


def test_parse_rejects_oversized_file(monkeypatch):
    monkeypatch.setattr(holdings_module, "CSV_MAX_BYTES", 10)
    with pytest.raises(holdings_module.HoldingsValidationError, match="bytes"):
        holdings_module.parse_and_validate_csv(_csv("ticker,weight\nAAPL,10\n"))


def test_parse_rejects_too_many_rows(monkeypatch):
    monkeypatch.setattr(holdings_module, "CSV_MAX_ROWS", 1)
    with pytest.raises(holdings_module.HoldingsValidationError, match="more than"):
        holdings_module.parse_and_validate_csv(_csv("ticker,weight\nAAPL,10\nMSFT,10\n"))


def test_upload_replaces_existing_holdings(api_client, fake_db):
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    fake_db.holding.create({"clientProfileId": "c1", "ticker": "OLD", "sector": None, "weight": 100})
    client = api_client(user_id="owner-A")

    res = client.post(
        "/api/clients/c1/holdings",
        files={"file": ("holdings.csv", "ticker,sector,weight\nAAPL,Technology,40\n", "text/csv")},
    )

    assert res.status_code == 200
    assert res.json() == {"count": 1}
    remaining = fake_db.holding.find_many(where={"clientProfileId": "c1"})
    assert [h.ticker for h in remaining] == ["AAPL"]


def test_upload_invalid_csv_returns_400_and_leaves_existing_rows(api_client, fake_db):
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    fake_db.holding.create({"clientProfileId": "c1", "ticker": "OLD", "sector": None, "weight": 100})
    client = api_client(user_id="owner-A")

    res = client.post(
        "/api/clients/c1/holdings",
        files={"file": ("holdings.csv", "ticker,weight\nnot a ticker!,40\n", "text/csv")},
    )

    assert res.status_code == 400
    remaining = fake_db.holding.find_many(where={"clientProfileId": "c1"})
    assert [h.ticker for h in remaining] == ["OLD"]


def test_upload_is_owner_only(api_client, fake_db):
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    client = api_client(user_id="attacker-B")

    res = client.post(
        "/api/clients/c1/holdings",
        files={"file": ("holdings.csv", "ticker,weight\nAAPL,40\n", "text/csv")},
    )

    assert res.status_code == 404


def test_list_holdings(api_client, fake_db):
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    fake_db.holding.create({"clientProfileId": "c1", "ticker": "AAPL", "sector": "Technology", "weight": 40})
    client = api_client(user_id="owner-A")

    res = client.get("/api/clients/c1/holdings")

    assert res.status_code == 200
    assert res.json() == [{"id": "holding-1", "ticker": "AAPL", "sector": "Technology", "weight": 40}]
