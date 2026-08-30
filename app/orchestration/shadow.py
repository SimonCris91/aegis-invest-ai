"""Read-only Shadow orchestration with no execution dependency."""

from collections.abc import Callable, Mapping
from datetime import datetime

from app.agent.context import AegisAgentContext
from app.agent.ports import AegisAgent
from app.brokers.models import TrackRecordKind
from app.brokers.ports import BrokerReadPort
from app.config.models import AegisStrategyConfig
from app.domain.risk import RiskContext
from app.news.ports import NewsProvider
from app.risk.manager import RiskManager
from app.storage.sqlite import SqliteRecordStore


class ShadowRecorder:
    def __init__(self, store: SqliteRecordStore) -> None:
        self._store = store

    def record(self, result: Mapping[str, object]) -> int:
        return self._store.append_track_record(TrackRecordKind.SHADOW, result)


class ShadowService:
    """Runs analysis and risk evaluation without accepting an execution provider."""

    def __init__(
        self,
        *,
        broker: BrokerReadPort,
        news: NewsProvider,
        agent: AegisAgent,
        risk_manager: RiskManager,
        strategy: AegisStrategyConfig,
        recorder: ShadowRecorder,
        clock: Callable[[], datetime],
        strategy_version: str,
    ) -> None:
        self._broker = broker
        self._news = news
        self._agent = agent
        self._risk_manager = risk_manager
        self._strategy = strategy
        self._recorder = recorder
        self._clock = clock
        self._strategy_version = strategy_version

    def run(self, symbol: str, instrument_id: int) -> dict[str, object]:
        now = self._clock()
        portfolio = self._broker.portfolio()
        quote = self._broker.quote(instrument_id, symbol)
        instrument = self._broker.instrument(instrument_id, symbol)
        news = self._news.get_news((symbol,), as_of=now)
        result = self._agent.analyze(
            AegisAgentContext(
                portfolio=portfolio,
                quotes=(quote,),
                news=news,
                instruments=(instrument,),
                analysis_timestamp=now,
                strategy=self._strategy,
            )
        )
        decision: object = None
        risk_status = "NONE"
        if result.proposal is not None:
            evaluation = self._risk_manager.evaluate(
                result.proposal,
                RiskContext(
                    evaluated_at=now,
                    portfolio=portfolio,
                    price=quote.to_price_snapshot(),
                    instrument=instrument,
                    market_data_available=True,
                    news_data_available=bool(news),
                    daily_new_trade_count=0,
                    api_state_consistent=True,
                ),
            )
            decision = evaluation.decision.model_dump(mode="json")
            risk_status = evaluation.decision.status.value
        record: dict[str, object] = {
            "mode": "SHADOW",
            "timestamp": now.isoformat(),
            "strategy_version": self._strategy_version,
            "risk_policy_digest": self._risk_manager.policy_digest,
            "portfolio": portfolio.model_dump(mode="json"),
            "market_observations": [quote.model_dump(mode="json")],
            "analysis": result.analysis.model_dump(mode="json"),
            "proposal": (
                result.proposal.model_dump(mode="json") if result.proposal is not None else None
            ),
            "risk_decision": decision,
            "risk_status": risk_status,
            "would_do": result.analysis.recommended_action.value,
            "broker_write_calls": 0,
        }
        self._recorder.record(record)
        return record
