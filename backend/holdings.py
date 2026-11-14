"""CSV holdings upload: parsing + strict validation.

Phase 7 scope. `parse_and_validate_csv` is pure (no DB, no filesystem) so
it's directly unit-testable; `replace_holdings` does the actual
replace-on-upload write, inside a single Prisma `batch_()` transaction so a
client's holdings table is never left half-deleted/half-inserted.

Validation is deliberately strict per the master prompt's extra
requirements:
  - size cap (CSV_MAX_BYTES) checked on the raw bytes BEFORE parsing
  - row cap (CSV_MAX_ROWS)
  - ticker: 1-10 uppercase letters/digits/dots (e.g. "BRK.B")
  - weight: a finite number in [0, 100]; the whole file's weights must sum
    to <= 100 + a small float-rounding tolerance ("weights sum sanity")
  - no formula/CSV-injection payloads in any stored field: a value whose
    first character is one Excel/Sheets would interpret as a formula
    trigger (=, +, -, @, tab, CR) is rejected outright rather than
    silently sanitized, since silently stripping a leading '-' from, say,
    a sector name could change its meaning.

Never reads the uploaded filename for any filesystem path -- the file is
parsed entirely in memory from the upload's bytes."""

import csv
import io
import os
import re
from dataclasses import dataclass
from typing import List, Optional

TICKER_RE = re.compile(r"^[A-Z0-9]{1,6}(?:\.[A-Z]{1,2})?$")
# Leading characters Excel/Sheets/LibreOffice treat as a formula trigger --
# a field starting with one of these is rejected, not sanitized (see
# module docstring for why silent-strip is the wrong call here).
_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@", "\t", "\r")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


CSV_MAX_ROWS = _int_env("CSV_MAX_ROWS", 500)
CSV_MAX_BYTES = _int_env("CSV_MAX_BYTES", 262144)  # 256 KiB


class HoldingsValidationError(ValueError):
    """Raised with a human-readable message naming the offending row/field."""


@dataclass
class HoldingRow:
    ticker: str
    sector: Optional[str]
    weight: float


def _reject_formula_injection(value: str, field: str, row_num: int) -> str:
    if value and value[0] in _FORMULA_TRIGGER_CHARS:
        raise HoldingsValidationError(
            f"Row {row_num}: {field} '{value}' starts with a character that could be interpreted as a "
            f"spreadsheet formula ({value[0]!r}) -- rejected, not sanitized."
        )
    return value


def parse_and_validate_csv(raw: bytes) -> List[HoldingRow]:
    """Parses + validates holdings CSV bytes. Raises HoldingsValidationError
    (never a bare csv.Error/ValueError) on anything invalid, with a message
    identifying the row. Expects a header row with (case-insensitive)
    columns `ticker`, `sector` (optional), `weight`."""
    if len(raw) > CSV_MAX_BYTES:
        raise HoldingsValidationError(f"File is {len(raw)} bytes, exceeding the {CSV_MAX_BYTES}-byte limit.")

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise HoldingsValidationError(f"File is not valid UTF-8 text: {e}") from e

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HoldingsValidationError("CSV has no header row.")

    header_map = {name.strip().lower(): name for name in reader.fieldnames}
    if "ticker" not in header_map or "weight" not in header_map:
        raise HoldingsValidationError("CSV header must include at least 'ticker' and 'weight' columns.")

    rows: List[HoldingRow] = []
    total_weight = 0.0
    seen_tickers = set()

    for row_num, raw_row in enumerate(reader, start=2):  # row 1 is the header
        if row_num - 1 > CSV_MAX_ROWS:
            raise HoldingsValidationError(f"CSV has more than {CSV_MAX_ROWS} data rows.")

        ticker_raw = (raw_row.get(header_map["ticker"]) or "").strip().upper()
        sector_raw = (raw_row.get(header_map.get("sector", "")) or "").strip() if "sector" in header_map else ""
        weight_raw = (raw_row.get(header_map["weight"]) or "").strip()

        if not ticker_raw:
            raise HoldingsValidationError(f"Row {row_num}: ticker is required.")
        _reject_formula_injection(ticker_raw, "ticker", row_num)
        if not TICKER_RE.match(ticker_raw):
            raise HoldingsValidationError(f"Row {row_num}: '{ticker_raw}' is not a valid ticker symbol.")
        if ticker_raw in seen_tickers:
            raise HoldingsValidationError(f"Row {row_num}: duplicate ticker '{ticker_raw}'.")
        seen_tickers.add(ticker_raw)

        if sector_raw:
            _reject_formula_injection(sector_raw, "sector", row_num)
            if len(sector_raw) > 80:
                raise HoldingsValidationError(f"Row {row_num}: sector name too long.")

        try:
            weight = float(weight_raw)
        except ValueError:
            raise HoldingsValidationError(f"Row {row_num}: weight '{weight_raw}' is not a number.") from None
        if not (weight == weight and weight not in (float("inf"), float("-inf"))):  # NaN/inf check
            raise HoldingsValidationError(f"Row {row_num}: weight must be finite.")
        if weight < 0 or weight > 100:
            raise HoldingsValidationError(f"Row {row_num}: weight {weight} must be between 0 and 100.")

        total_weight += weight
        rows.append(HoldingRow(ticker=ticker_raw, sector=sector_raw or None, weight=weight))

    if not rows:
        raise HoldingsValidationError("CSV has no data rows.")
    if total_weight > 100.5:  # small float-rounding tolerance
        raise HoldingsValidationError(f"Weights sum to {total_weight:.2f}, which exceeds 100%.")

    return rows


def replace_holdings(db, client_id: str, rows: List[HoldingRow]) -> int:
    """Replace-on-upload: deletes every existing Holding row for this
    client and inserts the new set, inside one Prisma batch_() transaction
    so a reader never observes a half-deleted/half-inserted state."""
    with db.batch_() as batcher:
        batcher.holding.delete_many(where={"clientProfileId": client_id})
        for row in rows:
            batcher.holding.create(
                data={
                    "clientProfileId": client_id,
                    "ticker": row.ticker,
                    "sector": row.sector,
                    "weight": row.weight,
                }
            )
    return len(rows)
