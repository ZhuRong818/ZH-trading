"""Optional LLM planner/explainer for strategy-generation research.

The LLM is intentionally outside the quantitative loop. It may propose
candidate JSON or explain deterministic grading output, but schema validation,
backtesting, and grading stay in code.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research.strategy_templates import generate_candidates


@dataclass
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    timeout: int = 60
    supports_json_mode: bool = True


class OpenAICompatClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg

    def complete_json(self, system: str, user: str) -> Any:
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self.cfg.supports_json_mode:
            payload["response_format"] = {"type": "json_object"}
        request = urllib.request.Request(
            self.cfg.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.cfg.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.cfg.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)


def deterministic_plan(schema_summary: dict[str, Any]) -> list[dict[str, Any]]:
    fields = set(schema_summary.get("canonical_fields") or schema_summary.get("fields") or [])
    fields.update(schema_summary.get("canonical_matches", {}).keys())
    return generate_candidates(fields)


def llm_plan(client: OpenAICompatClient, user_objective: str, schema_summary: dict[str, Any]) -> Any:
    system = (
        "You are a prediction-market strategy planner. Return JSON only. "
        "Generate research candidates, not live trading instructions."
    )
    user = {
        "user_objective": user_objective,
        "schema_summary": schema_summary,
        "allowed_families": [
            "momentum",
            "mean_reversion",
            "volume_shock",
            "early_attention",
            "open_interest_growth",
            "volatility_regime",
            "closing_time_behavior",
        ],
        "constraints": [
            "Use only confirmed schema fields.",
            "If a required field is missing, return disabled_reason.",
            "Do not generate live buy/sell instructions.",
        ],
    }
    return client.complete_json(system, json.dumps(user, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate strategy candidates from schema, optionally using an LLM")
    parser.add_argument("--schema", required=True, help="Schema probe JSON")
    parser.add_argument("--objective", default="Find backtestable prediction-market research strategies")
    parser.add_argument("--provider", choices=["deterministic", "openai_compat"], default="deterministic")
    parser.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", ""))
    parser.add_argument("--api-key", default=os.environ.get("LLM_API_KEY", ""))
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", ""))
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
    if args.provider == "deterministic":
        result = deterministic_plan(schema)
    else:
        if not args.base_url or not args.api_key or not args.model:
            raise SystemExit("openai_compat provider requires --base-url, --api-key, and --model")
        result = llm_plan(OpenAICompatClient(LLMConfig(args.base_url, args.api_key, args.model)), args.objective, schema)
    output = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
