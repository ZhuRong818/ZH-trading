#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[3]
RUN_MVP = ROOT / "scripts" / "run_mvp.py"

LLM_SYSTEM_PROMPT = """You are an X KOL discovery scorer for Polymarket-style prediction markets.
Return STRICT JSON only.

Reject accounts that are mainly referral spam, trader/PnL flex, automated summaries,
copied news, engagement bait, no original view, or no clear event outcome.

Score from 0-100:
- topic_fit
- originality
- reasoning_quality
- signal_density
- engagement_quality
- market_relevance
- noise_penalty

Return JSON:
{
  "handle": "",
  "verdict": "include|watchlist|reject",
  "domain": "",
  "topic_fit": 0,
  "originality": 0,
  "reasoning_quality": 0,
  "signal_density": 0,
  "engagement_quality": 0,
  "market_relevance": 0,
  "noise_penalty": 0,
  "total_score": 0,
  "reason": "",
  "best_evidence_tweet_url": "",
  "risk_flags": []
}
"""


def load_run_mvp():
    spec = importlib.util.spec_from_file_location("prediction_market_run_mvp", RUN_MVP)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {RUN_MVP}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def normalize_domain(value: str) -> str:
    v = (value or "").strip().lower()
    aliases = {
        "prediction_market": "any",
        "general-polymarket": "any",
        "politics": "Politics / election",
        "politics/election": "Politics / election",
        "election": "Politics / election",
        "crypto": "Crypto price",
        "crypto price": "Crypto price",
        "crypto regulation": "Crypto regulation",
        "sports": "Sports",
        "geopolitics": "Geopolitics / war / sanctions",
        "macro": "Macro / Fed / CPI / rates",
        "health": "Health / pandemic",
        "pandemic": "Health / pandemic",
        "health/pandemic": "Health / pandemic",
        "public health": "Health / pandemic",
        "company": "Company earnings/performance",
        "company/events": "Company earnings/performance",
        "other": "Other",
        "any": "any",
    }
    return aliases.get(v, value)


def empty_stats() -> Dict[str, Any]:
    return {
        "tweets_seen": 0,
        "signals": 0,
        "score": 0,
        "engagement": 0,
        "deadline_signals": 0,
        "falsifiable_signals": 0,
        "noise_rejected": 0,
        "weak_rejected": 0,
        "domains": Counter(),
        "predictive_examples": [],
        "rejected_examples": [],
    }


def iter_jsonl(paths: List[str], max_rows: int):
    rows = 0
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                yield json.loads(line)
                rows += 1
                if max_rows and rows >= max_rows:
                    return


def compact_signal(signal: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "tweet_id": signal.get("tweet_id"),
        "tweet_url": signal.get("tweet_url"),
        "created_at": signal.get("created_at"),
        "domain": signal.get("domain"),
        "direction": signal.get("direction"),
        "deadline": signal.get("deadline"),
        "signal_score": signal.get("signal_score"),
        "event": signal.get("event"),
    }


def compact_reject(row: Dict[str, Any], reason: str, mod: Any) -> Dict[str, Any]:
    tweet = row.get("tweet") or {}
    text = mod.text_of(row)
    return {
        "tweet_id": tweet.get("id"),
        "tweet_url": tweet.get("url") or tweet.get("twitterUrl"),
        "created_at": row.get("created_at") or tweet.get("createdAt"),
        "reason": reason,
        "text": text[:300],
    }


def build_packets(
    paths: List[str],
    requested_domain: str,
    max_rows: int,
    examples_per_kol: int,
    tweet_judge: Any = None,
) -> Dict[str, Dict[str, Any]]:
    mod = load_run_mvp()
    packets: Dict[str, Dict[str, Any]] = defaultdict(empty_stats)
    domain_filter = normalize_domain(requested_domain)

    for row in iter_jsonl(paths, max_rows):
        handle = str(row.get("kol_username") or "")
        if not handle:
            continue
        stats = packets[handle]
        stats["tweets_seen"] += 1

        text = mod.text_of(row)
        tweet_type = str(row.get("tweet_type") or "")
        ok, reason, score = mod.is_predictive_candidate(text, tweet_type)
        if not ok:
            if reason not in ("weak_prediction_language", "no_forward_signal"):
                stats["noise_rejected"] += 1
                if len(stats["rejected_examples"]) < examples_per_kol:
                    stats["rejected_examples"].append(compact_reject(row, reason, mod))
            else:
                stats["weak_rejected"] += 1
            continue

        signal = mod.build_signal(row, score=score)
        if not signal:
            continue

        keep, judge_reason = mod.apply_tweet_judgment(signal, tweet_judge)
        if not keep:
            stats["weak_rejected"] += 1
            if len(stats["rejected_examples"]) < examples_per_kol:
                rejected = compact_reject(row, judge_reason, mod)
                rejected["llm_tweet_judge"] = signal.get("llm_tweet_judge")
                stats["rejected_examples"].append(rejected)
            continue

        signal_domain = str(signal.get("domain") or "Other")
        stats["domains"][signal_domain] += 1
        if domain_filter != "any" and signal_domain != domain_filter:
            continue

        stats["signals"] += 1
        stats["score"] += int(signal.get("signal_score") or 0)
        stats["engagement"] += int(signal.get("engagement") or 0)
        if signal.get("deadline"):
            stats["deadline_signals"] += 1
        if signal.get("is_falsifiable"):
            stats["falsifiable_signals"] += 1
        if len(stats["predictive_examples"]) < examples_per_kol:
            stats["predictive_examples"].append(compact_signal(signal))

    return packets


