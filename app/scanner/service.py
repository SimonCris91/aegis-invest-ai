"""Broker-neutral multi-asset scanner."""

import hashlib
from datetime import datetime
from decimal import Decimal

from app.domain.enums import MarketStatus
from app.domain.market import MarketQuote
from app.domain.portfolio import PortfolioSnapshot
from app.domain.universe import (
    BrokerEligibilitySnapshot,
    CandidateState,
    MarketScanResult,
    OpportunityCandidate,
    OpportunityFeatures,
    UniversalInstrument,
)
from app.domain.versions import SCANNER_VERSION
from app.policies.engine import AssetPolicyEngine
from app.scanner.catalog import InstrumentCatalog
from app.scanner.models import ScannerLimits
from app.scanner.ports import MarketScannerAdapter
from app.scanner.ranking import OpportunityRankingEngine
from app.storage.sqlite import SqliteRecordStore


class MarketScanError(RuntimeError):
    pass


class OpenMarketCandidateScanner:
    def __init__(
        self,
        *,
        adapter: MarketScannerAdapter,
        policy_engine: AssetPolicyEngine,
        ranking_engine: OpportunityRankingEngine,
        limits: ScannerLimits | None = None,
        catalog: InstrumentCatalog | None = None,
        store: SqliteRecordStore | None = None,
        scanner_version: str = SCANNER_VERSION,
    ) -> None:
        self._adapter = adapter
        self._policy_engine = policy_engine
        self._ranking_engine = ranking_engine
        self._limits = limits or ScannerLimits()
        self._catalog = catalog or InstrumentCatalog()
        self._store = store
        self._scanner_version = scanner_version

    def scan(self, *, portfolio: PortfolioSnapshot, as_of: datetime) -> MarketScanResult:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("scan time must include a timezone")
        instruments = self._adapter.discover_instruments(
            as_of=as_of, limit=self._limits.discovery_limit
        )
        self._catalog.upsert_many(instruments)
        candidates = tuple(
            self._candidate_for(instrument, portfolio=portfolio, as_of=as_of)
            for instrument in instruments
        )
        ranked = self._ranking_engine.rank(
            candidates,
            top_n=self._limits.ranked_shortlist_limit,
        )
        result = MarketScanResult(
            broker=self._adapter.capabilities.provider,
            as_of=as_of,
            scanner_version=self._scanner_version,
            ranking_version=self._ranking_engine.ranking_version,
            policy_version=self._policy_engine.policy_version,
            total_discovered=len(instruments),
            candidates=candidates,
            ranked_candidates=ranked,
            broker_write_calls=0,
            real_execution_available=False,
            demo_execution_enabled=False,
        )
        if self._store is not None:
            record_id = self._store.append("market-scan-summary", self._record(result))
            result = result.model_copy(update={"persisted_record_id": record_id})
        return result

    def _candidate_for(
        self, instrument: UniversalInstrument, *, portfolio: PortfolioSnapshot, as_of: datetime
    ) -> OpportunityCandidate:
        quote = self._safe_quote(instrument, as_of=as_of)
        eligibility = self._safe_eligibility(instrument, as_of=as_of)
        enriched = self._enrich(instrument, quote=quote, eligibility=eligibility)
        features = self._features(enriched, quote=quote, portfolio=portfolio)
        decision = self._policy_engine.evaluate(
            enriched,
            portfolio=portfolio,
            as_of=as_of,
            features=features,
        )
        return OpportunityCandidate(
            candidate_id=self._candidate_id(enriched, as_of),
            broker=enriched.broker,
            instrument=enriched,
            asset_class=enriched.asset_class,
            market_status=enriched.market_status,
            quote=quote,
            broker_eligibility=eligibility,
            policy_allowed=decision.allowed,
            policy_version=decision.policy_version,
            candidate_state=decision.candidate_state,
            data_quality=decision.data_quality,
            candidate_score=Decimal("0"),
            opportunity_factors=(),
            risk_factors=(),
            rejection_reasons=decision.reasons,
            features=features,
            confidence=Decimal("0"),
            scanner_version=self._scanner_version,
            ranking_version=self._ranking_engine.ranking_version,
            timestamp=as_of,
        )

    def _safe_quote(
        self, instrument: UniversalInstrument, *, as_of: datetime
    ) -> MarketQuote | None:
        try:
            return self._adapter.quote(instrument, as_of=as_of)
        except (RuntimeError, ValueError):
            return None

    def _safe_eligibility(
        self, instrument: UniversalInstrument, *, as_of: datetime
    ) -> BrokerEligibilitySnapshot:
        try:
            return self._adapter.eligibility(instrument, as_of=as_of)
        except (RuntimeError, ValueError) as exc:
            return BrokerEligibilitySnapshot(
                broker=instrument.broker,
                broker_instrument_id=instrument.broker_instrument_id,
                symbol=instrument.symbol,
                checked_at=as_of,
                verified=False,
                allow_open=False,
                reason=type(exc).__name__,
            )

    @staticmethod
    def _enrich(
        instrument: UniversalInstrument,
        *,
        quote: MarketQuote | None,
        eligibility: BrokerEligibilitySnapshot,
    ) -> UniversalInstrument:
        updates: dict[str, object] = {"broker_eligibility": eligibility}
        if eligibility.minimum_order_value is not None:
            updates["minimum_order_value"] = eligibility.minimum_order_value
        if eligibility.currency is not None:
            updates["currency"] = eligibility.currency
        if eligibility.settlement_type is not None:
            updates["settlement_type"] = eligibility.settlement_type
        if eligibility.leverage_configs:
            updates["max_leverage"] = Decimal(max(eligibility.leverage_configs))
            updates["leverage_available"] = any(value > 1 for value in eligibility.leverage_configs)
        if quote is not None:
            updates.update(
                {
                    "bid": quote.bid,
                    "ask": quote.ask,
                    "last_price": quote.price,
                    "price_timestamp": quote.as_of,
                }
            )
            if quote.market_status is not MarketStatus.UNKNOWN:
                updates["market_status"] = quote.market_status
        return instrument.model_copy(update=updates)

    @staticmethod
    def _features(
        instrument: UniversalInstrument,
        *,
        quote: MarketQuote | None,
        portfolio: PortfolioSnapshot,
    ) -> OpportunityFeatures:
        bid = quote.bid if quote is not None else instrument.bid
        ask = quote.ask if quote is not None else instrument.ask
        price = quote.price if quote is not None else instrument.last_price
        mid_price: Decimal | None = None
        spread: Decimal | None = None
        spread_percentage: Decimal | None = None
        if bid is not None and ask is not None:
            spread = ask - bid
            mid_price = (ask + bid) / Decimal("2")
            if mid_price > 0:
                spread_percentage = spread / mid_price
        elif price is not None:
            mid_price = price
        momentum: Decimal | None = None
        if quote is not None and quote.previous_close is not None:
            momentum = (quote.price - quote.previous_close) / quote.previous_close
        instrument_id = instrument.numeric_instrument_id
        current_weight = (
            portfolio.weight_for(instrument_id) if instrument_id is not None else Decimal("0")
        )
        return OpportunityFeatures(
            mid_price=mid_price,
            spread=spread,
            spread_percentage=spread_percentage,
            short_term_momentum=momentum,
            current_portfolio_weight=current_weight,
            drawdown=portfolio.drawdown,
        )

    @staticmethod
    def _candidate_id(instrument: UniversalInstrument, as_of: datetime) -> str:
        payload = "|".join((instrument.broker, instrument.broker_instrument_id, as_of.isoformat()))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _record(result: MarketScanResult) -> dict[str, object]:
        return {
            "timestamp": result.as_of.isoformat(),
            "broker": result.broker,
            "scanner_version": result.scanner_version,
            "ranking_version": result.ranking_version,
            "asset_policy_version": result.policy_version,
            "markets_scanned": result.total_discovered,
            "asset_classes_scanned": tuple(item.value for item in result.asset_classes_found),
            "candidate_count": len(result.candidates),
            "blocked_count": len(
                [
                    item
                    for item in result.candidates
                    if item.candidate_state is not CandidateState.OPEN_AND_ALLOWED
                ]
            ),
            "top_candidates": tuple(
                {
                    "rank": item.rank,
                    "broker": item.broker,
                    "symbol": item.instrument.symbol,
                    "asset_class": item.asset_class.value,
                    "state": item.candidate_state.value,
                    "score": str(item.candidate_score),
                    "data_quality": item.data_quality.value,
                }
                for item in result.ranked_candidates
            ),
            "policy_decisions": tuple(
                {
                    "symbol": item.instrument.symbol,
                    "asset_class": item.asset_class.value,
                    "state": item.candidate_state.value,
                    "allowed": item.policy_allowed,
                    "reasons": item.rejection_reasons,
                }
                for item in result.candidates
            ),
            "data_quality_status": tuple(
                {
                    "symbol": item.instrument.symbol,
                    "status": item.data_quality.value,
                }
                for item in result.candidates
            ),
            "broker_write_calls": 0,
            "real_execution_available": False,
            "demo_execution_enabled": False,
        }
