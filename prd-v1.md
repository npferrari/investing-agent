# Investing Bot — Product Requirements Document (v1)

**Author** Nicolò Ferrari · **Date** July 2026 · **Status** Draft for review
**Build status** Execution & risk infrastructure live · Decision brains in development (current phase)
**Mode** Paper trading (8 weeks) with defined graduation criteria to real money

---

## 1. Problem & hypothesis

Discretionary retail investing fails in predictable ways: attention is intermittent, names are evaluated in isolation, short-term noise and long-term theses get conflated, risk appetite moves with emotion rather than evidence, and past reasoning is almost never scored against outcomes. Fully rule-based bots solve the discipline problem but are context-blind — they cannot weigh a story against its sector, hold two time horizons on the same name, or recognize that the whole market has changed character.

**Hypothesis.** A two-brain LLM system — one brain making investment decisions, a second brain scoring results and adapting strategy — operating inside hard, code-enforced risk limits, can produce disciplined, context-aware, multi-horizon decisions *and measurably improve its own process* over time.

v1 exists to test this hypothesis in paper trading, with the evidence bar defined before the first trade.

### Design principles (requirements, not aspirations)

1. **Context-aware by construction.** No candidate is ever evaluated in isolation. Every score is relative to a sector/peer neighborhood and the broader tape, so a "cheap" stock in a collapsing industry cannot masquerade as an opportunity.
2. **Multi-horizon by construction.** Every position carries explicit directional forecasts at 1 week / 1 month / 3 months, scored independently. A name can be a short-term trim and a long-term hold at the same time.
3. **A portfolio-level voice.** The system must be able to say "the whole market looks dangerous" and act on it. It is never fully invested by construction; a Defensive sleeve expands in danger regimes.
4. **Self-evaluating by construction.** Outcomes are scored, attributed, and fed back into strategy through a bounded adaptation loop. Learning is a runtime feature, not a post-mortem.

Principles 1–3 shape the Investment Brain; principle 4 is why the Evaluation & Strategy Brain exists.

---

## 2. Mandate & strategy

**Persona & mandate.** The bot runs a satellite sleeve of $5,000 (paper notional) with a medium-high risk profile: it hunts momentum and quality-at-a-reasonable-price, and is explicitly paranoid about regime shifts. Any core holdings sit outside the mandate and are untouched.

**Universe.** US large caps and liquid ETFs (EU exposure via ADRs = v1.5). Every instrument must pass the data-validation gate — freshness, sanity bounds, corporate-action handling — before either brain may act on it. *Single-vendor in v1 (accepted risk, bounded: stop-loss breaches require a confirming second quote; implausible stops are flagged STOP_ANOMALY and excluded from forecast stats). A second price source scoped to the stop-loss sweep is a graduation requirement (§9).*

**Screener + neighborhood analysis.** Candidates are scored relative to their sector/peer neighborhood — momentum, valuation, revisions vs peers — never in isolation.

**Three horizons.** Each position carries 1w / 1m / 3m directional forecasts with a stated confidence level, written to the forecast ledger at creation.

**Three sleeves.** Baseline: Momentum 40% / Quality-value 35% / Defensive 25%. The Defensive sleeve (cash + defensive ETFs) is how the bot expresses market-level fear; in danger regimes it expands to at least 50% of NAV. Sleeve baselines are tunable by the Strategy Brain within the bounds in §4; the regime mechanics (hysteresis, dwell) are not.

**Cadence.**

- **Pre-open run** (light model): defensive-only — monitor, trim, de-risk; no new buys.
- **Midday run** (light model): defensive-only; doubles as the EU-close review.
- **After-close run** (full model): full analysis; may open positions. Orders generated here are simulated as filled at the *next session open* with a slippage haircut (§5) — never at decision-time prices.
- **Weekly deep review:** owned by the Evaluation & Strategy Brain (§3.3) — forecast scoring, attribution, strategy memo, digest email.

