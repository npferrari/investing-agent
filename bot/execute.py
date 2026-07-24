import logging
import math
import subprocess
import time
from datetime import datetime, timezone

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from bot import positions_state
from bot.config import (
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    RISK,
    STOP_ANOMALY_SINGLE_DAY_PCT,
    STOP_CONFIRM_DELAY_SECONDS,
    UNIVERSE,
)
from bot.data import get_daily_bars, get_latest_quote
from bot.journal import get_ramp_cap_pct, read_entries

logger = logging.getLogger("bot.execute")

_trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)

_ALL_SYMBOLS = set(UNIVERSE["STOCKS"]) | set(UNIVERSE["ETFS"]) | set(UNIVERSE["HEDGE"])

STOP_LOSS = "STOP_LOSS"
STOP_ANOMALY = "STOP_ANOMALY"


def get_positions():
    return _trading_client.get_all_positions()


def get_equity():
    return float(_trading_client.get_account().equity)


def _reject(symbol, action, usd_amount, sleeve, reason, quote=None):
    logger.warning("REJECTED %s %s $%.2f (%s): %s", action, symbol, usd_amount, sleeve, reason)
    return {
        "status": "REJECTED",
        "symbol": symbol,
        "action": action,
        "usd_amount": usd_amount,
        "sleeve": sleeve,
        "reason": reason,
        "quote": quote,
    }


def execute_order(symbol, action, usd_amount, sleeve, time_in_force=TimeInForce.DAY):
    """Mechanically place an already-approved order.

    This function no longer owns policy (universe allowlist, ticker/sector/
    position caps) — bot/risk.py is the single gate for that now that the
    brain/risk/execute pipeline is the only path that trades for real (v0's
    signal computation continues, unexecuted, as a benchmark — see
    research/v1-architecture.md §4.1). What stays here is genuinely
    mechanical: is this even a real symbol, is the market in a state where
    this order type can be submitted, and the notional-vs-whole-share and
    deploy-ramp details of placing it.
    """
    action = action.upper()

    if symbol not in _ALL_SYMBOLS:
        return _reject(symbol, action, usd_amount, sleeve, f"{symbol} is outside UNIVERSE")

    clock = _trading_client.get_clock()
    # DAY orders need the market open right now. Market-on-open (OPG) orders
    # are, by design, submitted while the market is closed (queued for the
    # next session) — gating them on is_open would reject the exact use case
    # they exist for. Alpaca enforces its own OPG submission window; let the
    # API reject an out-of-window OPG order rather than second-guessing that
    # window here.
    if time_in_force == TimeInForce.DAY and not clock.is_open:
        return _reject(symbol, action, usd_amount, sleeve, "market is closed")

    # Quote is fetched and logged for every order attempt that reaches here,
    # independent of accept/reject, so spread + slippage vs. this reference
    # price can be reconstructed later from the fill.
    quote = get_latest_quote(symbol)[symbol]
    quote_log = {"bid": quote.bid_price, "ask": quote.ask_price, "timestamp": quote.timestamp}

    price = quote.ask_price if action == "BUY" else quote.bid_price
    if not price or price <= 0:
        return _reject(symbol, action, usd_amount, sleeve, "no valid quote available", quote_log)

    # Prefer a notional (dollar-denominated) market order so position size
    # tracks usd_amount exactly. Alpaca rejects notional orders for
    # non-fractionable instruments, so fall back to a whole-share qty order
    # for those.
    asset = _trading_client.get_asset(symbol)
    notional = None
    qty = None
    if asset.fractionable:
        notional = round(usd_amount, 2)
        order_value = notional
    else:
        qty = math.floor(usd_amount / price)
        if qty < 1:
            return _reject(
                symbol,
                action,
                usd_amount,
                sleeve,
                f"{symbol} is not fractionable and ${usd_amount:.2f} buys less than "
                f"one whole share at ${price:.2f}",
                quote_log,
            )
        order_value = qty * price

    if action == "BUY":
        # deploy_ramp caps total invested % of equity by account-age week —
        # not one of risk.py's G-checks (it's a v0-era pacing mechanism, not
        # a PRD-numbered guardrail), so it stays here rather than moving.
        equity = get_equity()
        positions = _trading_client.get_all_positions()
        ramp_cap_pct = get_ramp_cap_pct(read_entries())
        invested_value = sum(float(p.market_value) for p in positions)
        invested_after = invested_value + order_value
        if invested_after > (ramp_cap_pct / 100) * equity:
            return _reject(
                symbol,
                action,
                usd_amount,
                sleeve,
                f"RAMP_LIMIT: would push total invested to ${invested_after:.2f} "
                f"(cap is {ramp_cap_pct}% of ${equity:.2f} equity)",
                quote_log,
            )

    # notional and qty are mutually exclusive on MarketOrderRequest; exactly
    # one is set above depending on whether the asset is fractionable.
    order_request = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        notional=notional,
        side=OrderSide.BUY if action == "BUY" else OrderSide.SELL,
        time_in_force=time_in_force,
    )
    # A rejection here is a legitimate outcome (e.g. an OPG order submitted
    # outside Alpaca's 7pm-9:28am ET window — verified live 2026-07-29) and
    # must produce a graceful REJECTED result, not an uncaught exception that
    # would crash the whole run and abort every other proposal in the batch.
    try:
        order = _trading_client.submit_order(order_request)
    except APIError as exc:
        return _reject(symbol, action, usd_amount, sleeve, f"Alpaca rejected the order: {exc}", quote_log)

    logger.info(
        "SUBMITTED %s %s %s (~$%.2f, sleeve=%s, tif=%s) bid=%.2f ask=%.2f order_id=%s",
        action,
        symbol,
        f"notional=${notional:.2f}" if notional is not None else f"qty={qty}",
        order_value,
        sleeve,
        time_in_force,
        quote.bid_price,
        quote.ask_price,
        order.id,
    )

    if action == "BUY":
        positions_state.record_open(symbol, sleeve, datetime.now(timezone.utc).isoformat())
    elif action == "SELL":
        positions_state.record_close(symbol)

    return {
        "status": "SUBMITTED",
        "symbol": symbol,
        "action": action,
        "qty": qty,
        "notional": notional,
        "order_value": order_value,
        "usd_amount": usd_amount,
        "sleeve": sleeve,
        "time_in_force": str(time_in_force),
        "quote": quote_log,
        "order_id": str(order.id),
    }


