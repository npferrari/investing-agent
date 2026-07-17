import logging
import sys
import traceback

from bot.config import UNIVERSE
from bot.data import get_daily_bars
from bot.execute import execute_order, get_equity, get_positions
from bot.journal import journal_error, journal_run
from bot.signals import generate_signal, get_indicators, get_regime

logger = logging.getLogger("bot.main")

POSITION_SIZE_PCT = 0.06
SLEEVE = "TACTICAL"


def _skip_already_held(symbol, usd_amount, sleeve):
    logger.info("SKIPPED_ALREADY_HELD BUY %s $%.2f (%s)", symbol, usd_amount, sleeve)
    return {
        "status": "SKIPPED_ALREADY_HELD",
        "symbol": symbol,
        "action": "BUY",
        "usd_amount": usd_amount,
        "sleeve": sleeve,
        "reason": "already holding a position in this ticker",
        "quote": None,
    }


def _run():
    symbols = UNIVERSE["STOCKS"] + UNIVERSE["ETFS"] + UNIVERSE["HEDGE"]
    held_symbols = {p.symbol for p in get_positions()}

    # Sized off live account equity, not a hardcoded dollar figure, so this
    # tracks deposits/withdrawals/resets on the paper account automatically.
    buy_usd_amount = get_equity() * POSITION_SIZE_PCT

    bars = get_daily_bars(symbols, days=250)

    indicators = get_indicators(bars)
    regime = get_regime(bars.loc["SPY"], bars.loc["QQQ"])

    print(f"Regime: {regime}\n")
    header = (
        f"{'Symbol':<8}{'Close':>10}{'EMA9':>10}{'EMA21':>10}{'EMA50':>10}"
        f"{'EMA200':>10}{'%vsEMA200':>11}{'RSI14':>8}{'Cross':>8}{'Signal':>8}"
    )
    print(header)
    print("-" * len(header))

    analyzed = {}
    results = []
    for symbol in symbols:
        ind = indicators[symbol]
        signal = generate_signal(ind, regime)
        analyzed[symbol] = {**ind, "signal": signal}
        print(
            f"{symbol:<8}{ind['close']:>10.2f}{ind['ema9']:>10.2f}{ind['ema21']:>10.2f}"
            f"{ind['ema50']:>10.2f}{ind['ema200']:>10.2f}{ind['pct_from_ema200']:>10.1f}%"
            f"{ind['rsi14']:>8.1f}{ind['cross']:>8}{signal:>8}"
        )

        if signal == "BUY":
            if symbol in held_symbols:
                results.append(_skip_already_held(symbol, buy_usd_amount, SLEEVE))
            else:
                results.append(execute_order(symbol, "BUY", buy_usd_amount, SLEEVE))

    print("\nExecution summary:")
    if not results:
        print("  no BUY signals fired")
    for r in results:
        if r["status"] == "SUBMITTED":
            size = f"notional=${r['notional']:.2f}" if r["notional"] is not None else f"qty={r['qty']}"
            print(
                f"  {r['symbol']:<6} SUBMITTED {size} "
                f"bid={r['quote']['bid']:.2f} ask={r['quote']['ask']:.2f} order_id={r['order_id']}"
            )
        elif r["status"] == "SKIPPED_ALREADY_HELD":
            print(f"  {r['symbol']:<6} SKIPPED_ALREADY_HELD — {r['reason']}")
        else:
            print(f"  {r['symbol']:<6} REJECTED — {r['reason']}")

    journal_run(regime, analyzed, results)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        _run()
    except Exception as exc:
        journal_error(exc)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