The bot operates autonomously. The human checkpoint is the weekly digest plus the code-level guardrails and kill-switch — not per-trade approval.

---

## 3. System architecture — three components

The LLMs propose; code disposes. Two brains, one deterministic layer between them and the market.

### 3.1 Execution & Risk Layer — deterministic code — **BUILT**

- Data-validation gate: the only source of market data either brain may act on.
- Pre-trade guardrail enforcement (§4): every proposed order passes, is clipped, or is hard-blocked and logged.
- Paper order simulation with next-open fills and slippage model; NAV series, trade log, forecast ledger, guardrail log, cost meter.
- Cost kill-switch (§4); regime-state machine with hysteresis and dwell timers.

### 3.2 Investment Brain — LLM — **IN BUILD**

The daily decision-maker. Loop per full run:

1. Ingest validated screener data + current portfolio + regime state + latest Strategy Memo summary.
2. Score candidates vs their neighborhood; re-underwrite existing theses.
3. Produce trade proposals per sleeve. Every proposal must contain: a falsifiable thesis, neighborhood evidence, 1w/1m/3m forecasts with confidence, named risks, and sleeve/mandate fit.
4. Self-check against the mandate, then submit to the Execution Layer — which has final say.

Intraday runs use a smaller model and are structurally restricted to monitor / trim / de-risk.

### 3.3 Evaluation & Strategy Brain — LLM — **IN BUILD**

The results analyst and coach. Deterministic code computes the numbers; this brain interprets them and adapts strategy. Runs weekly (plus a cheap daily scoring job that is pure code).

1. **Forecast scoring.** Every expired forecast scored on direction — both absolute and relative to its benchmark (sector ETF for stocks, ACWI for broad ETFs) — with hit-rates, Wilson 95% CIs, and Brier calibration, by horizon and by sleeve.
2. **Attribution.** Return vs SPY, ACWI, and the static policy-mix benchmark (§5), decomposed into allocation vs selection vs FX.
3. **Reasoning audit.** Pre-grades all rationales on the rubric; the human audits a 10/week sample. The grader runs in a separate context (optionally a different model) from the Investment Brain; human-vs-brain grade disagreement is tracked.
4. **Strategy Memo.** Recommended changes to tunable parameters — each linked to the evidence that motivates it — within the bounds of §4.
5. **Digest email.** Returns vs benchmarks, scored forecasts, guardrail log, spend, and any applied strategy changes. This is the human's weekly checkpoint and veto point.

### 3.4 Inter-brain protocol

The brains never converse free-form. They communicate through versioned artifacts:

- **Shared state:** forecast ledger, trade log, NAV/attribution series, guardrail log, and a **config file of tunables**.
- **Flow:** Strategy Memo → Execution Layer validates each proposed change against hard bounds → applied to config → Investment Brain reads the new config plus a short memo summary on its next run.
- Every change is versioned, evidence-linked, and reported in the digest. The human can veto or roll back any change; anything outside bounds requires explicit human approval before it takes effect.

**Change discipline — enforced in code:**

- Max 2 parameter changes per weekly review.
- Minimum evidence to change: ≥30 scored forecasts, or ≥3 consecutive weekly attributions pointing the same way, for the metric cited.
- Per-parameter cooldown: 2 weeks between changes.
- Oscillation flag: the same parameter moved in opposite directions in consecutive reviews forces a human design review.
- Every change carries a post-change checkpoint in the digest two weeks later.

---

## 4. Hard guardrails — enforced in code, pre-trade; no brain can override

**Immutable by both brains:**