def _open_github_issue(title, body):
    """Best-effort — never raises. Requires the `gh` CLI authenticated (a
    GITHUB_TOKEN env var in Actions; local `gh auth login` for manual runs).
    A failure here must never abort the run over an already-handled event."""
    try:
        subprocess.run(
            ["gh", "issue", "create", "--title", title, "--body", body],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        logger.info("Opened GitHub issue: %s", title)
    except Exception as exc:
        logger.error("Could not auto-open GitHub issue (%s): %s", title, exc)


def _single_day_move_pct(symbol, current_price):
    """(current_price / previous session's close - 1) * 100, for the
    STOP_ANOMALY single-day-move check. Not the same as unrealized P/L,
    which is measured from entry price over the whole holding period."""
    bars = get_daily_bars([symbol], days=5)
    closes = bars.loc[symbol]["close"]
    previous_close = closes.iloc[-1]
    return (current_price / previous_close - 1) * 100


def stop_loss_sweep():
    """SELL any open position breaching its sleeve's stop-loss threshold.

    Runs every cycle unconditionally, including when circuit breakers have
    the account in DEFENSIVE MODE — a stop-loss is risk-reducing, so it must
    never be blocked by a freeze that only gates new BUYs.

    A breach on the position's current mark is not sold immediately: per
    the 2026-07-23 single-vendor-data decision (research/v1-architecture.md
    §5), a fresh quote is fetched STOP_CONFIRM_DELAY_SECONDS later and the
    breach must still hold — filtering a single bad print rather than
    trusting one reading. If the confirmed price implies a single-day move
    past STOP_ANOMALY_SINGLE_DAY_PCT, the sell still proceeds (a stop-loss
    never waits on a human) but is journaled STOP_ANOMALY and pages one via
    an auto-opened GitHub issue — its forecast resolution is excluded from
    scorecard.py's hit-rate stats once that exists (accepted, bounded risk,
    not silently absorbed into the track record).
    """
    state = positions_state.load()
    results = []

    for position in _trading_client.get_all_positions():
        symbol = position.symbol
        sleeve = positions_state.get_sleeve(symbol, state)
        stop_pct = RISK[f"stop_{sleeve.lower()}_pct"]
        avg_entry = float(position.avg_entry_price)

        unrealized_pct = float(position.unrealized_plpc) * 100
        if unrealized_pct > stop_pct:
            continue

        logger.warning(
            "STOP_LOSS candidate: %s unrealized=%.2f%% <= stop=%.2f%% (sleeve=%s) — "
            "confirming with a second quote in %ds",
            symbol,
            unrealized_pct,
            stop_pct,
            sleeve,
            STOP_CONFIRM_DELAY_SECONDS,
        )
        time.sleep(STOP_CONFIRM_DELAY_SECONDS)

        confirm_quote = get_latest_quote(symbol)[symbol]
        confirm_price = confirm_quote.bid_price
        if not confirm_price or confirm_price <= 0:
            logger.warning("STOP_LOSS %s: no valid confirming quote — skipping this cycle", symbol)
            continue

        confirmed_pct = (confirm_price - avg_entry) / avg_entry * 100
        if confirmed_pct > stop_pct:
            logger.info(
                "STOP_LOSS %s: breach did not hold on confirmation (%.2f%% > %.2f%%) — "
                "one-print glitch filtered, not selling",
                symbol,
                confirmed_pct,
                stop_pct,
            )
            continue

        anomaly = False
        try:
            single_day_pct = _single_day_move_pct(symbol, confirm_price)
            anomaly = abs(single_day_pct) > STOP_ANOMALY_SINGLE_DAY_PCT
        except Exception as exc:
            logger.error("STOP_LOSS %s: could not compute single-day move for anomaly check: %s", symbol, exc)
            single_day_pct = None

        status = STOP_ANOMALY if anomaly else STOP_LOSS
        logger.warning(
            "%s %s confirmed=%.2f%% <= stop=%.2f%% (sleeve=%s, single_day=%s) — selling",
            status,
            symbol,
            confirmed_pct,
            stop_pct,
            sleeve,
            f"{single_day_pct:.1f}%" if single_day_pct is not None else "unknown",
        )

        # Sell the full position: usd_amount is its current market value, so
        # the notional/qty logic above closes it out rather than trimming it.
        result = execute_order(symbol, "SELL", float(position.market_value), sleeve)
        result["stop_status"] = status
        result["confirmed_unrealized_pct"] = confirmed_pct
        result["single_day_move_pct"] = single_day_pct
        results.append(result)

        if anomaly:
            _open_github_issue(
                title=f"STOP_ANOMALY: {symbol} stopped out on a >{STOP_ANOMALY_SINGLE_DAY_PCT}% single-day move",
                body=(
                    f"{symbol} ({sleeve}) hit its stop-loss ({stop_pct}%) and was sold.\n\n"
                    f"Confirmed unrealized P/L: {confirmed_pct:.2f}%\n"
                    f"Single-day move: {single_day_pct:.2f}% (threshold: {STOP_ANOMALY_SINGLE_DAY_PCT}%)\n\n"
                    "This move is large enough that it's flagged STOP_ANOMALY — the sell "
                    "already executed (a stop-loss never waits on review), but this "
                    "forecast resolution will be excluded from hit-rate stats once "
                    "scorecard.py exists, and the underlying data point is worth a human "
                    "look given the single-vendor data risk accepted for paper trading "
                    "(research/v1-architecture.md §5)."
                ),
            )

    return results
