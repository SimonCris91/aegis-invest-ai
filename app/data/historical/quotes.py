"""Quote observations separate from candles, with bitemporal causal selection."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.domain.base import require_aware
from app.domain.enums import Currency
from app.domain.market import MarketQuote


def _stamp(value: datetime) -> str:
    return require_aware(value, "quote timestamp").astimezone(UTC).isoformat(timespec="microseconds")


class QuoteObservationStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute('CREATE TABLE IF NOT EXISTS quote_observations ('
            'provider TEXT NOT NULL, instrument_id INTEGER NOT NULL, symbol TEXT NOT NULL, '
            'currency TEXT NOT NULL, provider_at TEXT NOT NULL, received_at TEXT NOT NULL, '
            'payload TEXT NOT NULL, PRIMARY KEY(provider,instrument_id,symbol,currency,provider_at,received_at))')
        self.connection.commit()
        self.connection.execute('CREATE TABLE IF NOT EXISTS quote_collection_control (id INTEGER PRIMARY KEY, until_at TEXT NOT NULL)')
        self.connection.commit()

    def last_received(self, *, provider: str, instrument_id: int, symbol: str) -> str:
        row = self.connection.execute('SELECT MAX(received_at) FROM quote_observations WHERE provider=? AND instrument_id=? AND symbol=?',
            (provider, instrument_id, symbol)).fetchone()
        return row[0] or ""

    def cooling_down(self, *, now: datetime) -> bool:
        row = self.connection.execute('SELECT until_at FROM quote_collection_control WHERE id=1').fetchone()
        return bool(row and row[0] > _stamp(now))

    def set_cooldown(self, *, until: datetime) -> None:
        with self.connection:
            self.connection.execute('INSERT OR REPLACE INTO quote_collection_control VALUES (1,?)', (_stamp(until),))

    def close(self):
        self.connection.close()

    def record(self, *, provider: str, quote: MarketQuote, received_at: datetime) -> None:
        if not provider.strip():
            raise ValueError("provider required")
        if quote.bid is None or quote.ask is None:
            raise ValueError("complete bid/ask required")
        provider_at, received = _stamp(quote.as_of), _stamp(received_at)
        # Retain future-dated observations for diagnosis, but never admit them
        # to the causal selector, even once wall-clock time catches up.
        with self.connection:
            self.connection.execute('INSERT OR IGNORE INTO quote_observations VALUES (?,?,?,?,?,?,?)',
                (provider,quote.instrument_id,quote.symbol,quote.currency.value,
                 provider_at,received,quote.model_dump_json()))

    def at_cutoff(self, *, provider: str, instrument_id: int, symbol: str,
                  currency: Currency, cutoff: datetime, max_age: timedelta) -> MarketQuote | None:
        if max_age <= timedelta(0):
            raise ValueError("max_age must be positive")
        end, start = _stamp(cutoff), _stamp(cutoff - max_age)
        row = self.connection.execute('SELECT payload FROM quote_observations '
            'WHERE provider=? AND instrument_id=? AND symbol=? AND currency=? '
            'AND provider_at>=? AND provider_at<=? AND received_at<=? '
            'AND provider_at<=received_at ORDER BY provider_at DESC, received_at DESC LIMIT 1',
            (provider,instrument_id,symbol,currency.value,start,end,end)).fetchone()
        return MarketQuote.model_validate_json(row[0]) if row else None
