from __future__ import annotations

from tradingbot.config import Settings
from tradingbot.strategy.base import Strategy
from tradingbot.strategy.composite import CompositeStrategy
from tradingbot.strategy.consolidation_retest import ConsolidationRetestStrategy
from tradingbot.strategy.ma_crossover import MovingAverageCrossoverStrategy


def _optional(window: int) -> int | None:
    """Config uses 0 to mean 'filter disabled'; strategies use None."""
    return window if window > 0 else None


def build_strategy(settings: Settings) -> Strategy:
    """Build the configured strategy: MA crossover (with trend/volume/RSI/
    VWAP confirmation) as primary, optionally plus the consolidation-retest
    entry pattern as a second entry path."""
    primary = MovingAverageCrossoverStrategy(
        fast=settings.fast_ma,
        slow=settings.slow_ma,
        trend_ma=_optional(settings.trend_ma),
        volume_ma=_optional(settings.volume_ma),
        volume_fast_ma=settings.volume_fast_ma,
        volume_multiplier=settings.volume_multiplier,
        rsi_period=_optional(settings.rsi_period),
        rsi_overbought=settings.rsi_overbought,
        rsi_exit_overbought=settings.rsi_exit_overbought,
        vwap_window=_optional(settings.vwap_window),
        vwap_retest_tolerance=settings.vwap_retest_tolerance,
    )

    if not settings.consolidation_retest_enabled:
        return primary

    return CompositeStrategy(
        primary,
        [
            ConsolidationRetestStrategy(
                consolidation_window=settings.consolidation_window,
                max_range_pct=settings.consolidation_max_range_pct,
                max_bars_since_breakout=settings.consolidation_max_bars_since_breakout,
                retest_tolerance=settings.consolidation_retest_tolerance,
                volume_multiplier=settings.breakout_volume_multiplier,
            )
        ],
    )
