#!/usr/bin/env python3
"""Aggregate logs/journal.jsonl into a per-day operational digest at
research/reports/daily-digest.md: run counts, the screening -> proposal ->
approval funnel, guardrail rejections, sleeve trades, stop-loss events,
breaker states, equity, and the token economy (§5) with a 7-day cost trend —
so budget drift is visible the day it starts, not after a week of silent
run_history.log lines.

Run mode: python scripts/journal_report.py
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Repo root on sys.path so `from bot.config import ...` resolves regardless
# of cwd — running `python scripts/journal_report.py` only puts scripts/
# itself on sys.path, not its parent.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import CACHE_READ_MULTIPLIER, CACHE_WRITE_MULTIPLIER, PRICING_PER_MTOK  # noqa: E402

JOURNAL_PATHS = [
    Path("logs/journal-archive-100k.jsonl"),
    Path("logs/journal.jsonl"),
]
OUTPUT_PATH = Path("research/reports/daily-digest.md")

SECTION_ORDER = ["global", "stat_lines", "headlines", "macro", "forecast_accuracy", "instructions", "system_prompt", "output"]


def load_records():
    records = []
    for path in JOURNAL_PATHS:
        if not path.exists():
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    records.sort(key=lambda r: r["timestamp"])
    return records


def day_of(timestamp):
    return datetime.fromisoformat(timestamp).astimezone(timezone.utc).date()


def estimate_cost_usd(model, usage):
    pricing = PRICING_PER_MTOK.get(model)
    if pricing is None or not usage:
        return 0.0
    return (
        usage.get("input_tokens", 0) * pricing["input"]
        + usage.get("output_tokens", 0) * pricing["output"]
        + usage.get("cache_creation_input_tokens", 0) * pricing["input"] * CACHE_WRITE_MULTIPLIER
        + usage.get("cache_read_input_tokens", 0) * pricing["input"] * CACHE_READ_MULTIPLIER
    ) / 1_000_000


class DayStats:
    def __init__(self):
        self.run_labels = []  # e.g. "16:53 LIGHT"
        self.legacy_run_count = 0
        self.candidates_screened = 0
        self.proposals = 0  # actionable (non-HOLD) proposals from the brain
        self.approved = 0  # actionable proposals risk.py approved
        self.rejected_guardrail = defaultdict(int)
        self.execution_rejections = []  # (symbol, action, reason)
        self.trades_by_sleeve = defaultdict(lambda: {"count": 0, "notional": 0.0})
        self.stop_events = []  # (symbol, sleeve, stop_status, confirmed_pct)
        self.fill_results = []  # reconcile_pending_fills() outcomes
        self.breaker_states = set()
        self.regimes = set()
        self.equity_points = []  # (timestamp, equity)
        self.token_runs = []  # dicts: run_mode, model, usage, section_estimates, cost
        self.errors = []  # (timestamp, error)

    @property
    def total_cost(self):
        return sum(r["cost"] for r in self.token_runs)


def build_daily_stats(records):
    days = defaultdict(DayStats)

    for record in records:
        ts = record.get("timestamp")
        if not ts:
            continue
        stats = days[day_of(ts)]

        if record.get("status") == "ERROR":
            stats.errors.append((ts, record.get("error", "unknown error")))
            continue

        record_type = record.get("type")

        if record_type == "token_usage":
            usage = record.get("usage") or {}
            model = record.get("model")
            stats.token_runs.append(
                {
                    "run_mode": record.get("run_mode"),
                    "model": model,
                    "usage": usage,
                    "section_estimates": record.get("section_estimates") or {},
                    "cost": estimate_cost_usd(model, usage),
                }
            )
            continue

        if record_type in ("light_run", "full_run"):
            run_mode = record_type.split("_")[0].upper()
            stats.run_labels.append(f"{ts[11:16]} {run_mode}")
            stats.regimes.add(record.get("regime"))
            stats.breaker_states.add(record.get("breaker_status"))
            if "equity" in record:
                stats.equity_points.append((ts, record["equity"]))

            candidates = record.get("candidates") or []
            stats.candidates_screened += len(candidates)
            for c in candidates:
                proposal = c.get("proposal") or {}
                verdict = c.get("verdict") or {}
                if proposal.get("action") == "HOLD":
                    continue
                stats.proposals += 1
                if verdict.get("status") == "APPROVED":
                    stats.approved += 1
                elif verdict.get("status") == "REJECTED":
                    stats.rejected_guardrail[verdict.get("guardrail") or "UNKNOWN"] += 1

                execution = c.get("execution")
                if execution is None:
                    continue
                sleeve = execution.get("sleeve", "UNKNOWN")
                if execution.get("status") == "SUBMITTED":
                    order_value = execution.get("order_value") or execution.get("notional") or execution.get("usd_amount") or 0
                    stats.trades_by_sleeve[sleeve]["count"] += 1
                    stats.trades_by_sleeve[sleeve]["notional"] += order_value
                elif execution.get("status") == "REJECTED":
                    stats.execution_rejections.append((execution.get("symbol"), execution.get("action"), execution.get("reason")))

            for sweep in record.get("sweep_results") or []:
                stats.stop_events.append(
                    (
                        sweep.get("symbol"),
                        sweep.get("sleeve"),
                        sweep.get("stop_status"),
                        sweep.get("confirmed_unrealized_pct"),
                    )
                )

            stats.fill_results.extend(record.get("fill_results") or [])
            continue

        # Legacy pre-2026-07-24 v0 records: no "type" key, no candidates/
        # proposals/guardrails/tokens — only regime/breaker/equity/actions.
        stats.legacy_run_count += 1
        stats.regimes.add(record.get("regime"))
        if "breaker_status" in record:
            stats.breaker_states.add(record["breaker_status"])
        if "equity" in record:
            stats.equity_points.append((ts, record["equity"]))
        for action in record.get("actions") or []:
            if action.get("status") == "SUBMITTED":
                sleeve = action.get("sleeve", "UNKNOWN")
                order_value = action.get("usd_amount") or 0
                stats.trades_by_sleeve[sleeve]["count"] += 1
                stats.trades_by_sleeve[sleeve]["notional"] += order_value

    return days


def _fmt_usd(value):
    return f"${value:,.2f}"


def _fmt_pct(numerator, denominator):
    if not denominator:
        return "n/a"
    return f"{numerator / denominator * 100:.0f}%"


def render_trend_table(sorted_days, days):
    recent = sorted_days[-7:]
    lines = [
        "## Token economy — 7-day trend",
        "",
        "| Day | Runs | Input tok | Output tok | Cache read | Cache write | Est. cost |",
        "|---|---|---|---|---|---|---|",
    ]
    for day in recent:
        stats = days[day]
        total_in = sum(r["usage"].get("input_tokens", 0) for r in stats.token_runs)
        total_out = sum(r["usage"].get("output_tokens", 0) for r in stats.token_runs)
        total_read = sum(r["usage"].get("cache_read_input_tokens", 0) for r in stats.token_runs)
        total_write = sum(r["usage"].get("cache_creation_input_tokens", 0) for r in stats.token_runs)
        run_count = len(stats.run_labels) + stats.legacy_run_count
        lines.append(
            f"| {day} | {run_count} | {total_in:,} | {total_out:,} | {total_read:,} | "
            f"{total_write:,} | {_fmt_usd(stats.total_cost)} |"
        )
    lines.append("")
    return lines


def render_day_section(day, stats):
    lines = [f"## {day}", ""]

    run_count = len(stats.run_labels) + stats.legacy_run_count
    run_desc = ", ".join(stats.run_labels) if stats.run_labels else None
    if stats.legacy_run_count:
        legacy_desc = f"{stats.legacy_run_count} pre-pipeline (v0) run(s)"
        run_desc = f"{run_desc}; {legacy_desc}" if run_desc else legacy_desc
    lines.append(f"**Runs ({run_count}):** {run_desc or '(no runs logged)'}")

    regimes = ", ".join(sorted(r for r in stats.regimes if r)) or "n/a"
    breakers = ", ".join(sorted(b for b in stats.breaker_states if b)) or "n/a"
    lines.append(f"**Regime(s):** {regimes}  **Breaker state(s):** {breakers}")

    if stats.equity_points:
        first_ts, first_eq = stats.equity_points[0]
        last_ts, last_eq = stats.equity_points[-1]
        lines.append(f"**Equity:** open {_fmt_usd(first_eq)} -> close {_fmt_usd(last_eq)}")

    lines.append("")

    if stats.candidates_screened or stats.proposals:
        lines.append(
            f"**Screening funnel:** {stats.candidates_screened} candidates screened -> "
            f"{stats.proposals} actionable proposals -> {stats.approved} approved "
            f"({_fmt_pct(stats.approved, stats.proposals)} approval rate)"
        )

    if stats.rejected_guardrail:
        breakdown = ", ".join(f"{code}={count}" for code, count in sorted(stats.rejected_guardrail.items()))
        lines.append(f"**Rejections by guardrail:** {breakdown}")

    if stats.execution_rejections:
        lines.append(f"**Execution-level rejections ({len(stats.execution_rejections)}):**")
        for symbol, action, reason in stats.execution_rejections:
            lines.append(f"  - {action} {symbol}: {reason}")

    if stats.trades_by_sleeve:
        lines.append("**Trades submitted by sleeve:**")
        for sleeve, agg in sorted(stats.trades_by_sleeve.items()):
            lines.append(f"  - {sleeve}: {agg['count']} trade(s), {_fmt_usd(agg['notional'])} notional")

    if stats.fill_results:
        filled = [f for f in stats.fill_results if f.get("status") == "FILLED"]
        failed = [f for f in stats.fill_results if f.get("status") != "FILLED"]
        lines.append(f"**Fill reconciliation:** {len(filled)} filled, {len(failed)} never filled")
        for f in filled:
            lines.append(
                f"  - FILLED {f['action']} {f['symbol']}: decision={f['decision_price']:.2f} "
                f"fill={f['fill_price']:.2f} slippage={f['slippage_cost_pct']:.3f}% "
                f"(~{_fmt_usd(f['slippage_cost_usd'])})"
            )
        for f in failed:
            lines.append(f"  - {f['status']} {f['action']} {f['symbol']} — never filled")

    if stats.stop_events:
        lines.append(f"**Stop-loss events ({len(stats.stop_events)}):**")
        for symbol, sleeve, status, pct in stats.stop_events:
            pct_str = f"{pct:.2f}%" if pct is not None else "n/a"
            lines.append(f"  - {status} {symbol} ({sleeve}): confirmed {pct_str}")

    if stats.token_runs:
        lines.append("")
        lines.append("**Token economy:**")
        lines.append("")
        lines.append("| Run | Model | Input | Output | Cache read | Cache write | Cost | Sections (tok) |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for r in stats.token_runs:
            usage = r["usage"]
            sections = r["section_estimates"]
            section_str = ", ".join(
                f"{name}={sections[name]}" for name in SECTION_ORDER if name in sections
            )
            lines.append(
                f"| {r['run_mode']} | {r['model']} | {usage.get('input_tokens', 0):,} | "
                f"{usage.get('output_tokens', 0):,} | {usage.get('cache_read_input_tokens', 0):,} | "
                f"{usage.get('cache_creation_input_tokens', 0):,} | {_fmt_usd(r['cost'])} | {section_str} |"
            )
        lines.append(f"\n**Day total est. cost:** {_fmt_usd(stats.total_cost)}")

    if stats.errors:
        lines.append("")
        lines.append(f"**Errors ({len(stats.errors)}):**")
        for ts, error in stats.errors:
            lines.append(f"  - {ts[11:19]} UTC: {error}")

    lines.append("")
    return lines


def render_report(days):
    sorted_days = sorted(days.keys())
    lines = [
        "# Daily digest",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat()} by `scripts/journal_report.py` "
        f"from `logs/journal.jsonl` (+ archive). Regenerate after any run; not hand-edited.",
        "",
    ]
    lines.extend(render_trend_table(sorted_days, days))
    for day in reversed(sorted_days):
        lines.extend(render_day_section(day, days[day]))
    return "\n".join(lines).rstrip() + "\n"


def main():
    records = load_records()
    days = build_daily_stats(records)
    report = render_report(days)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(report)
    print(f"Wrote {OUTPUT_PATH} ({len(days)} day(s), {len(records)} record(s))")


if __name__ == "__main__":
    main()
