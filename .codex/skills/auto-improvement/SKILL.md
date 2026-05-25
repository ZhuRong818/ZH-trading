---
name: auto-improvement
description: "Use when iterating ZH-trading strategy research with the report-only orchestrator: read previous research summaries, generate experiment variants, run research.orchestrator/signal_eval/backtest.replay, compare gates and rankings, and recommend promote_candidate/keep_testing/reject without auto-editing live config or strategy code."
---

# Auto Improvement

## Purpose

Use this skill as a report-only research agent for ZH-trading. It helps close the loop after `strategy-creator` creates or wires a candidate strategy by running controlled experiment variants, comparing metrics, and proposing the next research step.

It is not an auto-deployment skill. Do not directly modify `config/settings.py`, live strategy modules, or strategy-library code as part of this workflow unless the user explicitly asks for implementation and the relevant strategy skill or `strategy-creator` is used.

## Safe Workflow

1. Read the experiment spec, usually `research/experiments.yaml`. If previous runs exist, also read the newest `reports/research_runs/*/summary.json`. On first run, skip the summary and proceed directly to variant generation.
2. Identify the best, worst, and inconclusive experiments from `gate_status`, `failed_gates`, `rank`, `recommendation`, and per-mode metrics.
3. Generate a small next batch of experiment variants by editing or creating a research spec only.
4. Run:
   ```bash
   python -m research.orchestrator --spec research/experiments.yaml
   ```
   For quick iteration, pass `--max-records N`.
5. Normalize the results through the generated `summary.json`, then rank and explain `promote_candidate`, `keep_testing`, or `reject`.
6. Stop when the target gates pass, the data is insufficient, or the next useful change requires strategy-code work.

## Allowed Outputs

- Updated research spec files such as `research/experiments.yaml` or `research/experiments_<topic>.yaml`.
- Research reports under `reports/research_runs/<run_id>/`.
- Suggested config overrides in report text or Markdown.
- A concise recommendation for whether to promote, keep testing, reject, or hand back to `strategy-creator`.

## Guardrails

- Do not auto-edit `config/settings.py`.
- Do not auto-edit `strategies/v2/*.py`, `main.py`, or `backtest/replay.py` during this skill's research loop.
- Do not claim live readiness from replay-only or signal-only evidence.
- Do not use a tiny `--max-records` smoke result for promotion; smoke runs only validate mechanics.
- Keep each iteration small: prefer 3-8 variants around one hypothesis over broad parameter sweeps.
- Preserve report-only auditability: every recommendation should point to the summary artifact and failed or passed gates.

## Variant Design

Choose variants from the observed failure mode:

- `min_trades` or `no_replay_trades`: loosen entry thresholds, widen timing windows, reduce confirmation counts, or test more assets/data.
- Negative PnL with enough trades: increase edge thresholds, lower max price, reduce notional, or add spread/depth gates if exposed by the strategy.
- High win rate but weak PnL: test larger edge/notional caps only in replay, and keep risk gates conservative.
- Poor signal accuracy/Brier/log loss: compare against `baseline_market_mid`, reduce confidence, or test a simpler probability transform.
- Asset instability: split variants by `btc`, `eth`, `sol`, `xrp` before recommending a portfolio-wide setting.

## Hand-Off To Strategy Creator

Use `strategy-creator` when the next step requires code:

- adding a new strategy under `strategies/v2/`;
- wiring a strategy into `main.py`;
- adding replay support or CLI flags in `backtest/replay.py`;
- changing signal construction, fill handling, snapshots, or strategy defaults.

After `strategy-creator` finishes, return to this skill to evaluate the candidate through `research.orchestrator`.
