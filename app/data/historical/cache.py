"""Secret-free SQLite cache for normalized historical market bars."""

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.data.models import ProviderInstrumentReference
from app.domain.enums import AssetClass, Currency
from app.intelligence.models import FeatureQuality, MarketBar, TimeFrame
from app.storage.sqlite import assert_secret_free


class HistoricalDataCache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS historical_bars ("
            "provider TEXT NOT NULL, broker TEXT NOT NULL, broker_instrument_id TEXT NOT NULL, "
            "symbol TEXT NOT NULL, timeframe TEXT NOT NULL, timestamp TEXT NOT NULL, "
            "open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL, "
            "volume TEXT, currency TEXT NOT NULL, source TEXT NOT NULL, "
            "data_quality TEXT NOT NULL, "
            "fetched_at TEXT NOT NULL, mapping_payload TEXT NOT NULL, "
            "PRIMARY KEY(provider, broker, broker_instrument_id, timeframe, timestamp))"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS historical_bars_quarantine ("
            "provider TEXT NOT NULL, broker TEXT NOT NULL, broker_instrument_id TEXT NOT NULL, "
            "symbol TEXT NOT NULL, timeframe TEXT NOT NULL, timestamp TEXT NOT NULL, "
            "open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL, "
            "volume TEXT, currency TEXT NOT NULL, source TEXT NOT NULL, "
            "data_quality TEXT NOT NULL, fetched_at TEXT NOT NULL, mapping_payload TEXT NOT NULL, "
            "quarantine_reason TEXT NOT NULL, original_asset_class TEXT, "
            "quarantined_at TEXT NOT NULL, "
            "PRIMARY KEY(provider, broker, broker_instrument_id, timeframe, timestamp, "
            "quarantine_reason))"
        )
        self._connection.commit()

    def upsert_bars(
        self,
        *,
        provider: str,
        bars: tuple[MarketBar, ...],
        fetched_at: datetime,
        mapping: ProviderInstrumentReference,
    ) -> int:
        stats = self.upsert_bars_with_stats(
            provider=provider,
            bars=bars,
            fetched_at=fetched_at,
            mapping=mapping,
        )
        return int(stats["inserted"]) + int(stats["updated"])

    def upsert_bars_with_stats(
        self,
        *,
        provider: str,
        bars: tuple[MarketBar, ...],
        fetched_at: datetime,
        mapping: ProviderInstrumentReference,
    ) -> dict[str, int]:
        payload = mapping.model_dump(mode="json")
        assert_secret_free(payload)
        inserted = 0
        updated = 0
        unchanged = 0
        for bar in bars:
            values = (
                provider,
                bar.instrument.broker,
                bar.instrument.broker_instrument_id,
                bar.instrument.symbol,
                bar.timeframe.value,
                bar.timestamp.isoformat(),
                str(bar.open),
                str(bar.high),
                str(bar.low),
                str(bar.close),
                str(bar.volume) if bar.volume is not None else None,
                bar.currency.value,
                bar.source,
                bar.data_quality.value,
                fetched_at.isoformat(),
                json.dumps(payload, sort_keys=True),
            )
            existing = self._connection.execute(
                "SELECT open, high, low, close, volume, currency, source, data_quality, "
                "mapping_payload FROM historical_bars WHERE provider=? AND broker=? "
                "AND broker_instrument_id=? AND timeframe=? AND timestamp=?",
                (values[0], values[1], values[2], values[4], values[5]),
            ).fetchone()
            comparable = (
                values[6],
                values[7],
                values[8],
                values[9],
                values[10],
                values[11],
                values[12],
                values[13],
                values[15],
            )
            if existing is None:
                self._connection.execute(
                    "INSERT INTO historical_bars("
                    "provider, broker, broker_instrument_id, symbol, timeframe, timestamp, "
                    "open, high, low, close, volume, currency, source, data_quality, "
                    "fetched_at, mapping_payload) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
                inserted += 1
            elif tuple(existing) == comparable:
                unchanged += 1
            else:
                self._connection.execute(
                    "UPDATE historical_bars SET symbol=?, open=?, high=?, low=?, close=?, "
                    "volume=?, currency=?, source=?, data_quality=?, fetched_at=?, "
                    "mapping_payload=? WHERE provider=? AND broker=? AND broker_instrument_id=? "
                    "AND timeframe=? AND timestamp=?",
                    (
                        values[3],
                        values[6],
                        values[7],
                        values[8],
                        values[9],
                        values[10],
                        values[11],
                        values[12],
                        values[13],
                        values[14],
                        values[15],
                        values[0],
                        values[1],
                        values[2],
                        values[4],
                        values[5],
                    ),
                )
                updated += 1
        self._connection.commit()
        return {"inserted": inserted, "updated": updated, "unchanged": unchanged}

    def get_bars(
        self,
        *,
        provider: str,
        instrument_key: tuple[str, str],
        timeframe: TimeFrame,
        as_of: datetime,
        limit: int,
        instrument_factory: dict[str, object],
    ) -> tuple[MarketBar, ...]:
        from app.domain.universe import UniversalInstrument

        rows = self._connection.execute(
            "SELECT timestamp, open, high, low, close, volume, currency, source, data_quality "
            "FROM historical_bars WHERE provider=? AND broker=? AND broker_instrument_id=? "
            "AND timeframe=? AND timestamp <= ? ORDER BY timestamp DESC LIMIT ?",
            (
                provider,
                instrument_key[0],
                instrument_key[1],
                timeframe.value,
                as_of.isoformat(),
                limit,
            ),
        ).fetchall()
        if not rows:
            return ()
        instrument = UniversalInstrument.model_validate(instrument_factory)
        bars = tuple(
            MarketBar(
                instrument=instrument,
                timestamp=datetime.fromisoformat(str(row[0])),
                timeframe=timeframe,
                open=Decimal(str(row[1])),
                high=Decimal(str(row[2])),
                low=Decimal(str(row[3])),
                close=Decimal(str(row[4])),
                volume=Decimal(str(row[5])) if row[5] is not None else None,
                currency=Currency(str(row[6])),
                source=str(row[7]),
                data_quality=FeatureQuality(str(row[8])),
            )
            for row in reversed(rows)
        )
        return bars

    def last_timestamp(
        self, *, provider: str, broker: str, broker_instrument_id: str, timeframe: TimeFrame
    ) -> datetime | None:
        row = self._connection.execute(
            "SELECT MAX(timestamp) FROM historical_bars WHERE provider=? AND broker=? "
            "AND broker_instrument_id=? AND timeframe=?",
            (provider, broker, broker_instrument_id, timeframe.value),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return datetime.fromisoformat(str(row[0]))

    def coverage_summary(
        self,
        *,
        provider: str,
        broker: str,
        broker_instrument_id: str,
        timeframe: TimeFrame,
        start: datetime,
        end: datetime,
    ) -> dict[str, object]:
        row = self._connection.execute(
            "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM historical_bars "
            "WHERE provider=? AND broker=? AND broker_instrument_id=? AND timeframe=? "
            "AND timestamp >= ? AND timestamp <= ?",
            (
                provider,
                broker,
                broker_instrument_id,
                timeframe.value,
                start.isoformat(),
                end.isoformat(),
            ),
        ).fetchone()
        count = int(row[0]) if row is not None else 0
        return {
            "count": count,
            "earliest": datetime.fromisoformat(str(row[1])) if count and row[1] else None,
            "latest": datetime.fromisoformat(str(row[2])) if count and row[2] else None,
        }

    def get_bars_range(
        self,
        *,
        provider: str,
        instrument_key: tuple[str, str],
        timeframe: TimeFrame,
        start: datetime,
        end: datetime,
        instrument_factory: dict[str, object],
    ) -> tuple[MarketBar, ...]:
        from app.domain.universe import UniversalInstrument

        rows = self._connection.execute(
            "SELECT timestamp, open, high, low, close, volume, currency, source, data_quality "
            "FROM historical_bars WHERE provider=? AND broker=? AND broker_instrument_id=? "
            "AND timeframe=? AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp ASC",
            (
                provider,
                instrument_key[0],
                instrument_key[1],
                timeframe.value,
                start.isoformat(),
                end.isoformat(),
            ),
        ).fetchall()
        if not rows:
            return ()
        instrument = UniversalInstrument.model_validate(instrument_factory)
        return tuple(
            MarketBar(
                instrument=instrument,
                timestamp=datetime.fromisoformat(str(row[0])),
                timeframe=timeframe,
                open=Decimal(str(row[1])),
                high=Decimal(str(row[2])),
                low=Decimal(str(row[3])),
                close=Decimal(str(row[4])),
                volume=Decimal(str(row[5])) if row[5] is not None else None,
                currency=Currency(str(row[6])),
                source=str(row[7]),
                data_quality=FeatureQuality(str(row[8])),
            )
            for row in rows
        )

    def quarantine_asset_class_mismatches(
        self,
        *,
        expected_asset_classes: Mapping[str, AssetClass],
        quarantined_at: datetime,
    ) -> dict[str, object]:
        rows = self._connection.execute(
            "SELECT provider, broker, broker_instrument_id, symbol, timeframe, timestamp, "
            "open, high, low, close, volume, currency, source, data_quality, fetched_at, "
            "mapping_payload FROM historical_bars"
        ).fetchall()
        quarantined: list[dict[str, object]] = []
        for row in rows:
            symbol = str(row[3]).upper()
            expected = expected_asset_classes.get(symbol)
            if expected is None:
                continue
            payload = json.loads(str(row[15]))
            observed_raw = payload.get("asset_class")
            observed = str(observed_raw).upper() if observed_raw is not None else "UNKNOWN"
            if observed == expected.value:
                continue
            reason = f"REQUESTED_ASSET_CLASS_MISMATCH:{expected.value}!={observed}"
            self._connection.execute(
                "INSERT OR REPLACE INTO historical_bars_quarantine("
                "provider, broker, broker_instrument_id, symbol, timeframe, timestamp, "
                "open, high, low, close, volume, currency, source, data_quality, fetched_at, "
                "mapping_payload, quarantine_reason, original_asset_class, quarantined_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*tuple(row), reason, observed, quarantined_at.isoformat()),
            )
            self._connection.execute(
                "DELETE FROM historical_bars WHERE provider=? AND broker=? "
                "AND broker_instrument_id=? AND timeframe=? AND timestamp=?",
                (row[0], row[1], row[2], row[4], row[5]),
            )
            quarantined.append(
                {
                    "symbol": symbol,
                    "provider": row[0],
                    "broker_instrument_id": row[2],
                    "timeframe": row[4],
                    "timestamp": row[5],
                    "reason": reason,
                }
            )
        self._connection.commit()
        return {
            "quarantined_rows": len(quarantined),
            "quarantined_groups": sorted(
                {
                    (
                        str(item["symbol"]),
                        str(item["broker_instrument_id"]),
                        str(item["timeframe"]),
                        str(item["reason"]),
                    )
                    for item in quarantined
                }
            ),
        }

    def quarantine_provider_broker_reference_mismatches(
        self,
        *,
        provider: str,
        expected_broker_ids: Mapping[str, str],
        quarantined_at: datetime,
    ) -> dict[str, object]:
        rows = self._connection.execute(
            "SELECT provider, broker, broker_instrument_id, symbol, timeframe, timestamp, "
            "open, high, low, close, volume, currency, source, data_quality, fetched_at, "
            "mapping_payload FROM historical_bars WHERE provider=?",
            (provider,),
        ).fetchall()
        quarantined: list[dict[str, object]] = []
        for row in rows:
            symbol = str(row[3]).upper()
            expected = expected_broker_ids.get(symbol)
            observed = str(row[2])
            if expected is None or observed == expected:
                continue
            reason = f"MAPPING_CONFLICT_QUARANTINED:{symbol}:{observed}!={expected}"
            self._connection.execute(
                "INSERT OR REPLACE INTO historical_bars_quarantine("
                "provider, broker, broker_instrument_id, symbol, timeframe, timestamp, "
                "open, high, low, close, volume, currency, source, data_quality, fetched_at, "
                "mapping_payload, quarantine_reason, original_asset_class, quarantined_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*tuple(row), reason, None, quarantined_at.isoformat()),
            )
            self._connection.execute(
                "DELETE FROM historical_bars WHERE provider=? AND broker=? "
                "AND broker_instrument_id=? AND timeframe=? AND timestamp=?",
                (row[0], row[1], row[2], row[4], row[5]),
            )
            quarantined.append(
                {
                    "symbol": symbol,
                    "provider": row[0],
                    "observed_broker_instrument_id": observed,
                    "expected_broker_instrument_id": expected,
                    "timeframe": row[4],
                    "timestamp": row[5],
                    "reason": reason,
                }
            )
        self._connection.commit()
        return {
            "quarantined_rows": len(quarantined),
            "quarantined_groups": sorted(
                {
                    (
                        str(item["symbol"]),
                        str(item["observed_broker_instrument_id"]),
                        str(item["expected_broker_instrument_id"]),
                        str(item["timeframe"]),
                    )
                    for item in quarantined
                }
            ),
        }

    def duplicate_timestamp_report(self) -> tuple[dict[str, object], ...]:
        rows = self._connection.execute(
            "SELECT symbol, timeframe, provider, broker_instrument_id, COUNT(*) AS total, "
            "COUNT(DISTINCT timestamp) AS unique_count "
            "FROM historical_bars GROUP BY symbol, timeframe, provider, broker_instrument_id "
            "HAVING total > unique_count ORDER BY symbol, timeframe"
        ).fetchall()
        return tuple(
            {
                "symbol": row[0],
                "timeframe": row[1],
                "provider": row[2],
                "broker_instrument_id": row[3],
                "total_rows": row[4],
                "unique_timestamps": row[5],
                "duplicate_timestamps": row[4] - row[5],
            }
            for row in rows
        )

    def inventory_summary(self) -> tuple[dict[str, object], ...]:
        rows = self._connection.execute(
            "SELECT symbol, timeframe, provider, broker_instrument_id, COUNT(*) AS total, "
            "MIN(timestamp) AS earliest, MAX(timestamp) AS latest "
            "FROM historical_bars GROUP BY symbol, timeframe, provider, broker_instrument_id "
            "ORDER BY symbol, timeframe, provider, broker_instrument_id"
        ).fetchall()
        return tuple(
            {
                "symbol": row[0],
                "timeframe": row[1],
                "provider": row[2],
                "broker_instrument_id": row[3],
                "bar_count": row[4],
                "earliest_timestamp": row[5],
                "latest_timestamp": row[6],
            }
            for row in rows
        )

    def mapping_references(
        self, *, provider: str | None = None
    ) -> tuple[ProviderInstrumentReference, ...]:
        if provider is None:
            rows = self._connection.execute(
                "SELECT mapping_payload FROM historical_bars GROUP BY mapping_payload"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT mapping_payload FROM historical_bars WHERE provider=? "
                "GROUP BY mapping_payload",
                (provider,),
            ).fetchall()
        return tuple(
            ProviderInstrumentReference.model_validate(json.loads(str(row[0]))) for row in rows
        )
