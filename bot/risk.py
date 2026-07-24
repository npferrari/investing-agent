import logging

from bot.config import RISK, UNIVERSE

logger = logging.getLogger("bot.risk")

APPROVED = "APPROVED"
REJECTED = "REJECTED"

MOMENTUM = "MOMENTUM"
QUALITY_VALUE = "QUALITY_VALUE"
DEFENSIVE = "DEFENSIVE"

_STOCK_SYMBOLS = set(UNIVERSE["STOCKS"])
_ETF_SYMBOLS = set(UNIVERSE["ETFS"]) | set(UNIVERSE["HEDGE"])
_ALL_SYMBOLS = _STOCK_SYMBOLS | _ETF_SYMBOLS

# G13's real NORMAL/DANGER regime state machine isn't built yet (bot/regime.py
# — see research/v1-architecture.md §4.5). RISK_OFF from signals.get_regime()
# is used as the DANGER analog here, matching tests/test_brain.py's convention,
# so this module doesn't hardcode a "DANGER" string nothing upstream produces.
_DANGER_REGIME_VALUES = {"DANGER", "RISK_OFF"}

_MAX_HOLDINGS = 15
_MIN_TRADE_USD = 100
_CASH_FLOOR_PCT = RISK["cash_floor_pct"]
_TURNOVER_CAP = 5


def _is_risk_adding(proposal):
    """A BUY into MOMENTUM or QUALITY_VALUE. Moves into DEFENSIVE are risk-
    reducing, same as SELL/TRIM (PRD §4: "Risk-reducing trades ... moves
    into the Defensive sleeve — never count against the cap")."""
    return proposal["action"] == "BUY" and proposal.get("sleeve") != DEFENSIVE


def _reject(proposal, code, reason):
    logger.warning(
        "REJECTED %s %s (%s) [%s]: %s",
        proposal["action"],
        proposal["symbol"],
        proposal.get("sleeve"),
        code,
        reason,
    )
    return {**proposal, "status": REJECTED, "guardrail": code, "reason": reason}


def _approve(proposal):
    logger.info(
        "APPROVED %s %s (%s) $%.2f",
        proposal["action"],
        proposal["symbol"],
        proposal.get("sleeve"),
        proposal.get("usd_amount", 0),
    )
    return {**proposal, "status": APPROVED, "guardrail": None, "reason": None}