- Long-only; no leverage, shorts, or options.
- Single stock ≤ 8% of NAV (~$400); single ETF ≤ 20%; 10–15 holdings at steady state.
- Cash floor: never below 10%; up to 60% in de-risk mode.
- Turnover cap: max 5 **risk-adding** trades/week (new positions and adds). Risk-reducing trades — trims, closes, moves into the Defensive sleeve — never count against the cap, so the system can always cut risk.
- Initial construction window: the first 5 trading days are exempt from the turnover cap (all other guardrails apply) so the book can reach 10–15 names without a multi-week ramp.
- Minimum trade size $100 on opens and adds (no position is dust); closes and trims exempt.
- Cost kill-switch: rolling 7-day spend > $14 (≈ $2/day) or single-day spend > $4 pauses all runs; positions stand; human notified; resume is a manual action.
- Regime mechanics: entry/exit hysteresis and a minimum 3-day dwell before re-risking are fixed.

**Tunable by the Strategy Brain, within code-enforced bounds (starting set — final values in §8):**

| Parameter | Baseline | Bounds |
|---|---|---|
| Sleeve baselines (M / QV / D) | 40 / 35 / 25 | ±10pp per sleeve per change; Defensive ≥ 15%; sum = 100 |
| Danger-regime Defensive floor | 50% | 40–60% |
| Screener factor weights (momentum / valuation / revisions) | equal | each within 20–50% |
| Horizon emphasis in sizing (1w / 1m / 3m) | equal | each within 20–50% |
| Regime-indicator thresholds | per indicator panel (§8) | predefined range per indicator; hysteresis/dwell fixed |

Universe, cadence, guardrails, and both brains' prompts are **not** tunable by either brain in v1.

---

## 5. Success metrics — 8-week paper evaluation

Process is the primary bar; beating SPY is the stretch goal. Over 8 weeks, returns are partly luck — forecast skill, calibration, reasoning quality, and adaptation discipline are the evidence that compounds.

| Metric | Target | How measured |
|---|---|---|
| Forecast hit-rate vs coin flip | ≥ 55% at 1w (n ≥ 60) and 1m (n ≥ 30), on **benchmark-relative** direction | Scored at expiry vs each name's benchmark (sector ETF / ACWI); absolute-direction hit-rate reported alongside; Wilson 95% CI; treated as directional evidence, not proof, at these sample sizes. 3-month forecasts score after the window (week 13+) and roll into the post-mortem. |
| Calibration | Brier score with improving trend; no confidence bucket inverted | Confidence-weighted scoring across all forecasts. Coverage rule: every holding must carry live forecasts at all horizons — no cherry-picking easy calls. |
| Reasoning quality | ≥ 4/5 average on human-audited sample | Evaluation Brain pre-grades all rationales; human audits 10/week on the rubric (grounded in data, cites neighborhood context, falsifiable thesis, risks named, mandate-consistent). Human–brain disagreement > 1 point on > 20% of audits forces a grader revision. |
| Return vs SPY (stretch) | Beat SPY net of modeled costs; **mandatory floor:** max drawdown no worse than SPY's | Daily USD NAV vs SPY total return, with MSCI ACWI and the **static policy-mix benchmark** (40% momentum ETF / 35% quality-value ETF / 25% defensive mix, rebalanced monthly) alongside; Sharpe and max DD included. The policy-mix comparison isolates selection skill from the allocation itself; the cash floor makes a 100%-invested SPY comparison structurally unfair in up markets. |
| Cost | ≤ $1.50/day average | 2 light runs (~$0.25 ea.) + 1 full run (~$0.55) ≈ $1.05/day; weekly Evaluation & Strategy run (~$2.00) amortized ≈ $0.40/day; daily forecast scoring is deterministic code (≈ $0). Prompt caching + per-run token budgets; kill-switch per §4. |
| Guardrail breaches | Zero | Every pre-trade check logged; any hard-block trigger counts as a breach and forces a design review. |
| Adaptation discipline *(new)* | 100% of strategy changes evidence-linked; ≤ 2 per review; zero oscillation flags | From the config change log and Strategy Memos; post-change checkpoints documented in the digests. |

**Paper-fill realism.** After-close orders fill at next session open; all fills carry a fixed slippage haircut (parameter in §8), logged per trade. Reported returns are net of modeled slippage. Paper otherwise flatters reality; this is the minimum honesty bar for graduation to mean anything.

