from datetime import datetime, timedelta, timezone

from bot.config import RISK

TRADING_OK = "TRADING_OK"
FROZEN_DAY = "FROZEN_DAY"
FROZEN_WEEK = "FROZEN_WEEK"


def _daily_closes(journal):
    """Last recorded equity per calendar date, from oldest to newest entry."""
    closes = {}
    for e in sorted(
        (e for e in journal if "equity" in e and "timestamp" in e),
        key=lambda e: e["timestamp"],
    ):
        ts = datetime.fromisoformat(e["timestamp"])
        closes[ts.date()] = e["equity"]
    return closes


def check_circuit_breakers(journal, equity):
    """TRADING_OK / FROZEN_DAY / FROZEN_WEEK from journal history + live equity.

    Freezes are triggered by comparing current equity to yesterday's close
    (daily) and to the close from 5 trading days ago (weekly), using the
    distinct calendar dates recorded in the journal as the trading calendar.

    Resets are explicit rather than a side effect of the rolling comparison
    recovering: FROZEN_DAY, once logged today, holds for the rest of today
    even if equity ticks back up, and only lifts once "today" becomes a new
    calendar date (the next trading day's first run). FROZEN_WEEK, once
    logged this week, holds through the week and only lifts on the next
    Monday. This is implemented by re-checking today's/this week's already
    -journaled breaker_status alongside the live comparison, not by
    persisting a separate freeze-state flag.
    """
    now = datetime.now(timezone.utc)
    today = now.date()
    monday = today - timedelta(days=today.weekday())

    closes = _daily_closes(journal)
    dates_before_today = sorted(d for d in closes if d < today)

    yesterday_close = closes[dates_before_today[-1]] if dates_before_today else None
    five_days_ago_close = (
        closes[dates_before_today[-5]] if len(dates_before_today) >= 5 else None
    )

    day_loss_pct = None
    if yesterday_close:
        day_loss_pct = (equity - yesterday_close) / yesterday_close * 100
    week_loss_pct = None
    if five_days_ago_close:
        week_loss_pct = (equity - five_days_ago_close) / five_days_ago_close * 100

    day_breach = day_loss_pct is not None and day_loss_pct <= -RISK["max_daily_loss_pct"]
    week_breach = week_loss_pct is not None and week_loss_pct <= -RISK["max_weekly_loss_pct"]

    already_frozen_today = any(
        e.get("breaker_status") == FROZEN_DAY
        for e in journal
        if e.get("timestamp") and datetime.fromisoformat(e["timestamp"]).date() == today
    )
    already_frozen_this_week = any(
        e.get("breaker_status") == FROZEN_WEEK
        for e in journal
        if e.get("timestamp") and datetime.fromisoformat(e["timestamp"]).date() >= monday
    )

    if week_breach or already_frozen_this_week:
        return FROZEN_WEEK
    if day_breach or already_frozen_today:
        return FROZEN_DAY
    return TRADING_OK