def _validate_one(proposal, state, working_positions, working_cash, equity, risk_adding_count):
    symbol = proposal["symbol"]
    action = proposal["action"]
    sleeve = proposal.get("sleeve")
    usd_amount = proposal.get("usd_amount", 0)
    confidence = proposal.get("confidence", 0)

    def reject(code, reason):
        return _reject(proposal, code, reason), working_positions, working_cash, risk_adding_count

    # Light runs may never BUY — cheapest, most categorical check, ahead of
    # every state-dependent guardrail.
    if state["run_mode"] == "LIGHT" and action == "BUY":
        return reject("LIGHT_RUN", "light runs are restricted to monitor/trim/de-risk — no new BUYs")

    # G2 — long-only; SH only inside DEFENSIVE.
    if symbol == "SH" and action == "BUY" and sleeve != DEFENSIVE:
        return reject("G2", "SH may only be bought into the DEFENSIVE sleeve")
    if action in ("SELL", "TRIM"):
        held_value = working_positions.get(symbol, {}).get("market_value", 0)
        if action == "SELL" and held_value <= 0:
            return reject("G2", f"{symbol} is not currently held — SELL would open a short position")
        if action == "TRIM" and usd_amount > held_value:
            return reject(
                "G2",
                f"TRIM of ${usd_amount:.2f} exceeds held value ${held_value:.2f} on {symbol} — would go short",
            )

    # G3 — universe allowlist (all actions) + data-validation gate (BUYs only —
    # exits should never be blocked by stale data on a name we're trying to
    # de-risk out of, same spirit as G6/G8's risk-reducing exemptions).
    if symbol not in _ALL_SYMBOLS:
        return reject("G3", f"{symbol} is outside UNIVERSE")
    if action == "BUY" and symbol not in state.get("validated_symbols", set()):
        return reject("G3", f"{symbol} has not passed the data-validation gate this run")

    # G5 — position caps. Only BUYs can breach a cap; SELL/TRIM only ever
    # shrink a position or free cash, so they're exempt from every check here.
    if action == "BUY":
        if usd_amount < _MIN_TRADE_USD:
            return reject("G5", f"${usd_amount:.2f} is below the ${_MIN_TRADE_USD} minimum for opens/adds")

        existing_value = working_positions.get(symbol, {}).get("market_value", 0)
        projected_value = existing_value + usd_amount
        cap_pct = 8 if symbol in _STOCK_SYMBOLS else 20
        cap_value = equity * cap_pct / 100
        if projected_value > cap_value:
            return reject(
                "G5",
                f"${projected_value:.2f} would exceed the {cap_pct}% NAV cap (${cap_value:.2f}) for {symbol}",
            )

        is_new_position = existing_value <= 0
        if is_new_position:
            held_count = sum(1 for p in working_positions.values() if p["market_value"] > 0)
            if held_count >= _MAX_HOLDINGS:
                return reject("G5", f"would exceed the {_MAX_HOLDINGS}-holding maximum")

        projected_cash = working_cash - usd_amount
        cash_floor_value = equity * _CASH_FLOOR_PCT / 100
        if projected_cash < cash_floor_value:
            return reject(
                "G5",
                f"would drop cash to ${projected_cash:.2f}, below the {_CASH_FLOOR_PCT}% NAV floor (${cash_floor_value:.2f})",
            )

    # G4 — Defensive floor (cash + DEFENSIVE-sleeve holdings combined, per
    # PRD §2's "Defensive sleeve (cash + defensive ETFs)"), >=15% always,
    # >=50% in a DANGER-analog regime. Only a non-DEFENSIVE BUY can breach
    # this: it's the only trade type that pulls value out of the combined
    # cash+DEFENSIVE pool without adding it back in. A DEFENSIVE BUY moves
    # cash into a DEFENSIVE holding (both already counted) — a wash. A
    # SELL/TRIM of anything converts to cash (still counted) — never a
    # reduction. So the check only needs to run for sleeve != DEFENSIVE BUYs.
    if action == "BUY" and sleeve != DEFENSIVE:
        projected_cash = working_cash - usd_amount
        projected_defensive_value = projected_cash + sum(
            p["market_value"] for p in working_positions.values() if p.get("sleeve") == DEFENSIVE
        )
        floor_pct = 50 if state["regime"] in _DANGER_REGIME_VALUES else 15
        floor_value = equity * floor_pct / 100
        if projected_defensive_value < floor_value:
            return reject(
                "G4",
                f"would drop Defensive (cash+holdings) to ${projected_defensive_value:.2f}, "
                f"below the {floor_pct}% NAV floor (${floor_value:.2f})",
            )

    # G6 — breaker freeze blocks BUYs of every sleeve, including DEFENSIVE.
    # Cash is the freeze's only destination; proactive Defensive rotation is
    # G13's job, not G6's (confirmed decision, research/v1-architecture.md §5,
    # 2026-07-28). SELL/TRIM are always legal — a freeze never blocks exits.
    if action == "BUY" and state["breaker_status"] != "TRADING_OK":
        return reject("G6", f"breaker is {state['breaker_status']} — no new BUYs of any sleeve during a freeze")

    # G8 — turnover cap: <=5 risk-adding trades per rolling week. Risk-reducing
    # trades (SELL, TRIM, and BUYs into DEFENSIVE) are exempt, and the whole
    # check is suspended during the 5-day construction window.
    if _is_risk_adding(proposal) and not state.get("in_construction_window", False):
        if risk_adding_count >= _TURNOVER_CAP:
            return reject("G8", f"would exceed {_TURNOVER_CAP} risk-adding trades this rolling week")

    # G13 — dwell: a risk-adding BUY is blocked while still inside the
    # post-DANGER dwell window, regardless of what the regime string says
    # right now (the whole point of a dwell timer is not trusting the first
    # tick back to NORMAL).
    if _is_risk_adding(proposal) and not state.get("dwell_ok", True):
        return reject("G13", "still within the post-DANGER dwell window — re-risking not yet allowed")

    # G9 — confidence floor on risk-adding ideas only ("cheap filter on weak
    # ideas" — a low-confidence exit or defensive move isn't a weak idea in
    # the same sense, so it isn't filtered here).
    if action == "BUY" and confidence < RISK["confidence_floor"]:
        return reject("G9", f"confidence {confidence:.2f} is below the {RISK['confidence_floor']} floor")

    # Approved — update the working state so later proposals in this same
    # batch see the effect (this is what makes a SELL-then-BUY swap at the
    # holdings cap work: the SELL frees a slot before the BUY is checked).
    if action == "BUY":
        working_cash -= usd_amount
        existing = working_positions.get(symbol, {"symbol": symbol, "qty": 0, "market_value": 0, "sleeve": sleeve})
        existing["market_value"] = existing.get("market_value", 0) + usd_amount
        existing["sleeve"] = sleeve
        working_positions[symbol] = existing
        if _is_risk_adding(proposal):
            risk_adding_count += 1
    elif action == "SELL":
        held = working_positions.get(symbol)
        if held:
            working_cash += held["market_value"]
            held["market_value"] = 0
    elif action == "TRIM":
        held = working_positions.get(symbol)
        if held:
            working_cash += usd_amount
            held["market_value"] -= usd_amount

    return _approve(proposal), working_positions, working_cash, risk_adding_count


