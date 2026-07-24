from bot.config import UNIVERSE
from bot.risk import APPROVED, REJECTED, validate_proposals

_ALL_VALIDATED = set(UNIVERSE["STOCKS"]) | set(UNIVERSE["ETFS"]) | set(UNIVERSE["HEDGE"])


def base_state(**overrides):
    state = {
        "equity": 10000.0,
        "positions": [],
        "regime": "MIXED",
        "breaker_status": "TRADING_OK",
        "run_mode": "FULL",
        "dwell_ok": True,
        "in_construction_window": False,
        "risk_adding_trades_this_week": 0,
        "validated_symbols": set(_ALL_VALIDATED),
    }
    state.update(overrides)
    return state


def base_proposal(**overrides):
    proposal = {
        "symbol": "AAPL",
        "action": "BUY",
        "sleeve": "MOMENTUM",
        "usd_amount": 500,
        "confidence": 0.7,
    }
    proposal.update(overrides)
    return proposal


# --- G3: universe allowlist -------------------------------------------------


def test_hallucinated_ticker_rejected():
    proposal = base_proposal(symbol="SCAM")
    result = validate_proposals([proposal], base_state())[0]
    assert result["status"] == REJECTED
    assert result["guardrail"] == "G3"


def test_data_gate_fails_for_unvalidated_buy():
    state = base_state(validated_symbols=_ALL_VALIDATED - {"AAPL"})
    result = validate_proposals([base_proposal()], state)[0]
    assert result["status"] == REJECTED
    assert result["guardrail"] == "G3"


def test_data_gate_not_checked_for_sell():
    state = base_state(
        validated_symbols=_ALL_VALIDATED - {"AAPL"},
        positions=[{"symbol": "AAPL", "qty": 1, "market_value": 500, "sleeve": "MOMENTUM"}],
    )
    proposal = base_proposal(action="SELL", usd_amount=500)
    result = validate_proposals([proposal], state)[0]
    assert result["status"] == APPROVED


# --- G2: long-only; SH only inside DEFENSIVE --------------------------------


def test_sh_buy_outside_defensive_rejected():
    proposal = base_proposal(symbol="SH", sleeve="MOMENTUM", usd_amount=500)
    result = validate_proposals([proposal], base_state())[0]
    assert result["status"] == REJECTED
    assert result["guardrail"] == "G2"


def test_sh_buy_inside_defensive_approved():
    proposal = base_proposal(symbol="SH", sleeve="DEFENSIVE", usd_amount=500)
    result = validate_proposals([proposal], base_state())[0]
    assert result["status"] == APPROVED


def test_sell_nonheld_symbol_rejected_as_short():
    proposal = base_proposal(action="SELL", usd_amount=500)
    result = validate_proposals([proposal], base_state())[0]
    assert result["status"] == REJECTED
    assert result["guardrail"] == "G2"


def test_trim_exceeding_held_value_rejected_as_short():
    state = base_state(positions=[{"symbol": "AAPL", "qty": 1, "market_value": 300, "sleeve": "MOMENTUM"}])
    proposal = base_proposal(action="TRIM", usd_amount=500)
    result = validate_proposals([proposal], state)[0]
    assert result["status"] == REJECTED
    assert result["guardrail"] == "G2"


# --- G5: position caps -------------------------------------------------------


def test_stock_over_8pct_cap_rejected():
    proposal = base_proposal(usd_amount=900)  # 9% of $10,000, over the 8% stock cap
    result = validate_proposals([proposal], base_state())[0]
    assert result["status"] == REJECTED
    assert result["guardrail"] == "G5"


def test_etf_over_20pct_cap_rejected():
    proposal = base_proposal(symbol="SPY", usd_amount=2100)  # 21% of $10,000, over the 20% ETF cap
    result = validate_proposals([proposal], base_state())[0]
    assert result["status"] == REJECTED
    assert result["guardrail"] == "G5"


def test_min_amount_rejected_for_buy():
    proposal = base_proposal(usd_amount=50)
    result = validate_proposals([proposal], base_state())[0]
    assert result["status"] == REJECTED
    assert result["guardrail"] == "G5"


