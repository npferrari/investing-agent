import logging

from bot.config import MAX_CANDIDATES_NEW, UNIVERSE

logger = logging.getLogger("bot.screener")

RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 35
HEADLINE_SPIKE_COUNT = 3

_ETF_SYMBOLS = set(UNIVERSE["ETFS"]) | set(UNIVERSE["HEDGE"])


def _attention_score(indicators, news_count):
    score = abs(indicators["pct_1d"]) * 2
    if indicators["cross"] != "NONE":
        score += 15
    if indicators["rsi14"] >= RSI_OVERBOUGHT or indicators["rsi14"] <= RSI_OVERSOLD:
        score += 10
    if news_count >= HEADLINE_SPIKE_COUNT:
        score += 10 + news_count
    return score


def select_candidates(universe_data, positions, news):
    """Rank the universe by attention score, always briefing held positions.

    `universe_data`: {symbol: indicators} from signals.get_indicators(bars).
    `positions`: Alpaca position objects (held_symbols derived from these).
    `news`: {symbol: [headline, ...]} from news.get_ticker_headlines(...).

    The cap (MAX_CANDIDATES_NEW) applies only to *new* discovery candidates —
    every held position is always briefed, uncapped, so a holding never loses
    forecast coverage just because the book is near steady-state size. If
    held positions alone reach or exceed the cap, discovery slots are simply
    zero for this run (logged) rather than silently dropping a holding.
    """
    held_symbols = {p["symbol"] for p in positions}

    scores = {
        symbol: _attention_score(indicators, len(news.get(symbol, [])))
        for symbol, indicators in universe_data.items()
    }

    ranked_new = sorted(
        (s for s in universe_data if s not in held_symbols),
        key=lambda s: scores[s],
        reverse=True,
    )

    new_slots = max(0, MAX_CANDIDATES_NEW - len(held_symbols))
    if len(held_symbols) >= MAX_CANDIDATES_NEW:
        logger.warning(
            "Held positions alone (%d) meet or exceed MAX_CANDIDATES_NEW (%d) — "
            "zero discovery slots this run; every held position is still briefed.",
            len(held_symbols),
            MAX_CANDIDATES_NEW,
        )

    discovery = ranked_new[:new_slots]

    if discovery and not any(s in _ETF_SYMBOLS for s in discovery):
        top_etf = next((s for s in ranked_new if s in _ETF_SYMBOLS), None)
        if top_etf is not None:
            logger.info("Swapping in %s for ETF coverage (weakest discovery slot bumped).", top_etf)
            discovery[-1] = top_etf

    candidates = sorted(held_symbols) + discovery

    logger.info(
        "Screener: %d held + %d discovery = %d candidates",
        len(held_symbols),
        len(discovery),
        len(candidates),
    )
    for symbol in candidates:
        logger.info("  %s attention_score=%.1f", symbol, scores.get(symbol, float("nan")))

    return candidates, scores
