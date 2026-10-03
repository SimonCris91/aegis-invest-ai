from decimal import Decimal

from app.domain.enums import Currency
from app.orchestration.active_runtime import (
    _capped_demo_pilot_settings,
    effective_aegis_managed_exposure_limit,
)


def test_configured_capital_cannot_raise_project_exposure_limit() -> None:
    assert effective_aegis_managed_exposure_limit(Decimal("98000")) == Decimal("200")
    assert effective_aegis_managed_exposure_limit(
        Decimal("98000"), Currency.USD
    ) == Decimal("98000")
    assert effective_aegis_managed_exposure_limit(Decimal("50")) == Decimal("50")
    assert effective_aegis_managed_exposure_limit(None) is None


def test_demo_pilot_notional_is_capped_and_missing_budget_fails_closed() -> None:
    values = {"AEGIS_ETORO_DEMO_PILOT_NOTIONAL": "98000"}
    settings = _capped_demo_pilot_settings(
        values, authorized_capital_eur=Decimal("98000")
    )
    assert settings.enabled is True
    assert settings.notional_eur == Decimal("200")

    usd_settings = _capped_demo_pilot_settings(
        values,
        authorized_capital_eur=Decimal("98000"),
        authorized_capital_currency=Currency.USD,
    )
    assert usd_settings.notional_eur == Decimal("98000")

    smaller = _capped_demo_pilot_settings(
        values, authorized_capital_eur=Decimal("50")
    )
    assert smaller.notional_eur == Decimal("50")

    missing = _capped_demo_pilot_settings(values, authorized_capital_eur=None)
    assert missing.enabled is False
    assert missing.notional_eur is None
