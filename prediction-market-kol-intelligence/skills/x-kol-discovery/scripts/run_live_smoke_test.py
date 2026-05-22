#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[4]
SEARCH_SCRIPT = ROOT / "prediction-market-kol-intelligence" / "skills" / "x-kol-discovery" / "scripts" / "search_discovery.py"
FIND_SCRIPT = ROOT / "prediction-market-kol-intelligence" / "skills" / "x-kol-discovery" / "scripts" / "find_kols.py"
FETCH_SCRIPT = ROOT / "prediction-market-kol-intelligence" / "scripts" / "fetch_complete_tweets.py"

MEDIA_TERMS = {
    "news", "newspaper", "daily", "times", "post", "gazette", "tribune", "journal",
    "herald", "observer", "standard", "chronicle", "bulletin", "report", "reports",
    "reuters", "ap", "afp", "bloomberg", "associated press", "guardian", "bbc",
    "cnn", "fox", "nbc", "cbs", "abc", "pbs", "nyt", "washington post", "wsj",
    "financial times", "finance", "business", "marketwatch", "tv", "radio",
    "magazine", "wire", "press", "media", "agency", "channel", "express",
    "digest", "punch", "headline", "headlines", "coverage",
}

QUALITY_ANCHOR_TERMS = {
    "analyst", "analysis", "research", "researcher", "scientist", "professor",
    "doctor", "dr", "phd", "md", "expert", "forecaster", "forecast", "model",
    "modeling", "tracker", "dashboard", "surveillance", "monitor", "briefing",
    "risk", "probability", "odds", "likely", "unlikely", "scenario", "thread",
    "lab", "institute", "university", "public health", "epidemiology",
}


def run(cmd: List[str]) -> None:
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def tokenize_query_text(text: str) -> set[str]:
    tokens = set()
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}", text.lower()):
        if token in {"and", "or", "the", "for", "with", "from", "that", "this", "who", "are"}:
            continue
        tokens.add(token)
    return tokens


def candidate_text(row: Dict[str, Any]) -> str:
    parts = [
        str(row.get("username") or ""),
        str(row.get("display_name") or ""),
        str(row.get("bio") or ""),
    ]
    for item in row.get("evidence") or []:
        if isinstance(item, dict):
            parts.append(str(item.get("query") or ""))
            parts.append(str(item.get("text") or ""))
    return "\n".join(parts).lower()


def media_penalty(row: Dict[str, Any]) -> float:
    profile = " ".join([
        str(row.get("username") or ""),
        str(row.get("display_name") or ""),
        str(row.get("bio") or ""),
    ]).lower()
    if not profile:
        return 0.0

    hits = 0
    for term in MEDIA_TERMS:
        if " " in term:
            matched = term in profile
        else:
            matched = re.search(rf"\b{re.escape(term)}\b", profile) is not None
        if matched:
            hits += 1

    if hits == 0:
        return 0.0
    if hits == 1:
        return 35.0
    return 60.0 + min(35.0, 12.0 * (hits - 2))


def candidate_quality_score(row: Dict[str, Any], domain_terms: set[str], allow_media_accounts: bool) -> float:
    followers = int(row.get("followers") or 0)
    matched_tweets = int(row.get("matched_tweets") or 0)
    distinct_query_hits = int(row.get("distinct_query_hits") or 0)
    text = candidate_text(row)

    evidence_items = row.get("evidence") or []
    evidence_text = " ".join(str(item.get("text") or "") for item in evidence_items if isinstance(item, dict)).lower()
    profile_text = " ".join([
        str(row.get("username") or ""),
        str(row.get("display_name") or ""),
        str(row.get("bio") or ""),
    ]).lower()

    domain_hits = sum(1 for term in domain_terms if len(term) >= 4 and term in text)
    evidence_domain_hits = sum(1 for term in domain_terms if len(term) >= 4 and term in evidence_text)
    profile_domain_hits = sum(1 for term in domain_terms if len(term) >= 4 and term in profile_text)
    quality_anchor_hits = sum(1 for term in QUALITY_ANCHOR_TERMS if term in text)

    one_off_penalty = 30.0 if matched_tweets <= 1 and distinct_query_hits <= 1 else 0.0
    media = 0.0 if allow_media_accounts else media_penalty(row)
    automated = 35.0 if row.get("is_automated") else 0.0

    score = (
        22.0 * distinct_query_hits
        + 9.0 * min(matched_tweets, 12)
        + 4.0 * min(domain_hits, 20)
        + 5.0 * min(evidence_domain_hits, 12)
        + 7.0 * min(profile_domain_hits, 8)
        + 5.0 * min(quality_anchor_hits, 8)
        + 6.0 * math.log10(max(1, followers) + 1)
        - one_off_penalty
        - media
        - automated
    )
    row["candidate_quality_score"] = round(score, 3)
    row["candidate_media_penalty"] = round(media, 3)
    return score


