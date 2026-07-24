import json
import logging

import anthropic
from anthropic import Anthropic

from bot.config import (
    ANTHROPIC_API_KEY,
    BRAIN_MODEL_FULL,
    BRAIN_MODEL_LIGHT,
    OUTPUT_TOKENS_PER_CANDIDATE,
    SHADOW_MODEL,
)

logger = logging.getLogger("bot.brain")

_client = Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = (
    "You are the INVESTMENT BRAIN of a two-brain trading system — an elite buy-side "
    "broker with 25 years across macro and equities, running the AGGRESSIVE SATELLITE "
    "of the owner's portfolio (their passive backbone exists elsewhere). You hunt "
    "momentum and quality-at-a-reasonable-price and are explicitly paranoid about "
    "regime shifts. Judge every candidate inside its NEIGHBORHOOD: sector trend, peer "
    "confirmation, relative strength — a stock leading a moving pack and a stock "
    "moving alone are different bets; always state which one you're proposing. Every "
    "proposal must contain a FALSIFIABLE thesis (what evidence would prove you wrong), "
    "neighborhood evidence, named risks, and 1-week/1-month/3-month directional "
    "forecasts with confidence. You read the latest Strategy Memo summary and current "
    "tunable config, and operate within them. You cite specific briefing data, state "
    "uncertainty honestly, and hate churn — you have 5 risk-adding trades per week and "
    "treat them as precious. Long-only mechanics; bearish market views only via SH "
    "inside the DEFENSIVE sleeve; never park in broad ETFs long-term. Sleeves: "
    "MOMENTUM (confirmed trends), QUALITY_VALUE (resilient names at reasonable prices "
    "— judge via low volatility, drawdown resilience, durable trend), DEFENSIVE (cash, "
    "TLT/IEF/GLD/XLP/XLU, SH). Position rules: stock ≤8% of NAV, ETF ≤20%, 10–15 holdings, "
    "min $100 per open/add. At the holdings cap, a new BUY must be paired with the SELL "
    "of your weakest holding — name it and justify the swap."
)

_FORECAST_SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "enum": ["UP", "FLAT", "DOWN"]},
        "confidence": {"type": "number", "description": "0.0-1.0"},
        "rationale": {"type": "string", "description": "<=20 words, must cite briefing data"},
    },
    "required": ["direction", "confidence", "rationale"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA = {
    "type": "object",
    "$defs": {"forecast": _FORECAST_SCHEMA},
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "forecasts": {
                        "type": "object",
                        "properties": {
                            "h1w": {"$ref": "#/$defs/forecast"},
                            "h1m": {"$ref": "#/$defs/forecast"},
                            "h3m": {"$ref": "#/$defs/forecast"},
                        },
                        "required": ["h1w", "h1m", "h3m"],
                        "additionalProperties": False,
                    },
                    "proposal": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["BUY", "SELL", "TRIM", "HOLD"]},
                            "sleeve": {"type": "string", "enum": ["MOMENTUM", "QUALITY_VALUE", "DEFENSIVE"]},
                            "usd_amount": {"type": "number"},
                            "confidence": {"type": "number", "description": "0.0-1.0"},
                            "thesis": {"type": "string", "description": "<=2 sentences"},
                            "falsifier": {"type": "string", "description": "<=1 sentence"},
                            "risks": {"type": "array", "items": {"type": "string"}, "description": "<=3 short items"},
                        },
                        "required": ["action", "sleeve", "usd_amount", "confidence", "thesis", "falsifier", "risks"],
                        "additionalProperties": False,
                    },
                },
                "required": ["symbol", "forecasts", "proposal"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}


def _estimate_tokens(text):
    return round(len(text) / 4)


def _usage_dict(response):
    usage = response.usage
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
    }