def packet_to_row(handle: str, data: Dict[str, Any], min_signals: int) -> Optional[Dict[str, Any]]:
    signals = int(data["signals"])
    if signals < min_signals:
        return None

    tweets_seen = int(data["tweets_seen"])
    signal_density = signals / max(1, tweets_seen)
    falsifiability_rate = data["falsifiable_signals"] / max(1, signals)
    deadline_rate = data["deadline_signals"] / max(1, signals)
    domain_focus = data["domains"].most_common(1)[0][1] / max(1, sum(data["domains"].values())) if data["domains"] else 0.0
    avg_signal_score = data["score"] / max(1, signals)
    avg_engagement = data["engagement"] / max(1, signals)
    spam_rate = data["noise_rejected"] / max(1, tweets_seen)
    top_domain = data["domains"].most_common(1)[0][0] if data["domains"] else "Other"

    final_score = (
        data["score"]
        + signal_density * 120
        + falsifiability_rate * 30
        + deadline_rate * 15
        + domain_focus * 10
        + min(20, avg_engagement / 20)
        - spam_rate * 30
    )

    if final_score >= 150 and signals >= 20 and signal_density >= 0.02:
        verdict = "include"
    elif final_score >= 40:
        verdict = "watchlist"
    else:
        verdict = "reject"

    best = data["predictive_examples"][0] if data["predictive_examples"] else {}
    return {
        "handle": handle,
        "verdict": verdict,
        "final_score": round(final_score, 3),
        "top_domain": top_domain,
        "tweets_seen": tweets_seen,
        "signals": signals,
        "signal_density": round(signal_density, 5),
        "falsifiability_rate": round(falsifiability_rate, 3),
        "deadline_rate": round(deadline_rate, 3),
        "domain_focus": round(domain_focus, 3),
        "avg_signal_score": round(avg_signal_score, 3),
        "avg_engagement": round(avg_engagement, 3),
        "spam_rate": round(spam_rate, 3),
        "best_evidence_tweet_url": best.get("tweet_url"),
        "best_example": best.get("event"),
        "ranker": "heuristic",
        "llm_reason": "",
        "risk_flags": "",
    }


def packet_for_llm(handle: str, data: Dict[str, Any], heuristic_row: Dict[str, Any], requested_domain: str) -> Dict[str, Any]:
    signals = max(1, int(data["signals"]))
    tweets_seen = max(1, int(data["tweets_seen"]))
    return {
        "handle": handle,
        "requested_domain": requested_domain,
        "heuristic": heuristic_row,
        "metrics": {
            "tweets_seen": data["tweets_seen"],
            "signals": data["signals"],
            "signal_density": round(data["signals"] / tweets_seen, 5),
            "spam_rate": round(data["noise_rejected"] / tweets_seen, 3),
            "weak_reject_rate": round(data["weak_rejected"] / tweets_seen, 3),
            "falsifiability_rate": round(data["falsifiable_signals"] / signals, 3),
            "deadline_rate": round(data["deadline_signals"] / signals, 3),
            "domains": dict(data["domains"]),
            "avg_engagement": round(data["engagement"] / signals, 3),
        },
        "sample_predictive_tweets": data["predictive_examples"][:8],
        "sample_rejected_tweets": data["rejected_examples"][:5],
    }


def provider_config(provider: str, model: str, api_key: Optional[str], base_url: Optional[str]) -> Dict[str, str]:
    p = provider.lower()
    if p == "deepseek":
        return {
            "api_key": api_key or os.getenv("DEEPSEEK_API_KEY", ""),
            "base_url": (base_url or "https://api.deepseek.com").rstrip("/"),
            "model": model or "deepseek-v4-flash",
        }
    if p == "qwen":
        return {
            "api_key": api_key or os.getenv("DASHSCOPE_API_KEY", ""),
            "base_url": (base_url or "https://dashscope-intl.aliyuncs.com/compatible-mode/v1").rstrip("/"),
            "model": model or "qwen3.5-flash",
        }
    if p == "openai":
        return {
            "api_key": api_key or os.getenv("OPENAI_API_KEY", ""),
            "base_url": (base_url or "https://api.openai.com/v1").rstrip("/"),
            "model": model or "gpt-4.1-mini",
        }
    raise ValueError(f"Unsupported llm_provider: {provider}")


