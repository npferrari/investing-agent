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


def _run(model, briefing_text, sections, candidate_count, effort, disable_thinking=False):
    max_tokens = max(400, candidate_count * OUTPUT_TOKENS_PER_CANDIDATE)
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


def run_light_analysis(briefing, candidate_count):
    """Haiku, restricted to monitor/trim/de-risk.

    Haiku 4.5 doesn't accept `output_config.effort` (errors on that model
    tier), so it's omitted here entirely rather than passed as None-meaning-
    default. The prompt already tells the model this is a light run (via
    the global block's regime/breaker context, not a special system prompt —
    step 13 owns wiring run-mode framing into the persona); this function's
    job is the *code-level* backstop: any BUY the model proposes anyway is
    downgraded to HOLD here, logged, because risk.py (next step) doesn't
    exist yet to be the real enforcement point.

    A live probe (2026-07-23) found that briefing._coverage instruction's
    "one entry per candidate, no omissions" wording, which reliably gets
    full coverage from Sonnet, was NOT reliably followed by Haiku 4.5 — it
    returned 1 of 12 candidates with stop_reason "end_turn" (a deliberate
    stop, not truncation). Build review decision (2026-07-24), noted here
    for step 13's main.py wiring rather than acted on now: light-run
    briefings will contain held positions only (no discovery candidates —
    light runs never open new positions, so screener.select_candidates
    shouldn't even run for them), which both matches the guide's
    monitor/trim/de-risk restriction and shrinks the coverage instruction
    to a much smaller, more tractable candidate count for the weaker model.
    No change to select_candidates or build_briefing themselves yet — this
    is main.py's run-mode branching to build, not this module's.
    """
    result = _run(BRAIN_MODEL_LIGHT, briefing["text"], briefing["sections"], candidate_count, effort=None)
    if result["candidates"]:
        for candidate in result["candidates"]:
            if candidate["proposal"]["action"] == "BUY":
                logger.warning(
                    "Light run proposed BUY for %s — code-level restriction downgrades to HOLD "
                    "(risk.py will own this rejection once built).",
                    candidate["symbol"],
                )
                candidate["proposal"]["action"] = "HOLD"
    return result