def _call(model, user_text, max_tokens, effort=None, disable_thinking=False):
    output_config = {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}}
    if effort is not None:
        output_config["effort"] = effort

    kwargs = {}
    if disable_thinking:
        # Sonnet 5 runs adaptive thinking by default when `thinking` is
        # omitted, and thinking tokens count against the same max_tokens
        # budget as the JSON output — verified live: a single-candidate call
        # burned 81 thinking tokens before writing a byte of the schema'd
        # response. This task doesn't need extended reasoning per candidate
        # (the schema already forces a structured, scoped answer), so
        # thinking is disabled to make the token budget predictable.
        # Haiku 4.5 (light run) never sets this — omitting `thinking`
        # already means no-thinking on that model tier, and passing an
        # explicit {"type": "disabled"} isn't documented as accepted there.
        kwargs["thinking"] = {"type": "disabled"}

    return _client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        output_config=output_config,
        messages=[{"role": "user", "content": user_text}],
        **kwargs,
    )


def _parse_candidates(response):
    """None on any failure — G10: broken output always falls back to HOLD, never a retry-with-improvisation."""
    if response.stop_reason == "refusal":
        logger.warning("Brain call refused: %s", getattr(response, "stop_details", None))
        return None
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        logger.warning("Brain response had no text block (stop_reason=%s)", response.stop_reason)
        return None
    try:
        return json.loads(text)["candidates"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("Brain output failed to parse despite schema constraint: %s", exc)
        return None


def _run(model, briefing_text, sections, candidate_count, effort, disable_thinking=False, max_tokens=None):
    if max_tokens is None:
        # Batch-call budget: shared across candidate_count candidates in one
        # response. Floor raised 400->900 (2026-07-29 build review) after a
        # real 1-holding light-run briefing truncated mid-JSON at 550 tokens
        # on Haiku 4.5 — the per-candidate JSON envelope overhead is
        # proportionally larger at low candidate counts than the 12-candidate
        # full-run case OUTPUT_TOKENS_PER_CANDIDATE was calibrated against.
        # Superseded for light runs specifically by LIGHT_RUN_TOKENS_PER_CALL
        # below (2026-07-24 production run: even that 900-token *shared*
        # budget, effectively a per-candidate budget for a 1-holding run,
        # still truncated — proving a shared/derived budget isn't reliably
        # enough even at n=1, let alone n>1 where one verbose candidate can
        # crowd out every other candidate's tokens in the same response).
        max_tokens = max(900, candidate_count * OUTPUT_TOKENS_PER_CANDIDATE)
    try:
        response = _call(model, briefing_text, max_tokens, effort=effort, disable_thinking=disable_thinking)
    except anthropic.APIError as exc:
        logger.error("Brain API call failed (%s) — falling back to HOLD.", exc)
        return {
            "model": model,
            "candidates": None,
            "fallback": True,
            "usage": None,
            "section_estimates": {name: _estimate_tokens(t) for name, t in sections.items()},
        }

    candidates = _parse_candidates(response)
    return {
        "model": model,
        "candidates": candidates,
        "fallback": candidates is None,
        "usage": _usage_dict(response),
        "section_estimates": {
            **{name: _estimate_tokens(t) for name, t in sections.items()},
            "system_prompt": _estimate_tokens(SYSTEM_PROMPT),
            "output": _usage_dict(response)["output_tokens"],
        },
    }


def run_full_analysis(briefing, candidate_count, effort="medium"):
    """Sonnet, full judgment. `briefing` is briefing.build_briefing()'s return value."""
    result = _run(
        BRAIN_MODEL_FULL, briefing["text"], briefing["sections"], candidate_count, effort, disable_thinking=True
    )

    shadow = None
    if SHADOW_MODEL:
        # Identical briefing, second model, same call shape — proposals are
        # tagged SHADOW by the caller and journaled/forecast-ledgered but
        # never passed to execution. This is the evidence-based way to
        # decide if a stronger model earns its premium (enable after 2-3
        # weeks of baseline; changelog it — build-guide step 11).
        shadow = _run(
            SHADOW_MODEL, briefing["text"], briefing["sections"], candidate_count, effort, disable_thinking=True
        )
    return result, shadow


def _merge_usage(total, addition):
    for key in total:
        total[key] += addition.get(key, 0)


# Dedicated, generous, fixed budget for light-run calls — not derived from
# OUTPUT_TOKENS_PER_CANDIDATE's batch formula. Every light-run call is
# single-candidate by construction (see run_light_analysis), so this budget
# never has to be shared or divided; it just needs headroom against one
# candidate's natural response-length variance. Verified live 2026-07-24:
# a production run truncated a single held position (AAPL) at 900 tokens
# despite that already being an effectively per-candidate budget at n=1 —
# 1800 gives a 2x margin over the observed failure, at a cost that's
# immaterial on Haiku pricing ($5/MTok output) even at PRD's 15-holding max.
LIGHT_RUN_TOKENS_PER_CALL = 1800


def run_light_analysis(briefing, candidate_symbols):
    """Haiku, restricted to monitor/trim/de-risk, briefed on held positions
    only (main.py's job to pass a held-only briefing — no discovery
    candidates, since light runs never open new positions).

    Haiku 4.5 doesn't accept `output_config.effort` (errors on that model
    tier), so it's omitted here entirely rather than passed as None-meaning-
    default. This function's code-level backstop remains even though
    risk.py now exists and independently rejects light-run BUYs — defense
    in depth costs nothing here.

    One individual API call per held symbol, ALWAYS — not a batch call with
    a per-holding fallback for stragglers (the original design, replaced
    2026-07-24 after two live findings). First: Haiku 4.5 doesn't reliably
    follow the "one entry per candidate" coverage instruction that Sonnet
    follows reliably (returned 1 of 12 candidates, stop_reason "end_turn" —
    a deliberate stop, not truncation). Second, and more fundamental: a
    shared batch token budget means one candidate's natural verbosity can
    truncate every candidate in the same response — verified in production,
    a single held position alone hit this. Per-holding-always with its own
    dedicated LIGHT_RUN_TOKENS_PER_CALL budget makes that failure mode
    structurally impossible: no candidate's output ever competes with
    another's for tokens, and a failure on one symbol can't cascade to wipe
    out the others. With light runs bounded by PRD's 10-15 holdings, the
    extra API calls this costs are immaterial on Haiku's pricing — the
    input tokens re-send the same held-only briefing per call rather than
    caching it across calls, and even that isn't worth optimizing at this
    scale.
    """
    usage_total = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    section_estimates_total = {}
    candidates = []

    for symbol in candidate_symbols:
        focused_text = (
            f"{briefing['text']}\n\n"
            f"Focus only on {symbol}. Return exactly one entry in `candidates`, for {symbol} only."
        )
        result = _run(
            BRAIN_MODEL_LIGHT,
            focused_text,
            briefing["sections"],
            1,
            effort=None,
            max_tokens=LIGHT_RUN_TOKENS_PER_CALL,
        )
        if result["usage"] is not None:
            _merge_usage(usage_total, result["usage"])
            for name, tokens in result["section_estimates"].items():
                section_estimates_total[name] = section_estimates_total.get(name, 0) + tokens

        # Keep only the entry matching the symbol this call actually asked
        # about — verified live 2026-07-24: despite "return exactly one
        # entry ... for {symbol} only", a single-symbol focused call can
        # still come back with a second, degenerate entry (empty symbol,
        # zero confidence, empty rationale/thesis). Blindly extending with
        # the whole array puts garbage rows in the forecast ledger.
        matches = [c for c in (result["candidates"] or []) if c["symbol"] == symbol]
        if matches:
            candidates.append(matches[0])
        else:
            logger.error(
                "Light run call for %s failed or returned no matching entry — no proposal "
                "for %s this run (FALLBACK_HOLD: doing nothing is always safe).",
                symbol,
                symbol,
            )

    for candidate in candidates:
        if candidate["proposal"]["action"] == "BUY":
            logger.warning(
                "Light run proposed BUY for %s — code-level restriction downgrades to HOLD "
                "(risk.py also independently rejects this).",
                candidate["symbol"],
            )
            candidate["proposal"]["action"] = "HOLD"
            # Zero the stale BUY amount too, not just the action — otherwise
            # this reads as "APPROVED HOLD AAPL $400.00" in logs/journal,
            # which looks like a HOLD somehow still moved money.
            candidate["proposal"]["usd_amount"] = 0

    return {
        "model": BRAIN_MODEL_LIGHT,
        "candidates": candidates,
        "fallback": not candidates,
        "usage": usage_total,
        "section_estimates": section_estimates_total,
    }