def extract_json_object(text: str) -> Dict[str, Any]:
    if not text or not text.strip():
        raise ValueError("empty LLM response content")
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def chat_completion_json(
    provider: str,
    model: str,
    api_key: Optional[str],
    base_url: Optional[str],
    packet: Dict[str, Any],
    timeout: int,
    retries: int,
) -> Dict[str, Any]:
    cfg = provider_config(provider, model, api_key, base_url)
    if not cfg["api_key"]:
        env = "DEEPSEEK_API_KEY" if provider == "deepseek" else "DASHSCOPE_API_KEY" if provider == "qwen" else "OPENAI_API_KEY"
        raise RuntimeError(f"Missing LLM API key. Set {env} or pass --llm_api_key.")

    body: Dict[str, Any] = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": LLM_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Return JSON.\n\nCandidate packet:\n" + json.dumps(packet, ensure_ascii=False),
            },
        ],
        "response_format": {"type": "json_object"},
    }
    if provider.lower() == "deepseek":
        body["max_tokens"] = 700
        body["extra_body"] = {"thinking": {"type": "disabled"}}
    elif provider.lower() == "qwen":
        body["extra_body"] = {"enable_thinking": False}
    else:
        body["max_tokens"] = 700

    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        cfg["base_url"] + "/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"].get("content") or ""
            return extract_json_object(content)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in (429, 500, 502, 503, 504) or attempt >= retries:
                body_text = exc.read().decode("utf-8", errors="replace")[:500]
                raise RuntimeError(f"LLM HTTP {exc.code}: {body_text}") from exc
            time.sleep(2 ** attempt)
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                raise RuntimeError(f"LLM returned invalid JSON/content: {exc}") from exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"LLM request failed: {last_error}")


def llm_total_score(result: Dict[str, Any], fallback: float) -> float:
    if isinstance(result.get("total_score"), (int, float)):
        return float(result["total_score"])
    fields = [
        "topic_fit",
        "originality",
        "reasoning_quality",
        "signal_density",
        "engagement_quality",
        "market_relevance",
    ]
    vals = [float(result.get(k) or 0) for k in fields]
    if not any(vals):
        return fallback
    score = (
        0.25 * vals[0]
        + 0.18 * vals[1]
        + 0.18 * vals[2]
        + 0.14 * vals[3]
        + 0.10 * vals[4]
        + 0.15 * vals[5]
        - 0.20 * float(result.get("noise_penalty") or 0)
    )
    return max(0.0, min(100.0, score))