def validate_proposals(proposals, state):
    """G2-G9 + G13, deterministic, in order. No brain can override.

    `proposals`: flat list of dicts, each {"symbol", "action", "sleeve",
    "usd_amount", "confidence"} — the caller flattens brain.py's nested
    {"symbol", "forecasts", "proposal"} candidate shape before calling this
    (forecasts aren't risk-relevant; only the proposal is).

    `state`: {
        "equity": float — current NAV,
        "positions": [{"symbol", "qty", "market_value", "sleeve"}, ...] —
            current holdings as plain dicts, not Alpaca objects (decouples
            this module from the broker SDK and makes it trivially testable),
        "regime": str — "RISK_ON"|"RISK_OFF"|"MIXED" (RISK_OFF == DANGER
            analog — see _DANGER_REGIME_VALUES),
        "breaker_status": str — "TRADING_OK"|"FROZEN_DAY"|"FROZEN_WEEK",
        "run_mode": str — "FULL"|"LIGHT",
        "dwell_ok": bool — G13; False blocks new risk-adding BUYs regardless
            of what the regime string says right now,
        "in_construction_window": bool — G8 exemption,
        "risk_adding_trades_this_week": int — G8's count so far, NOT
            including the proposals passed in this call,
        "validated_symbols": set — G3 data-gate: symbols with fresh, sane
            data this run,
    }

    Returns one result dict per input proposal (input fields plus
    status/guardrail/reason), in the order: SELLs and TRIMs first, then
    BUYs, then everything else (HOLD, always approved — nothing to gate).
    Processing SELL/TRIM before BUY is what makes "a new BUY paired with the
    SELL of your weakest holding" actually work at the holdings cap: the
    SELL's effect on the working state is visible to the BUY's checks.
    """
    equity = state["equity"]
    working_positions = {p["symbol"]: dict(p) for p in state["positions"]}
    working_cash = equity - sum(p["market_value"] for p in working_positions.values())
    risk_adding_count = state["risk_adding_trades_this_week"]

    sells = [p for p in proposals if p["action"] in ("SELL", "TRIM")]
    buys = [p for p in proposals if p["action"] == "BUY"]
    others = [p for p in proposals if p["action"] not in ("SELL", "TRIM", "BUY")]

    results = []
    for proposal in sells + buys:
        result, working_positions, working_cash, risk_adding_count = _validate_one(
            proposal, state, working_positions, working_cash, equity, risk_adding_count
        )
        results.append(result)

    results.extend(_approve(p) for p in others)
    return results
