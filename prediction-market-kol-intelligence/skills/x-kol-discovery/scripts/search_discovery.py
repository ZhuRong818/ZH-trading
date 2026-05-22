#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except Exception:
    yaml = None


BASE_URL = "https://api.twitterapi.io"
SEARCH_ENDPOINT = "/twitter/tweet/advanced_search"
TEMPLATES_PATH = Path(__file__).resolve().parents[1] / "references" / "query_templates.yaml"

QUERY_PLANNER_PROMPT = """You generate X/Twitter advanced-search queries for finding KOLs.
Return STRICT JSON only.

Goal: discover authors who are useful KOLs for the requested domain. The queries
should not merely find random tweets with matching keywords; they should be likely
to surface accounts that repeatedly publish domain-relevant analysis.

Return JSON:
{
  "domain_concepts": ["core topic 1", "core topic 2"],
  "quality_anchors": ["forecast", "analysis"],
  "negative_terms": ["spam", "referral"],
  "queries": [
    {
      "query_type": "Latest",
      "query": "(\"keyword\" OR \"keyword\") (\"forecast\" OR \"risk\" OR \"will\")",
      "intent": "short reason"
    }
  ]
}

Rules:
- query_type must be Latest or Top.
- Prefer Latest for active discovery.
- Use X advanced-search boolean syntax with quotes, OR, and parentheses.
- Do not use since:, until:, from:, lang:, min_faves:, min_retweets:, URLs, or line breaks.
- Each query must be broad enough to discover authors, not just one account.
- Avoid spammy giveaway/referral/airdrop terms unless the user explicitly asks for them.
- Keep each query under 280 characters.
- Every query should combine at least one domain concept with at least one quality anchor.
- Use negative terms with -term when helpful to remove generic news, entertainment, finance,
  crypto, spam, or other off-domain noise.
- Generate exactly the requested number of queries.
- Each query should be self-contained and domain-specific. Avoid generic one-word concepts.
- Use at most 3 OR terms per parenthesized group.
- Do not end a query with an operator or dangling negative sign.

Query strategy by discovery_mode:
- topic_density: optimize for accounts that repeatedly cover the domain. Favor professional
  identity anchors, technical vocabulary, recurring update/report formats, research/lab/
  institutional context, and phrases that imply ongoing analysis.
- predictive: optimize for authors making forecasts, probabilistic claims, explicit views,
  market-relevant claims, or event-outcome analysis.
- balanced: mix topic_density and predictive strategies.

Useful query families, adapted to the user's domain:
1. professional identity: analyst, researcher, practitioner, domain expert, professor,
   scientist, engineer, investor, journalist, forecaster, or other relevant roles.
2. recurring analysis: weekly update, situational report, dashboard, tracker, model,
   monitor, forecast, briefing, risk assessment, thread, notes.
3. technical vocabulary: domain-specific terms that only serious accounts use.
4. judgment terms: will, likely, unlikely, risk, probability, odds, forecast, expects,
   scenario, trend, leading indicator.
"""


def load_templates() -> Dict[str, Any]:
    text = TEMPLATES_PATH.read_text(encoding="utf-8")
    if yaml is not None:
        return yaml.safe_load(text)
    # Minimal fallback for environments without PyYAML: use the built-in defaults.
    return {
        "prediction_market": {
            "latest": [
                '"Polymarket" ("will" OR "odds" OR "chance" OR "probability")',
                '"Polymarket" ("YES" OR "NO" OR "buy YES" OR "buy NO")',
                '"Polymarket" ("resolve" OR "resolution" OR "market")',
            ],
            "top": ['"Polymarket" ("prediction" OR "forecast" OR "odds")'],
        }
    }


