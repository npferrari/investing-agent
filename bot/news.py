import difflib
from datetime import datetime, timedelta, timezone

from alpaca.common.enums import Sort
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

from bot.config import ALPACA_API_KEY, ALPACA_SECRET_KEY

_client = NewsClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

# Proxy for "macro" news: the broad-index ETFs from the universe (build-guide
# §3's "Broad" group). Alpaca's news endpoint has no macro/category filter —
# omitting `symbols` entirely returns the raw firehose of every single-company
# story market-wide (verified: mostly earnings-guidance blurbs for names we
# don't even track), not curated market-wide coverage. Querying against the
# broad-index tickers instead returns what's actually macro in flavor (Fed
# policy, rates, geopolitics, commodity inventories) because that's what gets
# tagged to the index ETFs themselves.
_MACRO_PROXY_SYMBOLS = ("SPY", "QQQ", "DIA", "IWM")

_TITLE_MAX_CHARS = 90
# SequenceMatcher ratio above which two titles are treated as the same story
# reported by different sources (e.g. a Reuters wire picked up verbatim by
# two other outlets with a trimmed headline).
_DUPLICATE_SIMILARITY_THRESHOLD = 0.82


def _normalize(title):
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in title).split())


def _is_duplicate(title_a, title_b):
    return difflib.SequenceMatcher(None, _normalize(title_a), _normalize(title_b)).ratio() >= _DUPLICATE_SIMILARITY_THRESHOLD


def _dedupe(headlines):
    # headlines arrives newest-first (we always request sort=desc), so the
    # first copy of a story seen here is the most recent one — later
    # (older) near-duplicates are simply dropped.
    kept = []
    for headline in headlines:
        if not any(_is_duplicate(headline["title"], k["title"]) for k in kept):
            kept.append(headline)
    return kept


def _to_headline(article, now):
    age_hours = (now - article.created_at).total_seconds() / 3600
    return {
        "title": article.headline[:_TITLE_MAX_CHARS],
        "age_hours": round(age_hours, 1),
        "source": article.source,
        "summary": article.summary,
        # briefing.py flips this to True for news-spike candidates only
        # (max 2 per run) — everyone else's summary rides in the returned
        # dict but never reaches the prompt (T5).
        "include_summary": False,
    }


def get_ticker_headlines(symbols, hours=24, cap=5):
    now = datetime.now(timezone.utc)
    request = NewsRequest(
        symbols=",".join(symbols),
        start=now - timedelta(hours=hours),
        end=now,
        sort=Sort.DESC.value,
        limit=min(len(symbols) * cap * 4, 200),
        include_content=False,
    )
    articles = _client.get_news(request).data.get("news", [])
    headlines = [_to_headline(article, now) for article in articles]

    by_symbol = {symbol: [] for symbol in symbols}
    for article, headline in zip(articles, headlines):
        for symbol in article.symbols:
            if symbol in by_symbol:
                by_symbol[symbol].append(headline)

    return {symbol: _dedupe(candidates)[:cap] for symbol, candidates in by_symbol.items()}


def get_macro_headlines(hours=24, cap=8):
    now = datetime.now(timezone.utc)
    request = NewsRequest(
        symbols=",".join(_MACRO_PROXY_SYMBOLS),
        start=now - timedelta(hours=hours),
        end=now,
        sort=Sort.DESC.value,
        limit=max(cap * 4, 50),
        include_content=False,
    )
    articles = _client.get_news(request).data.get("news", [])
    headlines = [_to_headline(article, now) for article in articles]
    return _dedupe(headlines)[:cap]
