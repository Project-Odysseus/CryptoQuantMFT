"""The order file the desk's system imports, and the fills file it sends back.

No order leaves this code. Each release writes one CSV with the orders to send; the desk's system (SimCorp
Dimension) sends them and returns what was filled. The column layout is in one place here, so matching it to the
importer's format is a change to this file only.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ORDER_COLUMNS = ["order_id", "book", "created_utc", "slot", "market", "contract", "exchange", "side", "quantity", "order_type", "limit_price",
                 "currency", "valid_until_utc", "reason"]
FILL_COLUMNS = ["order_id", "contract", "side", "quantity", "price", "fee", "filled_utc"]


@dataclass(frozen=True, slots=True)
class FileOrder:
    """One row of the order file. `quantity` is whole contracts, always positive; `side` is BUY or SELL."""

    order_id: str
    book: str
    created_utc: str
    slot: str
    market: str
    contract: str
    exchange: str
    side: str
    quantity: int
    order_type: str
    limit_price: str
    currency: str
    valid_until_utc: str
    reason: str


def order_file_name(book: str, released: datetime, slot: str) -> str:
    """"orders_<book>_<YYYYMMDD>_<HHMM>_<slot>.csv", in UTC, so files sort by time and a slot is never overwritten by another."""
    return f"orders_{book}_{released.astimezone(timezone.utc):%Y%m%d_%H%M}_{slot}.csv"


def write_orders(path: Path | str, orders: list[FileOrder]) -> Path:
    """Write the order file in one step (a temporary file, then a rename), so the importer never reads half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ORDER_COLUMNS)
        writer.writeheader()
        for order in orders:
            writer.writerow(asdict(order))
    temporary.replace(path)
    return path


def read_orders(path: Path | str) -> list[dict[str, str]]:
    """The rows of an order file."""
    with Path(path).open(newline="") as handle:
        return list(csv.DictReader(handle))


def read_fills(directory: Path | str) -> list[dict[str, Any]]:
    """Every fill in the fills files of `directory` ("fills*.csv"), oldest file first.

    A fill row needs order_id, contract, side, quantity and price; fee and filled_utc are optional. A row that
    cannot be read is skipped and returned under "error", so one bad line does not hide the rest.
    """
    fills: list[dict[str, Any]] = []
    for path in sorted(Path(directory).glob("fills*.csv")):
        with path.open(newline="") as handle:
            for number, row in enumerate(csv.DictReader(handle), start=2):
                try:
                    fills.append({"order_id": row["order_id"].strip(), "contract": row["contract"].strip(), "side": row["side"].strip().lower(),
                                  "quantity": int(float(row["quantity"])), "price": float(row["price"]), "fee": float(row.get("fee") or 0.0),
                                  "filled_utc": (row.get("filled_utc") or "").strip(), "file": path.name, "line": number})
                except (KeyError, ValueError, AttributeError) as exc:
                    fills.append({"error": f"{path.name} line {number}: {type(exc).__name__}: {exc}", "file": path.name, "line": number})
    return fills