def apply_llm_rerank(
    rows: List[Dict[str, Any]],
    packets: Dict[str, Dict[str, Any]],
    domain: str,
    provider: str,
    model: str,
    api_key: Optional[str],
    base_url: Optional[str],
    candidate_limit: int,
    timeout: int,
    retries: int,
) -> List[Dict[str, Any]]:
    rerank_rows = rows[:candidate_limit]
    untouched = rows[candidate_limit:]
    scored = []
    for row in rerank_rows:
        handle = row["handle"]
        packet = packet_for_llm(handle, packets[handle], row, domain)
        try:
            result = chat_completion_json(provider, model, api_key, base_url, packet, timeout, retries)
        except Exception as exc:
            merged = dict(row)
            merged["ranker"] = f"llm_failed:{provider}:{model}"
            merged["llm_reason"] = f"LLM scoring failed for @{handle}; kept heuristic score. Error: {exc}"
            merged["risk_flags"] = "llm_scoring_failed"
            print(f"[WARN] @{handle}: LLM scoring failed, keeping heuristic score: {exc}", flush=True)
            scored.append(merged)
            continue
        llm_score = llm_total_score(result, float(row["final_score"]))
        merged = dict(row)
        merged["ranker"] = f"llm:{provider}:{model}"
        merged["final_score"] = round(llm_score, 3)
        merged["verdict"] = result.get("verdict") or row["verdict"]
        merged["top_domain"] = result.get("domain") or row["top_domain"]
        merged["llm_reason"] = result.get("reason") or result.get("summary") or ""
        flags = result.get("risk_flags") or []
        merged["risk_flags"] = ";".join(map(str, flags)) if isinstance(flags, list) else str(flags)
        if result.get("best_evidence_tweet_url"):
            merged["best_evidence_tweet_url"] = result["best_evidence_tweet_url"]
        scored.append(merged)

    for row in untouched:
        copied = dict(row)
        copied["ranker"] = "heuristic_not_llm_scored"
        scored.append(copied)
    scored.sort(key=lambda r: (r["final_score"], r["signals"]), reverse=True)
    return scored


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--input", action="append", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--packets_out")
    parser.add_argument("--max_rows", type=int, default=0)
    parser.add_argument("--min_signals", type=int, default=2)
    parser.add_argument("--examples_per_kol", type=int, default=8)
    parser.add_argument("--ranker", choices=["heuristic", "llm"], default="heuristic")
    parser.add_argument("--llm_provider", choices=["deepseek", "qwen", "openai"], default="deepseek")
    parser.add_argument("--llm_model", default="")
    parser.add_argument("--llm_api_key")
    parser.add_argument("--llm_base_url")
    parser.add_argument("--llm_candidate_limit", type=int, default=25)
    parser.add_argument("--llm_timeout", type=int, default=90)
    parser.add_argument("--llm_retries", type=int, default=3)
    parser.add_argument("--tweet_judge", choices=["heuristic", "llm"], default="heuristic")
    parser.add_argument("--tweet_judge_min_score", type=int, default=6)
    parser.add_argument("--tweet_judge_provider", choices=["deepseek", "qwen", "openai"], default="")
    parser.add_argument("--tweet_judge_model", default="")
    parser.add_argument("--tweet_judge_api_key")
    parser.add_argument("--tweet_judge_base_url")
    parser.add_argument("--tweet_judge_timeout", type=int, default=60)
    parser.add_argument("--tweet_judge_retries", type=int, default=2)
    parser.add_argument("--tweet_judge_max_calls", type=int, default=0)
    args = parser.parse_args()

    mod = load_run_mvp()
    tweet_judge_provider = args.tweet_judge_provider or args.llm_provider
    tweet_judge_model = args.tweet_judge_model or (
        "deepseek-chat" if tweet_judge_provider == "deepseek" else "qwen3.5-flash" if tweet_judge_provider == "qwen" else ""
    )
    tweet_judge = mod.TweetJudge(
        enabled=args.tweet_judge == "llm",
        domain=args.domain,
        min_score=args.tweet_judge_min_score,
        provider=tweet_judge_provider,
        model=tweet_judge_model,
        api_key=args.tweet_judge_api_key or args.llm_api_key,
        base_url=args.tweet_judge_base_url or args.llm_base_url,
        timeout=args.tweet_judge_timeout,
        retries=args.tweet_judge_retries,
        max_calls=args.tweet_judge_max_calls,
    )

    packets = build_packets(args.input, args.domain, args.max_rows, args.examples_per_kol, tweet_judge)
    rows = []
    for handle, packet in packets.items():
        row = packet_to_row(handle, packet, args.min_signals)
        if row:
            rows.append(row)
    rows.sort(key=lambda r: (r["final_score"], r["signals"]), reverse=True)
    prelimit = max(args.count, args.llm_candidate_limit if args.ranker == "llm" else args.count)
    rows = rows[:prelimit]

    if args.ranker == "llm":
        model = args.llm_model or ("deepseek-v4-flash" if args.llm_provider == "deepseek" else "qwen3.5-flash" if args.llm_provider == "qwen" else "")
        try:
            rows = apply_llm_rerank(
                rows,
                packets,
                args.domain,
                args.llm_provider,
                model,
                args.llm_api_key,
                args.llm_base_url,
                args.llm_candidate_limit,
                args.llm_timeout,
                args.llm_retries,
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc

    rows = rows[: args.count]

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "handle", "verdict", "final_score", "top_domain", "tweets_seen", "signals",
            "signal_density", "falsifiability_rate", "deadline_rate", "domain_focus",
            "avg_signal_score", "avg_engagement", "spam_rate", "best_evidence_tweet_url",
            "best_example", "ranker", "llm_reason", "risk_flags",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    if args.packets_out:
        os.makedirs(os.path.dirname(args.packets_out), exist_ok=True)
        selected = {r["handle"] for r in rows}
        with open(args.packets_out, "w", encoding="utf-8") as f:
            for handle in selected:
                packet = packets[handle]
                out = {
                    "handle": handle,
                    "tweets_seen": packet["tweets_seen"],
                    "signals": packet["signals"],
                    "domains": dict(packet["domains"]),
                    "sample_predictive_tweets": packet["predictive_examples"],
                    "sample_rejected_tweets": packet["rejected_examples"],
                }
                f.write(json.dumps(out, ensure_ascii=False) + "\n")

    print(f"rows={len(rows)}")
    print(f"out={args.out}")
    if args.packets_out:
        print(f"packets_out={args.packets_out}")
    if tweet_judge.enabled:
        print(
            "tweet_judge="
            f"calls={tweet_judge.calls} accepted={tweet_judge.accepted} "
            f"rejected={tweet_judge.rejected} skipped={tweet_judge.skipped} failed={tweet_judge.failed}"
        )


if __name__ == "__main__":
    main()