def request_json(api_key: str, params: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{BASE_URL}{SEARCH_ENDPOINT}?{query}",
        headers={"X-API-Key": api_key},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
    raise ValueError(f"Unsupported query planner provider: {provider}")


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


def validate_query_item(item: Dict[str, Any], query_type_mix: str) -> Optional[Dict[str, str]]:
    query = str(item.get("query") or "").strip()
    query_type = str(item.get("query_type") or item.get("queryType") or "Latest").strip().title()
    intent = str(item.get("intent") or "").strip()

    if query_type not in {"Latest", "Top"}:
        query_type = "Latest"
    if query_type_mix == "latest" and query_type != "Latest":
        return None
    if query_type_mix == "top" and query_type != "Top":
        return None
    if "\n" in query or "\r" in query:
        return None
    if len(query) < 8 or len(query) > 280:
        return None
    if query.endswith("-"):
        return None

    lowered = query.lower()
    blocked_terms = [" since:", " until:", " from:", " lang:", " min_faves:", " min_retweets:", "http://", "https://"]
    if any(term in f" {lowered}" for term in blocked_terms):
        return None
    return {"query_type": query_type, "query": query, "intent": intent[:160]}


def add_valid_query(
    planned: List[Dict[str, str]],
    seen: set[tuple[str, str]],
    item: Dict[str, Any],
    query_type_mix: str,
    query_count: int,
) -> None:
    valid = validate_query_item(item, query_type_mix)
    if not valid:
        return
    key = (valid["query_type"], valid["query"].lower())
    if key in seen:
        return
    seen.add(key)
    planned.append(valid)


def parse_planned_queries(raw: Dict[str, Any], query_type_mix: str, query_count: int) -> List[Dict[str, str]]:
    raw_queries = raw.get("queries") if isinstance(raw.get("queries"), list) else []
    planned: List[Dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_queries:
        if not isinstance(item, dict):
            continue
        add_valid_query(planned, seen, item, query_type_mix, query_count)
        if len(planned) >= query_count:
            break
    return planned


def query_quality_score(query: str, domain: str, discovery_mode: str) -> int:
    lower = query.lower()
    domain_terms = [term for term in domain_keywords(domain) if len(term) >= 4]
    domain_hits = sum(1 for term in domain_terms if term in lower)
    quality_terms = [
        "forecast", "target", "will", "likely", "unlikely", "odds", "probability",
        "analysis", "research", "thread", "risk", "scenario", "model", "tracker",
        "setup", "thesis", "catalyst", "expects", "expect",
    ]
    quality_hits = sum(1 for term in quality_terms if term in lower)
    negatives = len(re.findall(r"(?:^|\s)-[A-Za-z][A-Za-z0-9_+-]+", query))
    structure = int("(" in query and ")" in query) + int(" OR " in query)
    score = min(6, domain_hits * 2) + min(5, quality_hits) + min(3, negatives) + structure
    if discovery_mode == "predictive" and not any(term in lower for term in ("forecast", "target", "will", "likely", "expect", "odds", "probability")):
        score -= 4
    if re.search(r"\b(news|headline|headlines|giveaway|airdrop|referral)\b", lower) and "-" not in lower:
        score -= 2
    return score


def score_and_rank_queries(
    planned: List[Dict[str, str]],
    domain: str,
    discovery_mode: str,
) -> List[Dict[str, str]]:
    for item in planned:
        score = query_quality_score(item["query"], domain, discovery_mode)
        item["quality"] = str(score)
        item["intent"] = f"{item.get('intent', '')};quality={score}".strip(";")
    return sorted(planned, key=lambda item: int(item.get("quality") or 0), reverse=True)


def select_planned_queries(
    planned: List[Dict[str, str]],
    domain: str,
    discovery_mode: str,
    min_quality: int,
    query_count: int,
) -> List[Dict[str, str]]:
    ranked = score_and_rank_queries(planned, domain, discovery_mode)
    if min_quality <= 0:
        return ranked[:query_count]

    strong = [item for item in ranked if int(item.get("quality") or 0) >= min_quality]
    if len(strong) >= query_count:
        return strong[:query_count]

    if len(ranked) >= query_count:
        print(
            f"[WARN] Only {len(strong)}/{query_count} LLM queries met min_query_quality={min_quality}; "
            "using highest-scored LLM queries instead of fallback.",
            flush=True,
        )
        return ranked[:query_count]
    return strong


def fill_missing_queries(
    planned: List[Dict[str, str]],
    domain: str,
    query_count: int,
    query_type_mix: str,
    discovery_mode: str,
) -> List[Dict[str, str]]:
    if len(planned) >= query_count:
        return planned

    seen = {(item["query_type"], item["query"].lower()) for item in planned}
    for item in fallback_queries_from_domain(domain, query_count, query_type_mix, discovery_mode):
        key = (item["query_type"], item["query"].lower())
        if key in seen:
            continue
        seen.add(key)
        planned.append(item)
        if len(planned) >= query_count:
            break
    return planned


def recover_queries_from_malformed_text(text: str, query_type_mix: str, query_count: int) -> List[Dict[str, str]]:
    planned: List[Dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    current_type = "Latest"

    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if "query_type" in lowered or "querytype" in lowered:
            if "top" in lowered:
                current_type = "Top"
            elif "latest" in lowered:
                current_type = "Latest"
        if '"query"' not in lowered and "'query'" not in lowered and "query:" not in lowered:
            continue
        query_key_pos = max(stripped.find('"query"'), stripped.find("'query'"), stripped.lower().find("query:"))
        colon_pos = stripped.find(":", query_key_pos)
        if colon_pos < 0:
            continue
        query = stripped[colon_pos + 1:].strip().rstrip(",").strip()
        for marker in [',"intent"', ", 'intent'", ', "intent"', ", intent"]:
            marker_pos = query.lower().find(marker.lower())
            if marker_pos >= 0:
                query = query[:marker_pos].strip().rstrip(",")
                break
        query = query.strip('"').strip("'").replace('\\"', '"').replace("\\'", "'")
        query = query.strip()
        if query.endswith('"') or query.endswith("'"):
            query = query[:-1].strip()
        if "(" not in query and " OR " not in query:
            continue
        add_valid_query(
            planned,
            seen,
            {"query_type": current_type, "query": query, "intent": "recovered_from_malformed_llm_json"},
            query_type_mix,
            query_count,
        )
        if len(planned) >= query_count:
            break
    return planned


def domain_keywords(domain: str) -> List[str]:
    stopwords = {
        "english", "language", "accounts", "account", "people", "repeatedly",
        "discuss", "exclude", "generic", "spam", "only", "pure", "with", "and",
        "the", "for", "who", "make", "calls", "market", "markets", "domain",
        "analysts", "analyst", "forecasters", "forecaster", "researchers",
        "researcher", "traders", "trader", "english-language", "news",
        "aggregators", "aggregator", "promo", "airdrop", "referral", "meme",
        "bots", "bot", "accounts", "account",
    }
    tokens: List[str] = []
    seen = set()
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_+-]{2,}", domain.lower()):
        if token in stopwords or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens[:24]


def fallback_queries_from_domain(
    domain: str,
    query_count: int,
    query_type_mix: str,
    discovery_mode: str,
) -> List[Dict[str, str]]:
    keywords = domain_keywords(domain)
    if not keywords:
        keywords = ["forecast", "analysis", "risk", "market"]

    if discovery_mode == "topic_density":
        anchors = ['"analysis"', '"research"', '"report"', '"tracker"', '"dashboard"', '"update"']
    elif discovery_mode == "predictive":
        anchors = ['"forecast"', '"target"', '"will"', '"likely"', '"thesis"', '"setup"']
    else:
        anchors = ['"analysis"', '"forecast"', '"risk"', '"likely"', '"update"', '"thread"']

    query_type = "Top" if query_type_mix == "top" else "Latest"
    negative = "-giveaway -airdrop -referral -promo -bot"
    planned: List[Dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for idx in range(0, max(len(keywords), 1), 3):
        group = keywords[idx:idx + 3] or keywords[:3]
        concept = "(" + " OR ".join(f'"{term}"' for term in group) + ")"
        anchor_group = "(" + " OR ".join(anchors[:3]) + ")"
        query = f"{concept} {anchor_group} {negative}"
        add_valid_query(
            planned,
            seen,
            {"query_type": query_type, "query": query, "intent": "fallback_domain_keywords"},
            query_type_mix,
            query_count,
        )
        if len(planned) >= query_count:
            break

    return planned


def plan_queries_with_llm(
    domain: str,
    language: str,
    query_count: int,
    provider: str,
    model: str,
    api_key: Optional[str],
    base_url: Optional[str],
    timeout: int,
    retries: int,
    query_type_mix: str,
    discovery_mode: str,
    min_query_quality: int,
    allow_fallback: bool,
    json_mode: str,
) -> List[Dict[str, str]]:
    cfg = provider_config(provider, model, api_key, base_url)
    if not cfg["api_key"]:
        env = "DEEPSEEK_API_KEY" if provider == "deepseek" else "DASHSCOPE_API_KEY" if provider == "qwen" else "OPENAI_API_KEY"
        raise RuntimeError(f"Missing query planner API key. Set {env} or pass --query_planner_api_key.")

    messages = [
            {"role": "system", "content": QUERY_PLANNER_PROMPT},
            {
                "role": "user",
                "content": (
                    "Return JSON.\n"
                    f"Requested KOL domain: {domain}\n"
                    f"Language: {language}\n"
                    f"Number of queries: {query_count}\n"
                    f"Allowed query_type mix: {query_type_mix}\n"
                    f"Discovery mode: {discovery_mode}\n"
                    "\n"
                    "Important: optimize the searches for accounts with repeated topic density, "
                    "not one-off mentions. Avoid broad news-only queries unless the requested "
                    "domain is specifically news/media KOLs.\n"
                    "Every query must include domain-specific concepts from the requested domain "
                    "and quality/prediction anchors. Return valid JSON with exactly the requested "
                    "number of query objects.\n"
                ),
            },
        ]
    body: Dict[str, Any] = {
        "model": cfg["model"],
        "messages": messages,
    }
    use_json_mode = json_mode == "on" or (json_mode == "auto" and provider.lower() != "deepseek")
    if use_json_mode:
        body["response_format"] = {"type": "json_object"}
    if provider.lower() == "deepseek":
        body["max_tokens"] = 1800
        body["extra_body"] = {"thinking": {"type": "disabled"}}
    elif provider.lower() == "qwen":
        body["extra_body"] = {"enable_thinking": False}
    else:
        body["max_tokens"] = 1000

    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            attempt_body = dict(body)
            if attempt > 1:
                attempt_body["messages"] = messages + [{
                    "role": "user",
                    "content": (
                        f"Previous attempt failed: {last_error}. Return only valid JSON. "
                        f"Include exactly {query_count} complete query objects."
                    ),
                }]
            req = urllib.request.Request(
                cfg["base_url"] + "/chat/completions",
                data=json.dumps(attempt_body).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {cfg['api_key']}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            content = message.get("content") or choice.get("text") or ""
            if not str(content).strip():
                finish_reason = choice.get("finish_reason")
                message_keys = ",".join(sorted(message.keys()))
                raise RuntimeError(
                    "empty LLM response content "
                    f"(finish_reason={finish_reason}, message_keys={message_keys}, "
                    f"json_mode={'on' if use_json_mode else 'off'}, model={cfg['model']})"
                )
            try:
                raw = extract_json_object(content)
                planned = parse_planned_queries(raw, query_type_mix, query_count)
            except Exception as parse_exc:
                planned = recover_queries_from_malformed_text(content, query_type_mix, query_count)
                if planned:
                    print(f"[WARN] recovered {len(planned)} queries from malformed LLM planner JSON: {parse_exc}", flush=True)
                else:
                    raise RuntimeError(f"LLM query planner returned invalid JSON and no recoverable queries: {parse_exc}") from parse_exc
            planned = select_planned_queries(planned, domain, discovery_mode, min_query_quality, query_count)
            if not planned:
                raise RuntimeError("LLM query planner returned no valid queries.")
            if len(planned) < query_count:
                raise RuntimeError(
                    f"LLM query planner returned only {len(planned)}/{query_count} valid high-quality queries."
                )
            return planned
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in (429, 500, 502, 503, 504) or attempt >= retries:
                body_text = exc.read().decode("utf-8", errors="replace")[:500]
                raise RuntimeError(f"Query planner HTTP {exc.code}: {body_text}") from exc
            time.sleep(2 ** attempt)
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                if not allow_fallback:
                    raise RuntimeError(
                        f"LLM query planner failed after {retries} attempts and fallback is disabled. "
                        f"Last error: {exc}"
                    ) from exc
                fallback = fallback_queries_from_domain(domain, query_count, query_type_mix, discovery_mode)
                if fallback:
                    print(
                        f"[WARN] LLM query planner failed after {retries} attempts; using fallback domain queries. "
                        f"Last error: {exc}",
                        flush=True,
                    )
                    return fallback
                raise
            time.sleep(2 ** attempt)
    if not allow_fallback:
        raise RuntimeError(f"LLM query planner failed and fallback is disabled. Last error: {last_error}")
    fallback = fallback_queries_from_domain(domain, query_count, query_type_mix, discovery_mode)
    if fallback:
        print(f"[WARN] LLM query planner failed; using fallback domain queries. Last error: {last_error}", flush=True)
        return fallback
    raise RuntimeError(f"Query planner failed: {last_error}")


def plan_queries_from_templates(domain: str, query_type_mix: str) -> List[Dict[str, str]]:
    templates = load_templates()
    domain_cfg = templates.get(domain) or templates.get(domain.lower()) or templates["prediction_market"]
    queries: List[Dict[str, str]] = []
    if query_type_mix in ("latest", "both"):
        queries.extend({"query_type": "Latest", "query": q, "intent": "template"} for q in domain_cfg.get("latest", []))
    if query_type_mix in ("top", "both"):
        queries.extend({"query_type": "Top", "query": q, "intent": "template"} for q in domain_cfg.get("top", []))
    return queries


def plan_manual_queries(raw_queries: List[str], query_type_mix: str) -> List[Dict[str, str]]:
    query_type = "Top" if query_type_mix == "top" else "Latest"
    queries: List[Dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_queries:
        add_valid_query(
            queries,
            seen,
            {"query_type": query_type, "query": raw, "intent": "manual"},
            query_type_mix,
            len(raw_queries),
        )
    return queries


def safe_get_tweets(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    if isinstance(resp.get("tweets"), list):
        return resp["tweets"]
    data = resp.get("data")
    if isinstance(data, dict):
        for key in ("tweets", "items", "list"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def page_info(resp: Dict[str, Any]) -> tuple[bool, str]:
    data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    return bool(data.get("has_next_page")), str(data.get("next_cursor") or "")


def author_from_tweet(tweet: Dict[str, Any]) -> Dict[str, Any]:
    author = tweet.get("author") or {}
    return {
        "author_id": str(author.get("id") or ""),
        "username": author.get("userName") or author.get("username") or author.get("screen_name"),
        "display_name": author.get("name"),
        "bio": author.get("description") or "",
        "followers": int(author.get("followers") or 0),
        "is_blue_verified": bool(author.get("isBlueVerified")),
        "is_verified": bool(author.get("isVerified")),
        "is_automated": bool(author.get("isAutomated")),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", default="prediction_market")
    parser.add_argument("--language", default="en")
    parser.add_argument("--api_key", default=os.getenv("TWITTERAPI_IO_KEY"))
    parser.add_argument("--out", required=True)
    parser.add_argument("--max_pages_per_query", type=int, default=3)
    parser.add_argument("--query_type_mix", choices=["latest", "top", "both"], default="both")
    parser.add_argument(
        "--query",
        action="append",
        default=[],
        help="Manual X advanced-search query. Repeatable. When set, skips static/LLM query planning.",
    )
    parser.add_argument("--query_planner", choices=["static", "llm"], default="static")
    parser.add_argument(
        "--discovery_mode",
        choices=["balanced", "topic_density", "predictive"],
        default="balanced",
        help="LLM query objective. topic_density improves candidate quality before fetching.",
    )
    parser.add_argument("--query_count", type=int, default=8)
    parser.add_argument("--query_planner_provider", choices=["deepseek", "qwen", "openai"], default="deepseek")
    parser.add_argument("--query_planner_model", default="")
    parser.add_argument("--query_planner_api_key")
    parser.add_argument("--query_planner_base_url")
    parser.add_argument("--query_planner_timeout", type=int, default=60)
    parser.add_argument("--query_planner_retries", type=int, default=3)
    parser.add_argument(
        "--query_planner_json_mode",
        choices=["auto", "on", "off"],
        default="auto",
        help="Use chat-completions JSON mode for query planning. Auto disables it for DeepSeek.",
    )
    parser.add_argument(
        "--min_query_quality",
        type=int,
        default=8,
        help="Minimum local quality score for LLM-planned queries. Use 0 to disable.",
    )
    parser.add_argument(
        "--allow_query_fallback",
        action="store_true",
        help="Allow keyword fallback queries if the LLM planner fails after retries.",
    )
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--sleep", type=float, default=0.25)
    args = parser.parse_args()

    api_key = (args.api_key or "").strip()
    if not api_key:
        raise SystemExit("Missing API key. Set TWITTERAPI_IO_KEY or pass --api_key.")

    if args.query:
        queries = plan_manual_queries(args.query, args.query_type_mix)
        if not queries:
            raise SystemExit("No valid manual --query values.")
        args.query_planner = "manual"
    elif args.query_planner == "llm":
        try:
            queries = plan_queries_with_llm(
                args.domain,
                args.language,
                max(1, args.query_count),
                args.query_planner_provider,
                args.query_planner_model,
                args.query_planner_api_key,
                args.query_planner_base_url,
                args.query_planner_timeout,
                args.query_planner_retries,
                args.query_type_mix,
                args.discovery_mode,
                args.min_query_quality,
                args.allow_query_fallback,
                args.query_planner_json_mode,
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
    else:
        queries = plan_queries_from_templates(args.domain, args.query_type_mix)[: max(1, args.query_count)]

    print(f"planner={args.query_planner} discovery_mode={args.discovery_mode} queries={len(queries)}")
    for idx, item in enumerate(queries, 1):
        suffix = f" // {item['intent']}" if item.get("intent") else ""
        print(f"[QUERY {idx}] {item['query_type']} {item['query']}{suffix}")

    candidates: Dict[str, Dict[str, Any]] = {}
    evidence = defaultdict(list)
    seen_tweets = set()

    for item in queries:
        query_type = item["query_type"]
        query_text = item["query"]
        cursor = ""
        seen_cursors = set()
        for page in range(1, args.max_pages_per_query + 1):
            resp = request_json(api_key, {"query": query_text, "queryType": query_type, "cursor": cursor}, args.timeout)
            tweets = safe_get_tweets(resp)
            for tweet in tweets:
                tid = str(tweet.get("id") or "")
                if tid in seen_tweets:
                    continue
                seen_tweets.add(tid)
                author = author_from_tweet(tweet)
                key = author["author_id"] or str(author.get("username") or "").lower()
                if not key:
                    continue
                if key not in candidates:
                    candidates[key] = author
                    candidates[key]["matched_queries"] = set()
                    candidates[key]["matched_tweets"] = 0
                candidates[key]["matched_queries"].add(query_text)
                candidates[key]["matched_tweets"] += 1
                evidence[key].append({
                    "tweet_id": tid,
                    "tweet_url": tweet.get("url") or tweet.get("twitterUrl"),
                    "query": query_text,
                    "query_type": query_type,
                    "text": str(tweet.get("text") or "")[:500],
                })

            has_next, next_cursor = page_info(resp)
            if not has_next or not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            time.sleep(args.sleep)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for key, candidate in candidates.items():
            matched_queries = candidate.pop("matched_queries")
            out = dict(candidate)
            out["candidate_key"] = key
            out["distinct_query_hits"] = len(matched_queries)
            out["matched_queries"] = sorted(matched_queries)
            out["evidence"] = evidence[key][:10]
            out["discovery_score"] = (
                15 * out["distinct_query_hits"]
                + 3 * out["matched_tweets"]
                + 0.001 * min(out["followers"], 500000)
                - (20 if out["is_automated"] else 0)
            )
            f.write(json.dumps(out, ensure_ascii=False) + "\n")

    print(f"candidates={len(candidates)}")
    print(f"out={args.out}")


if __name__ == "__main__":
    main()
