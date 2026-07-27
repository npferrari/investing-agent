import logging
import time

import anthropic
from alpaca.common.exceptions import APIError as AlpacaAPIError
from requests.exceptions import RequestException

from bot.config import API_RETRY_BASE_DELAY_SECONDS, API_RETRY_MAX_ATTEMPTS

logger = logging.getLogger("bot.retry")

# alpaca-py's own RESTClient only retries a 429 internally (see
# alpaca/common/rest.py's RetryException loop) — every other HTTP error
# (4xx/5xx) and every network-level failure (a ConnectionError/Timeout
# raised before any HTTP response comes back at all) propagates raw and
# uncaught. This is the default set of "worth retrying" exceptions for
# every read call in this codebase (Alpaca market data/news/trading, and
# Anthropic) that goes through call_with_retry below.
DEFAULT_RETRYABLE_EXCEPTIONS = (AlpacaAPIError, RequestException, anthropic.APIError)


def call_with_retry(
    fn,
    *args,
    retryable_exceptions=DEFAULT_RETRYABLE_EXCEPTIONS,
    max_attempts=API_RETRY_MAX_ATTEMPTS,
    base_delay_seconds=API_RETRY_BASE_DELAY_SECONDS,
    **kwargs,
):
    """Call fn(*args, **kwargs), retrying up to max_attempts times with
    exponential backoff (base_delay_seconds, then x2, x4, ...) on any
    exception in retryable_exceptions, then re-raising the last one.

    Deliberately NOT used on execute.submit_order(): a network failure
    mid-submission is genuinely ambiguous (did the order reach Alpaca or
    not?), and retrying a mutating, non-idempotent trading action on an
    ambiguous failure risks placing a duplicate real order. Every call site
    this wraps instead is a read (market data, news, account/position/order
    lookups) or the brain's LLM call — retrying those has no such risk,
    worst case is wasted latency or a few cents of duplicate tokens.
    """
    attempt = 1
    while True:
        try:
            return fn(*args, **kwargs)
        except retryable_exceptions as exc:
            label = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", repr(fn))
            if attempt >= max_attempts:
                logger.error("%s failed after %d attempt(s), giving up: %s", label, attempt, exc)
                raise
            delay = base_delay_seconds * (2 ** (attempt - 1))
            logger.warning(
                "%s failed (attempt %d/%d): %s — retrying in %ss", label, attempt, max_attempts, exc, delay
            )
            time.sleep(delay)
            attempt += 1
