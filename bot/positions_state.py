import json
import logging
from pathlib import Path

from bot.config import DEFAULT_SLEEVE

logger = logging.getLogger("bot.positions_state")

STATE_PATH = Path("logs") / "positions_state.json"


def load():
    """{symbol: {"sleeve": str, "opened_at": iso-timestamp}} for every
    position this codebase has opened since this file started tracking.
    Not the answer to "what do we hold" on its own — see reconcile(), the
    one place that combines this bookkeeping with Alpaca's live truth.
    """
    if not STATE_PATH.exists():
        return {}
    with open(STATE_PATH) as f:
        return json.load(f)


def save(state):
    STATE_PATH.parent.mkdir(exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def record_open(symbol, sleeve, opened_at):
    state = load()
    state[symbol] = {"sleeve": sleeve, "opened_at": opened_at}
    save(state)


def record_close(symbol):
    state = load()
    state.pop(symbol, None)
    save(state)


def reconcile(live_symbols):
    """The one place sleeve/opened_at truth gets resolved against live
    reality — every caller that needs "what sleeve is X, since when" goes
    through this instead of independently load()-ing this file and falling
    back to DEFAULT_SLEEVE per symbol on its own. That independent-fallback
    pattern is exactly how briefing.py drifted from everything else: it
    never actually read this file at all, silently hardcoding DEFAULT_SLEEVE
    since before this module existed (found 2026-07-24 while tracing a
    dust-remnant position — a $50 smoke-test BUY/SELL whose fractional
    remainder Alpaca never fully zeroed out, left held after record_close()
    had already dropped the tracked entry).

    Prunes tracked entries for symbols no longer actually held (stale), and
    logs a warning — once per run, not silently per call — for any live-held
    symbol with no tracked entry before defaulting it. Returns
    {symbol: {"sleeve", "opened_at"}} for every symbol in `live_symbols`;
    `opened_at` is None for an untracked/defaulted entry.
    """
    state = load()
    live_symbols = set(live_symbols)

    stale = set(state) - live_symbols
    for symbol in stale:
        logger.info("Dropping stale positions_state entry for %s (no longer held)", symbol)
        del state[symbol]
    if stale:
        save(state)

    untracked = sorted(s for s in live_symbols if s not in state)
    if untracked:
        logger.warning(
            "Held per Alpaca but untracked in positions_state.json (no sleeve/opened_at), "
            "falling back to DEFAULT_SLEEVE (%s): %s. Likely a manually-placed trade, a "
            "position opened before this file existed, or a SELL whose fill left a "
            "fractional dust remainder after record_close() already dropped the entry.",
            DEFAULT_SLEEVE,
            ", ".join(untracked),
        )

    return {
        symbol: state.get(symbol, {"sleeve": DEFAULT_SLEEVE, "opened_at": None}) for symbol in live_symbols
    }