def read_candidates(
    path: Path,
    min_followers: int,
    limit: int,
    domain: str,
    allow_media_accounts: bool,
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    domain_terms = tokenize_query_text(domain)
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            username = row.get("username")
            if not username:
                continue
            if row.get("is_automated"):
                continue
            if int(row.get("followers") or 0) < min_followers:
                continue
            candidates.append(row)
    candidates.sort(
        key=lambda r: (
            candidate_quality_score(r, domain_terms, allow_media_accounts),
            int(r.get("matched_tweets") or 0),
            int(r.get("distinct_query_hits") or 0),
        ),
        reverse=True,
    )
    return candidates[:limit]


def write_handles(candidates: List[Dict[str, Any]], path: Path) -> None:
    seen = set()
    with path.open("w", encoding="utf-8") as f:
        for row in candidates:
            username = str(row.get("username") or "").strip().lstrip("@")
            key = username.lower()
            if username and key not in seen:
                seen.add(key)
                f.write(username + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live discovery: advanced_search -> extract authors -> fetch recent tweets -> rank KOLs."
    )
    parser.add_argument("--domain", default="prediction_market")
    parser.add_argument("--rank_domain", default="any", help="Domain passed to find_kols.py, e.g. 'Politics / election'.")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument(
        "--min_signals",
        type=int,
        default=1,
        help="Minimum domain-matching predictive signals before ranking a candidate.",
    )
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
    parser.add_argument("--candidate_limit", type=int, default=50)
    parser.add_argument("--min_followers", type=int, default=0)
    parser.add_argument(
        "--allow_media_accounts",
        action="store_true",
        help="Do not penalize generic newspaper/media accounts during pre-fetch candidate selection.",
    )
    parser.add_argument("--max_pages_per_query", type=int, default=1)
    parser.add_argument("--query_type_mix", choices=["latest", "top", "both"], default="both")
    parser.add_argument(
        "--query",
        action="append",
        default=[],
        help="Manual X advanced-search query. Repeatable. When set, search skips static/LLM planning.",
    )
    parser.add_argument("--query_planner", choices=["static", "llm"], default="static")
    parser.add_argument(
        "--discovery_mode",
        choices=["balanced", "topic_density", "predictive"],
        default="balanced",
        help="LLM query objective. topic_density improves candidate quality before fetching.",
    )
    parser.add_argument("--query_count", type=int, default=8)
    parser.add_argument("--query_planner_provider", choices=["deepseek", "qwen", "openai"], default="")
    parser.add_argument("--query_planner_model", default="")
    parser.add_argument("--query_planner_api_key")
    parser.add_argument("--query_planner_base_url")
    parser.add_argument("--query_planner_timeout", type=int, default=60)
    parser.add_argument("--query_planner_retries", type=int, default=3)
    parser.add_argument("--query_planner_json_mode", choices=["auto", "on", "off"], default="auto")
    parser.add_argument("--min_query_quality", type=int, default=8)
    parser.add_argument("--allow_query_fallback", action="store_true")
    parser.add_argument("--start", required=True, help="Fetch start date YYYY-MM-DD.")
    parser.add_argument("--until", required=True, help="Fetch until date YYYY-MM-DD.")
    parser.add_argument("--work_prefix", default="reports/kol_intelligence/live_x_kol_test")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--qps", type=float, default=6.0)
    parser.add_argument("--max_pages_per_slice", type=int, default=20)
    parser.add_argument(
        "--tweets_per_candidate",
        type=int,
        default=50,
        help="Max kept tweets to fetch per discovered candidate. 0 means no cap.",
    )
    parser.add_argument("--slice", choices=["day", "week", "month"], default="week")
    parser.add_argument("--strip_extended_entities", action="store_true")
    args = parser.parse_args()

    if not os.getenv("TWITTERAPI_IO_KEY"):
        raise SystemExit("Missing TWITTERAPI_IO_KEY. Run: export TWITTERAPI_IO_KEY='your_key'")

    prefix = Path(args.work_prefix)
    candidates_path = ROOT / f"{prefix}_candidates.jsonl"
    handles_path = ROOT / f"{prefix}_handles.txt"
    tweets_path = ROOT / f"{prefix}_tweets.jsonl"
    failed_path = ROOT / f"{prefix}_failed.txt"
    ranked_path = ROOT / f"{prefix}_ranked.csv"
    packets_path = ROOT / f"{prefix}_packets.jsonl"

    candidates_path.parent.mkdir(parents=True, exist_ok=True)

    planner_provider = args.query_planner_provider or args.llm_provider
    search_cmd = [
        sys.executable,
        str(SEARCH_SCRIPT),
        "--domain",
        args.domain,
        "--max_pages_per_query",
        str(args.max_pages_per_query),
        "--out",
        str(candidates_path.relative_to(ROOT)),
        "--query_planner",
        args.query_planner,
        "--query_count",
        str(args.query_count),
        "--query_type_mix",
        args.query_type_mix,
        "--discovery_mode",
        args.discovery_mode,
        "--min_query_quality",
        str(args.min_query_quality),
    ]
    if args.allow_query_fallback:
        search_cmd.append("--allow_query_fallback")
    for query in args.query:
        search_cmd.extend(["--query", query])
    if args.query_planner == "llm":
        search_cmd.extend([
            "--query_planner_provider",
            planner_provider,
            "--query_planner_timeout",
            str(args.query_planner_timeout),
            "--query_planner_retries",
            str(args.query_planner_retries),
            "--query_planner_json_mode",
            args.query_planner_json_mode,
        ])
        if args.query_planner_model:
            search_cmd.extend(["--query_planner_model", args.query_planner_model])
        elif args.llm_model:
            search_cmd.extend(["--query_planner_model", args.llm_model])
        if args.query_planner_api_key:
            search_cmd.extend(["--query_planner_api_key", args.query_planner_api_key])
        elif args.llm_api_key:
            search_cmd.extend(["--query_planner_api_key", args.llm_api_key])
        if args.query_planner_base_url:
            search_cmd.extend(["--query_planner_base_url", args.query_planner_base_url])
        elif args.llm_base_url:
            search_cmd.extend(["--query_planner_base_url", args.llm_base_url])
    run(search_cmd)

    candidates = read_candidates(
        candidates_path,
        args.min_followers,
        args.candidate_limit,
        args.domain,
        args.allow_media_accounts,
    )
    write_handles(candidates, handles_path)
    print(f"candidate_handles={len(candidates)} handles_out={handles_path}", flush=True)
    if not candidates:
        raise SystemExit("No candidates found after filters. Try lower min_followers or more pages.")

    fetch_cmd = [
        sys.executable,
        str(FETCH_SCRIPT),
        "--usernames",
        str(handles_path.relative_to(ROOT)),
        "--out",
        str(tweets_path.relative_to(ROOT)),
        "--failed_out",
        str(failed_path.relative_to(ROOT)),
        "--start",
        args.start,
        "--until",
        args.until,
        "--slice",
        args.slice,
        "--workers",
        str(args.workers),
        "--qps",
        str(args.qps),
        "--max_pages_per_slice",
        str(args.max_pages_per_slice),
    ]
    if args.tweets_per_candidate > 0:
        fetch_cmd.extend(["--max_tweets_per_user", str(args.tweets_per_candidate)])
    if args.strip_extended_entities:
        fetch_cmd.append("--strip_extended_entities")
    run(fetch_cmd)

    rank_cmd = [
        sys.executable,
        str(FIND_SCRIPT),
        "--domain",
        args.rank_domain,
        "--count",
        str(args.count),
        "--min_signals",
        str(args.min_signals),
        "--input",
        str(tweets_path.relative_to(ROOT)),
        "--out",
        str(ranked_path.relative_to(ROOT)),
        "--packets_out",
        str(packets_path.relative_to(ROOT)),
        "--ranker",
        args.ranker,
        "--tweet_judge",
        args.tweet_judge,
        "--tweet_judge_min_score",
        str(args.tweet_judge_min_score),
        "--tweet_judge_timeout",
        str(args.tweet_judge_timeout),
        "--tweet_judge_retries",
        str(args.tweet_judge_retries),
        "--tweet_judge_max_calls",
        str(args.tweet_judge_max_calls),
    ]
    if args.tweet_judge_provider:
        rank_cmd.extend(["--tweet_judge_provider", args.tweet_judge_provider])
    if args.tweet_judge_model:
        rank_cmd.extend(["--tweet_judge_model", args.tweet_judge_model])
    if args.tweet_judge_api_key:
        rank_cmd.extend(["--tweet_judge_api_key", args.tweet_judge_api_key])
    elif args.llm_api_key:
        rank_cmd.extend(["--tweet_judge_api_key", args.llm_api_key])
    if args.tweet_judge_base_url:
        rank_cmd.extend(["--tweet_judge_base_url", args.tweet_judge_base_url])
    elif args.llm_base_url:
        rank_cmd.extend(["--tweet_judge_base_url", args.llm_base_url])
    if args.ranker == "llm":
        rank_cmd.extend([
            "--llm_provider",
            args.llm_provider,
            "--llm_candidate_limit",
            str(args.llm_candidate_limit),
            "--llm_timeout",
            str(args.llm_timeout),
            "--llm_retries",
            str(args.llm_retries),
        ])
        if args.llm_model:
            rank_cmd.extend(["--llm_model", args.llm_model])
        if args.llm_api_key:
            rank_cmd.extend(["--llm_api_key", args.llm_api_key])
        if args.llm_base_url:
            rank_cmd.extend(["--llm_base_url", args.llm_base_url])
    run(rank_cmd)

    print("Done.", flush=True)
    print(f"candidates={candidates_path}", flush=True)
    print(f"handles={handles_path}", flush=True)
    print(f"tweets={tweets_path}", flush=True)
    print(f"ranked={ranked_path}", flush=True)
    print(f"packets={packets_path}", flush=True)


if __name__ == "__main__":
    main()
