import pytest

from app.brokers.etoro.demo_preflight import (
    _allows_amount_order,
    _eligibility_blockers,
    build_first_demo_preflight_report,
)
from app.brokers.etoro.http import DisciplinedHttpClient
from tests.test_step75 import FakeReadClient, _config, _eligibility, _now, _values


@pytest.mark.parametrize("value", ["all", " ALL ", "amount", "cash", "byamount", "amountorder"])
def test_supported_amount_values(value):
    assert _allows_amount_order((value,))


@pytest.mark.parametrize("values", [("units",), ("fractional",), ("unknown",), ("",), ()])
def test_unsupported_amount_values(values):
    assert not _allows_amount_order(values)


def test_all_does_not_override_other_eligibility_rules():
    assert _eligibility_blockers(_eligibility(quantity_types=("all",))) == ()
    assert _eligibility_blockers(_eligibility(quantity_types=("all",), allow_open=False)) == (
        "allowOpenPosition is false",
    )
    assert "unleveraged Demo trading is unavailable" in _eligibility_blockers(
        _eligibility(quantity_types=("all",), leverage=2)
    )


def test_read_only_preflight_accepts_all_without_broker_write(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No broker HTTP requests are permitted in this test")

    for method in ("get", "get_once", "post_once", "post_read"):
        monkeypatch.setattr(DisciplinedHttpClient, method, forbidden)

    class Client(FakeReadClient):
        last_server_now = _now()

    report = build_first_demo_preflight_report(
        _config(kill_switch=True),
        values=_values(),
        client=Client(eligibility=_eligibility(quantity_types=("all",))),
        clock=_now,
    )
    check = next(c for c in report.checks if c.name == "demo_eligibility")
    assert check.status == "PASS"
    assert report.eligibility_result == "APPROVED"