def test_trim_exempt_from_min_amount():
    state = base_state(positions=[{"symbol": "AAPL", "qty": 1, "market_value": 300, "sleeve": "MOMENTUM"}])
    proposal = base_proposal(action="TRIM", usd_amount=50)
    result = validate_proposals([proposal], state)[0]
    assert result["status"] == APPROVED


def test_cash_floor_violation_rejected():
    # Cash floor is 10% of $10,000 = $1,000. Existing positions leave $1,200
    # cash; an $800 stock buy (within the 8% cap on its own) would drop cash
    # to $400, below the floor.
    state = base_state(positions=[{"symbol": "MSFT", "qty": 1, "market_value": 8800, "sleeve": "MOMENTUM"}])
    proposal = base_proposal(usd_amount=800)
    result = validate_proposals([proposal], state)[0]
    assert result["status"] == REJECTED
    assert result["guardrail"] == "G5"


def test_max_holdings_without_swap_rejected():
    stocks = UNIVERSE["STOCKS"][:15]
    positions = [{"symbol": s, "qty": 1, "market_value": 400, "sleeve": "MOMENTUM"} for s in stocks]
    state = base_state(positions=positions)
    new_symbol = UNIVERSE["STOCKS"][15]
    proposal = base_proposal(symbol=new_symbol, usd_amount=400)
    result = validate_proposals([proposal], state)[0]
    assert result["status"] == REJECTED
    assert result["guardrail"] == "G5"


def test_swap_at_the_cap_approved():
    stocks = UNIVERSE["STOCKS"][:15]
    positions = [{"symbol": s, "qty": 1, "market_value": 400, "sleeve": "MOMENTUM"} for s in stocks]
    state = base_state(positions=positions)
    new_symbol = UNIVERSE["STOCKS"][15]
    sell = base_proposal(symbol=stocks[0], action="SELL", usd_amount=400)
    buy = base_proposal(symbol=new_symbol, usd_amount=400)
    results = validate_proposals([sell, buy], state)
    by_symbol = {r["symbol"]: r for r in results}
    assert by_symbol[stocks[0]]["status"] == APPROVED
    assert by_symbol[new_symbol]["status"] == APPROVED


def test_valid_buy_approved_baseline():
    result = validate_proposals([base_proposal()], base_state())[0]
    assert result["status"] == APPROVED
    assert result["guardrail"] is None


# --- G4: Defensive floor ------------------------------------------------------


def _defensive_floor_state(regime):
    # 6 stocks at $800 each = $4,800 invested, $5,200 cash (52% of NAV).
    stocks = UNIVERSE["STOCKS"][:6]
    positions = [{"symbol": s, "qty": 1, "market_value": 800, "sleeve": "MOMENTUM"} for s in stocks]
    return base_state(positions=positions, regime=regime)


def test_sleeve_breach_in_danger_regime_rejected():
    state = _defensive_floor_state("RISK_OFF")
    new_symbol = UNIVERSE["STOCKS"][6]
    proposal = base_proposal(symbol=new_symbol, usd_amount=500)  # cash 5200 -> 4700, below the 50% DANGER floor
    result = validate_proposals([proposal], state)[0]
    assert result["status"] == REJECTED
    assert result["guardrail"] == "G4"


def test_same_trade_approved_in_normal_regime():
    state = _defensive_floor_state("MIXED")
    new_symbol = UNIVERSE["STOCKS"][6]
    proposal = base_proposal(symbol=new_symbol, usd_amount=500)  # cash 5200 -> 4700, still above the 15% floor
    result = validate_proposals([proposal], state)[0]
    assert result["status"] == APPROVED


def test_defensive_buy_exempt_from_g4():
    # Same starting cash (5200, DANGER regime), but the proposal itself is a
    # DEFENSIVE buy — cash-neutral for the combined cash+DEFENSIVE total, so
    # it can never trip G4 regardless of size.
    state = _defensive_floor_state("RISK_OFF")
    proposal = base_proposal(symbol="TLT", sleeve="DEFENSIVE", usd_amount=500)
    result = validate_proposals([proposal], state)[0]
    assert result["status"] == APPROVED


