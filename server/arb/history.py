"""Filtered, cursor-paginated reads of canonical opportunity episodes.

History is read newest first in the fixed order ``start_ns DESC, id DESC``,
which ``idx_episodes_start`` (``idx_episodes_pair_start`` for a pair filter,
``idx_episodes_close_start`` for open episodes or a close reason) supplies
directly because SQLite index entries end in the rowid. A filter that the
chosen index cannot narrow is bounded by the store's per-page query budget.

Pagination is keyset, not offset: the cursor carries the last returned
``(start_ns, id)`` so later pages cost the same as the first. The first page
also fixes a snapshot bound, ``id <= MAX(id)`` at that moment, so rows the
writer inserts while a client is paging never appear in that traversal. A
traversal therefore never repeats a row and never skips a row that existed
when it began. The snapshot bounds membership only: an open episode that
closes between pages is read with its current fields, so a ``state=open``
traversal can lose it. Explicit pruning can delete rows mid-traversal.

Each cursor is bound to the filters it was issued for; reusing it with
different filters is rejected rather than silently resuming a different query.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal, TypeGuard

from arb.types import EpisodeCloseReason

MAX_SQLITE_INTEGER = 2**63 - 1
HISTORY_ORDER = "start_ns_desc_id_desc"
CURSOR_VERSION = 1

HistoryState = Literal["open", "closed"]

EPISODE_COLUMNS = (
    "start_ns, end_ns, pair, quote_asset, buy_exchange, sell_exchange, buy_price, "
    "sell_price, spread_pct, max_size, theoretical_profit, peak_spread_pct, peak_size, "
    "peak_profit, pricing_ledgers, close_spread_pct, close_reason"
)


class CursorError(ValueError):
    """A cursor that is malformed, from another version, or issued for other filters."""


@dataclass(frozen=True)
class HistoryFilters:
    """Conjunctive episode filters. ``from_ns`` is inclusive, ``to_ns`` exclusive.

    ``state="open"`` matches episodes with neither an end nor a close reason,
    the same set ``OpportunityStore.open_count`` counts. ``state="closed"``
    matches every episode with a close reason, including ``orphaned`` rows,
    which have no ``end_ns`` because their lifetime is unknown.
    """

    from_ns: int | None = None
    to_ns: int | None = None
    pair: str | None = None
    buy_exchange: str | None = None
    sell_exchange: str | None = None
    close_reason: EpisodeCloseReason | None = None
    state: HistoryState | None = None

    def __post_init__(self) -> None:
        for name in ("from_ns", "to_ns"):
            value = getattr(self, name)
            if value is not None and not 0 <= value <= MAX_SQLITE_INTEGER:
                raise ValueError(f"{name} must be between 0 and {MAX_SQLITE_INTEGER}")
        if self.from_ns is not None and self.to_ns is not None and self.from_ns >= self.to_ns:
            raise ValueError("from_ns must be less than to_ns")

    def fingerprint(self) -> str:
        canonical = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class HistoryCursor:
    """Position after the last returned row, within a fixed insert snapshot."""

    start_ns: int
    row_id: int
    snapshot_max_id: int
    fingerprint: str

    def encode(self) -> str:
        body = json.dumps(
            {
                "v": CURSOR_VERSION,
                "s": self.start_ns,
                "i": self.row_id,
                "m": self.snapshot_max_id,
                "f": self.fingerprint,
            },
            separators=(",", ":"),
        )
        return base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str, filters: HistoryFilters) -> HistoryCursor:
        if not token or len(token) > 512:
            raise CursorError("cursor is empty or too long")
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            body = json.loads(raw)
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise CursorError("cursor is not valid") from exc
        if not isinstance(body, dict) or body.get("v") != CURSOR_VERSION:
            raise CursorError("cursor version is not supported")
        start_ns, row_id, snapshot_max_id = body.get("s"), body.get("i"), body.get("m")
        fingerprint = body.get("f")
        if not (
            _sqlite_integer(start_ns)
            and _sqlite_integer(row_id)
            and _sqlite_integer(snapshot_max_id)
            and isinstance(fingerprint, str)
        ):
            raise CursorError("cursor is not valid")
        if fingerprint != filters.fingerprint():
            raise CursorError("cursor was issued for different filters")
        return cls(start_ns, row_id, snapshot_max_id, fingerprint)


def _sqlite_integer(value: object) -> TypeGuard[int]:
    # `type(...) is int` rejects JSON booleans, which are ints in Python.
    return type(value) is int and 0 <= value <= MAX_SQLITE_INTEGER


def build_page_query(
    filters: HistoryFilters,
    after: tuple[int, int] | None,
    snapshot_max_id: int,
    limit: int,
) -> tuple[str, list[object]]:
    """Return SQL and bound parameters for one page, selecting ``id`` first.

    Every filter value is a bound parameter; only fixed clause text is joined.
    """
    clauses = ["id <= ?"]
    params: list[object] = [snapshot_max_id]
    if after is not None:
        clauses.append("(start_ns, id) < (?, ?)")
        params.extend(after)
    if filters.from_ns is not None:
        clauses.append("start_ns >= ?")
        params.append(filters.from_ns)
    if filters.to_ns is not None:
        clauses.append("start_ns < ?")
        params.append(filters.to_ns)
    for column in ("pair", "buy_exchange", "sell_exchange", "close_reason"):
        value = getattr(filters, column)
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    if filters.state == "open":
        clauses.append("end_ns IS NULL AND close_reason IS NULL")
    elif filters.state == "closed":
        # Unary `+` keeps this almost-always-true term off `idx_episodes_close_start`,
        # whose range scan would need a sort; the start index already yields order.
        clauses.append("+close_reason IS NOT NULL")
    params.append(limit)
    sql = (
        f"SELECT id, {EPISODE_COLUMNS} FROM opportunity_episodes "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY start_ns DESC, id DESC LIMIT ?"
    )
    return sql, params