---

## 6. Risks & mitigations

| Risk | Mitigation |
|---|---|
| 8 weeks is too short — returns are mostly luck | Process metrics are the primary gate; returns reported with CIs and multiple benchmarks; extend to 12 weeks if borderline. |
| LLM hallucinates data or narratives | Brains may only act on gate-validated data; neighborhood analysis forces cross-checks; rubric penalizes ungrounded claims; unvalidated tickers hard-blocked. |
| 3×-daily cadence becomes churn | Intraday runs defensive-only; turnover cap on risk-adding trades. |
| De-risk mode whipsaws in/out of cash | Hysteresis (entry ≠ exit thresholds) + minimum 3-day dwell, fixed in code. |
| **Adaptation loop overfits to noise** | Minimum-evidence thresholds, CI-aware decisions, change budget, per-parameter cooldowns. |
| **Strategy thrash / oscillation** | Cooldowns; oscillation flag forces human design review; bounded moves only. |
| **Reward hacking of own metrics** (e.g., steering toward only easy forecasts to juice hit-rate) | Coverage rule (every holding forecast at every horizon); calibration scored alongside hit-rate; benchmark-relative scoring; human audit of grades. |
| **Correlated blind spots** (both brains share model heritage) | Separate contexts and prompts, optionally different models; deterministic code checks remain the backstop; weekly human audit. |
| Self-grading leniency | Grader isolated from Investment Brain context; human audit with disagreement tracking and forced grader revision. |
| Paper results flatter live trading | Next-open fills + slippage haircut; commission-free broker + fractional shares assumed and re-verified at graduation (§9). |
| Cost creep from 3 runs + weekly review | Small model intraday, caching, token budgets, kill-switch per §4. |
| Small NAV ($5k): fees and lot sizes distort returns | Commission-free broker + fractional shares required; $100 minimum on opens/adds. |
| Multi-market universe: FX noise, staggered hours, patchier EU data | NAV and reporting in USD; FX unhedged but its impact isolated in attribution; midday run doubles as the EU-close review; EU tickers pass the same data-validation gate. |

---

## 7. Out of scope (v1)

Intraday/high-frequency trading; options, leverage, shorting; crypto; emerging-market listings; FX hedging; real-money execution until the §9 gates pass. Neither brain may modify its own or the other's prompts or code — prompt and code changes are human-made in v1; the Strategy Brain touches only the bounded tunables in §4. Personal project with personal capital only — not investment advice.

---

## 8. Open items to close during the brain phase

1. **Regime indicator panel:** which coded signals (e.g., index vs long-term moving average, market breadth, volatility level/term structure, credit spreads), with thresholds and hysteresis values. The LLM interprets the panel; code owns the regime state.
2. **Per-name benchmark map** for relative forecast scoring (sector-ETF assignments; ACWI for broad ETFs).
3. **Slippage haircut parameter(s)** for US and EU fills.
4. **Reasoning rubric weights** and the grading prompt for the Evaluation Brain.
5. **Strategy Memo schema** and the final config-bounds file (locking the §4 table values).

---

## 9. Graduation to real money

Real capital only if, over the full 8 weeks: all mandatory targets met (benchmark-relative hit-rate, calibration, reasoning, drawdown floor, cost, adaptation discipline) **and** zero guardrail breaches. First real allocation capped at $1,000 with identical guardrails and autonomy: the bot trades on its own; oversight is the weekly digest plus the code-level guardrails and kill-switch. Broker-level realities (order types, fractional shares, actual fees) re-verified in a live sandbox before the first real order. A second independent price source, scoped to gating the stop-loss sweep, must be integrated and verified before the first real order — closing the paper phase's accepted single-vendor risk (§2; changelog 2026-07-23). Scale toward the full $5,000 only after a clean first month — no breaches, no unexplained losses vs benchmark, no oscillation flags.
