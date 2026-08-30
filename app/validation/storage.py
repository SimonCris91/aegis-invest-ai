"""Sanitized persistence for validation research runs."""

from pathlib import Path

from app.storage.sqlite import SqliteRecordStore
from app.validation.models import StrategyValidationResult

DEFAULT_STRATEGY_VALIDATION_STORE_PATH = Path("work") / "strategy-validation.sqlite3"


class StrategyValidationStore:
    def __init__(self, store: SqliteRecordStore) -> None:
        self._store = store

    def record_result(self, result: StrategyValidationResult) -> int:
        return self._store.append(
            "strategy-validation",
            {
                "run_id": result.run_id,
                "dataset_id": result.dataset.dataset_id,
                "dataset_digest": result.dataset.data_digest,
                "strategy_version": result.dataset.mapping_version,
                "qualification": result.qualification.status.value,
                "trade_count": result.metrics.trade_count,
                "trade_count_semantics": "completed_realized_trade_records",
                "lifecycle": (
                    result.lifecycle.model_dump(mode="json")
                    if result.lifecycle is not None
                    else None
                ),
                "total_return": str(result.metrics.total_return),
                "total_return_semantics": "total_portfolio_equity_return",
                "maximum_drawdown": str(result.metrics.maximum_drawdown),
                "maximum_drawdown_semantics": "portfolio_equity_drawdown",
                "decision_funnel": (
                    result.decision_funnel.model_dump(mode="json")
                    if result.decision_funnel is not None
                    else None
                ),
                "zero_trade_diagnostics": tuple(
                    item.model_dump(mode="json") for item in result.zero_trade_diagnostics[:10]
                ),
                "research_matrix": tuple(
                    row.model_dump(mode="json") for row in result.research_matrix
                ),
                "evidence_passed": result.evidence_passed,
                "evidence_failures": result.evidence_failures,
                "broker_write": False,
                "broker_write_calls": 0,
                "real_execution_available": False,
            },
        )

    def list_results(self) -> tuple[dict[str, object], ...]:
        return self._store.list("strategy-validation")


def default_strategy_validation_store(path: Path | None = None) -> StrategyValidationStore:
    return StrategyValidationStore(
        SqliteRecordStore(path or DEFAULT_STRATEGY_VALIDATION_STORE_PATH)
    )
