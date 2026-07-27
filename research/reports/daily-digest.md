# Daily digest

Generated 2026-07-27T10:48:07.162398+00:00 by `scripts/journal_report.py` from `logs/journal.jsonl` (+ archive). Regenerate after any run; not hand-edited.

## Token economy — 7-day trend

| Day | Runs | Input tok | Output tok | Cache read | Cache write | Est. cost |
|---|---|---|---|---|---|---|
| 2026-07-17 | 4 | 0 | 0 | 0 | 0 | $0.00 |
| 2026-07-20 | 3 | 0 | 0 | 0 | 0 | $0.00 |
| 2026-07-21 | 3 | 0 | 0 | 0 | 0 | $0.00 |
| 2026-07-22 | 3 | 0 | 0 | 0 | 0 | $0.00 |
| 2026-07-23 | 3 | 0 | 0 | 0 | 0 | $0.00 |
| 2026-07-24 | 2 | 4,347 | 2,669 | 0 | 0 | $0.02 |
| 2026-07-25 | 1 | 5,442 | 4,889 | 0 | 1,619 | $0.06 |

## 2026-07-25

**Runs (1):** 00:58 FULL
**Regime(s):** MIXED  **Breaker state(s):** TRADING_OK
**Equity:** open $5,000.00 -> close $5,000.00

**Screening funnel:** 12 candidates screened -> 4 actionable proposals -> 1 approved (25% approval rate)
**Rejections by guardrail:** G9=3
**Execution-level rejections (1):**
  - BUY UNP: Alpaca rejected the order: {"code":42210000,"message":"fractional orders must be DAY orders"}

**Token economy:**

| Run | Model | Input | Output | Cache read | Cache write | Cost | Sections (tok) |
|---|---|---|---|---|---|---|---|
| FULL | claude-sonnet-5 | 5,442 | 4,889 | 0 | 1,619 | $0.06 | global=29, stat_lines=472, headlines=1473, macro=222, forecast_accuracy=132, instructions=70, system_prompt=372, output=4889 |

**Day total est. cost:** $0.06

## 2026-07-24

**Runs (2):** 16:53 LIGHT, 18:09 LIGHT
**Regime(s):** MIXED  **Breaker state(s):** TRADING_OK
**Equity:** open $5,000.00 -> close $5,000.00

**Screening funnel:** 1 candidates screened -> 0 actionable proposals -> 0 approved (n/a approval rate)

**Token economy:**

| Run | Model | Input | Output | Cache read | Cache write | Cost | Sections (tok) |
|---|---|---|---|---|---|---|---|
| LIGHT | claude-haiku-4-5 | 2,173 | 869 | 0 | 0 | $0.01 | global=29, stat_lines=46, headlines=193, macro=230, forecast_accuracy=11, instructions=54, system_prompt=372, output=869 |
| LIGHT | claude-haiku-4-5 | 2,174 | 1,800 | 0 | 0 | $0.01 | global=29, stat_lines=46, headlines=193, macro=226, forecast_accuracy=11, instructions=54, system_prompt=372, output=1800 |

**Day total est. cost:** $0.02

## 2026-07-23

**Runs (3):** 3 pre-pipeline (v0) run(s)
**Regime(s):** MIXED, RISK_OFF  **Breaker state(s):** TRADING_OK
**Equity:** open $5,000.00 -> close $5,000.00


## 2026-07-22

**Runs (3):** 3 pre-pipeline (v0) run(s)
**Regime(s):** MIXED, RISK_ON  **Breaker state(s):** TRADING_OK
**Equity:** open $5,000.00 -> close $5,000.00


## 2026-07-21

**Runs (3):** 3 pre-pipeline (v0) run(s)
**Regime(s):** RISK_ON  **Breaker state(s):** TRADING_OK
**Equity:** open $5,000.00 -> close $5,000.00


## 2026-07-20

**Runs (3):** 3 pre-pipeline (v0) run(s)
**Regime(s):** MIXED  **Breaker state(s):** TRADING_OK
**Equity:** open $5,000.00 -> close $5,000.00


## 2026-07-17

**Runs (4):** 4 pre-pipeline (v0) run(s)
**Regime(s):** MIXED  **Breaker state(s):** TRADING_OK
**Equity:** open $5,000.00 -> close $5,000.00


## 2026-07-16

**Runs (4):** 4 pre-pipeline (v0) run(s)
**Regime(s):** MIXED, RISK_ON  **Breaker state(s):** n/a
**Equity:** open $100,014.78 -> close $100,056.72

**Trades submitted by sleeve:**
  - TACTICAL: 3 trade(s), $3,000.00 notional
