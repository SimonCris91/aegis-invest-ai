"""Historical replay freshness rules separate from live quote freshness."""

from datetime import datetime, timedelta

from app.domain.enums import AssetClass
from app.intelligence.models import TimeFrame

HISTORICAL_REPLAY_FRESHNESS_VERSION = "historical-replay-freshness-v1"


class HistoricalReplayFreshnessPolicy:
    """Maps replay timestamps to a RiskManager reference time for cached bars.

    Live market data continues to use the RiskManager 300-second quote-age rule.
    Historical replay may evaluate an older daily bar at the bar close when the
    gap is valid for that asset class and timeframe.
    """

    policy_version = HISTORICAL_REPLAY_FRESHNESS_VERSION

    def risk_reference_time(
        self,
        *,
        asset_class: AssetClass,
        timeframe: TimeFrame,
        price_timestamp: datetime,
        replay_timestamp: datetime,
    ) -> datetime:
        if price_timestamp > replay_timestamp:
            return replay_timestamp
        if timeframe is TimeFrame.ONE_DAY and self.is_valid_historical_bar(
            asset_class=asset_class,
            timeframe=timeframe,
            price_timestamp=price_timestamp,
            replay_timestamp=replay_timestamp,
        ):
            return price_timestamp
        return replay_timestamp

    def is_valid_historical_bar(
        self,
        *,
        asset_class: AssetClass,
        timeframe: TimeFrame,
        price_timestamp: datetime,
        replay_timestamp: datetime,
    ) -> bool:
        if price_timestamp > replay_timestamp:
            return False
        age = replay_timestamp - price_timestamp
        if timeframe is not TimeFrame.ONE_DAY:
            return False
        if asset_class in {AssetClass.EQUITY, AssetClass.ETF}:
            return age <= timedelta(days=4)
        if asset_class is AssetClass.CRYPTO:
            return age <= timedelta(hours=36)
        return False