# --- G6: breaker freeze --------------------------------------------------------


def test_breaker_freeze_rejects_buy():
    state = base_state(breaker_status="FROZEN_DAY")
    result = validate_proposals([base_proposal()], state)[0]
    assert result["status"] == REJECTED
    assert result["guardrail"] == "G6"


def test_breaker_freeze_rejects_defensive_buy_too():
    state = base_state(breaker_status="FROZEN_WEEK")
    proposal = base_proposal(symbol="TLT", sleeve="DEFENSIVE", usd_amount=500)
    result = validate_proposals([proposal], state)[0]
    assert result["status"] == REJECTED
    assert result["guardrail"] == "G6"


def test_breaker_freeze_allows_sell():
    state = base_state(
        breaker_status="FROZEN_WEEK",
        positions=[{"symbol": "AAPL", "qty": 1, "market_value": 500, "sleeve": "MOMENTUM"}],
    )
    proposal = base_proposal(action="SELL", usd_amount=500)
    result = validate_proposals([proposal], state)[0]
    assert result["status"] == APPROVED


# --- G8: turnover cap -----------------------------------------------------------


def test_turnover_cap_breach_rejected():
    state = base_state(risk_adding_trades_this_week=5)
    result = validate_proposals([base_proposal()], state)[0]
    assert result["status"] == REJECTED
    assert result["guardrail"] == "G8"


def test_turnover_cap_risk_reducing_exempt():
    state = base_state(
        risk_adding_trades_this_week=5,
        positions=[{"symbol": "AAPL", "qty": 1, "market_value": 500, "sleeve": "MOMENTUM"}],
    )
    proposal = base_proposal(action="SELL", usd_amount=500)
    result = validate_proposals([proposal], state)[0]
    assert result["status"] == APPROVED


def test_turnover_cap_defensive_buy_exempt():
    state = base_state(risk_adding_trades_this_week=5)
    proposal = base_proposal(symbol="TLT", sleeve="DEFENSIVE", usd_amount=500)
    result = validate_proposals([proposal], state)[0]
    assert result["status"] == APPROVED


def test_construction_window_exemption():
    state = base_state(risk_adding_trades_this_week=5, in_construction_window=True)
    result = validate_proposals([base_proposal()], state)[0]
    assert result["status"] == APPROVED


# --- G13: dwell ------------------------------------------------------------------


def test_dwell_violation_rejected():
    state = base_state(dwell_ok=False)
    result = validate_proposals([base_proposal()], state)[0]
    assert result["status"] == REJECTED
    assert result["guardrail"] == "G13"


def test_dwell_ok_allows_buy():
    state = base_state(dwell_ok=True)
    result = validate_proposals([base_proposal()], state)[0]
    assert result["status"] == APPROVED


def test_dwell_violation_does_not_block_defensive_buy():
    # Dwell only gates re-risking (non-DEFENSIVE BUYs) — a DEFENSIVE buy is
    # risk-reducing and stays legal even inside the dwell window.
    state = base_state(dwell_ok=False)
    proposal = base_proposal(symbol="TLT", sleeve="DEFENSIVE", usd_amount=500)
    result = validate_proposals([proposal], state)[0]
    assert result["status"] == APPROVED


# --- G9: confidence floor ---------------------------------------------------------


def test_confidence_floor_rejected():
    proposal = base_proposal(confidence=0.4)
    result = validate_proposals([proposal], base_state())[0]
    assert result["status"] == REJECTED
    assert result["guardrail"] == "G9"


# --- light runs --------------------------------------------------------------------


def test_light_run_buy_rejected():
    state = base_state(run_mode="LIGHT")
    result = validate_proposals([base_proposal()], state)[0]
    assert result["status"] == REJECTED
    assert result["guardrail"] == "LIGHT_RUN"


def test_light_run_sell_allowed():
    state = base_state(
        run_mode="LIGHT",
        positions=[{"symbol": "AAPL", "qty": 1, "market_value": 500, "sleeve": "MOMENTUM"}],
    )
    proposal = base_proposal(action="SELL", usd_amount=500)
    result = validate_proposals([proposal], state)[0]
    assert result["status"] == APPROVED
