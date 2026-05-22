#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import email.utils
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple


LLM_TWEET_JUDGE_PROMPT = """You judge whether an X/Twitter post is a useful prediction-market signal.
Return STRICT JSON only.

Accept only tweets that make a forward-looking, falsifiable, market-relevant claim.
Reject spam, generic news, vague hype, retrospective recap, PnL flexing, engagement bait,
and commentary without a clear future outcome.

Use the requested domain as guidance, but allow genuinely predictive cross-domain tweets.

Return JSON:
{
  "keep": true,
  "quality_score": 0,
  "domain": "",
  "event": "",
  "direction": "YES|NO|UP|DOWN|OVER|UNDER|WIN|LOSE|DELAY|UNKNOWN",
  "deadline": null,
  "is_falsifiable": true,
  "reason": ""
}

quality_score is 0-10:
- 0-3 reject
- 4-5 weak/watch only
- 6-7 usable signal
- 8-10 strong signal
"""


PREDICTIVE_PATTERNS = [
    r"\bwill\b",
    r"\bwon't\b",
    r"\bgoing to\b",
    r"\blikely\b",
    r"\bunlikely\b",
    r"\bexpect(?:ed|s)?\b",
    r"\bforecast\b",
    r"\bpredict(?:s|ed|ion)?\b",
    r"\bby\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\d{1,2})",
    r"\bbefore\b",
    r"\bafter\b",
    r"\bdeadline\b",
    r"\bodds?\b",
    r"\bchance\b",
    r"\bprobability\b",
    r"\bpriced?\b",
    r"\bwin\b",
    r"\blose\b",
    r"\bover\b",
    r"\bunder\b",
    r"\bclose[s]?\b",
]

FORWARD_PATTERNS = [
    r"\bwill\b",
    r"\bwon't\b",
    r"\bgoing to\b",
    r"\blikely\b",
    r"\bunlikely\b",
    r"\bexpect(?:ed|s)?\b",
    r"\bforecast\b",
    r"\bpredict(?:s|ed|ion)?\b",
    r"\bby\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\d{1,2})",
    r"\bbefore\b",
    r"\bodds?\b",
    r"\bchance\b",
    r"\bprobability\b",
    r"\bpriced?\b",
]

SPAM_PATTERNS = [
    r"\breferral\b",
    r"\buse my code\b",
    r"\bsign up\b",
    r"\bgiveaway\b",
    r"\bairdrop\b",
    r"\bfollow(?: me)?\b",
    r"\blike and retweet\b",
    r"\bjoin discord\b",
    r"\btrade in bio\b",
    r"\blink in bio\b",
    r"\bcode=([a-z0-9_-]+)\b",
    r"\br=deposited\b",
    r"\bguaranteed winning position\b",
    r"\bfree money\b",
    r"\bif you don't hear about him\b",
    r"\bwhat are you doing here\b",
]

TRADER_FLEX_PATTERNS = [
    r"\bpolymarket trader\b",
    r"\bmade over\b.*\btrades\b",
    r"\bwin rate\b",
    r"\bstarted with just\b",
    r"\bturned (?:it )?into\b",
    r"\bhere'?s how (?:he|she|they) trades\b",
    r"\bhis positions\b",
    r"\bwallet link\b",
    r"\bmaker rebate\b",
    r"\btop\s+\d+\s+polymarket trader\b",
]

AUTOMATED_MARKET_SUMMARY_PATTERNS = [
    r"\b自动发推\b",
    r"\bdaily_\b",
    r"\bvolume surged\b",
    r"\btop\s+\d+\s+traders holding\b",
    r"\bedge:\s*[-+]?\d+(?:\.\d+)?%",
    r"\broi\b",
    r"\bvolume\b.*\b(?:surged|increased|decreased)\b",
    r"\bwho wins this\b",
    r"\bbo3\b",
]

LOW_VALUE_MARKET_PROMO_PATTERNS = [
    r"\bhow to make money on\b",
    r"\bfirst method\b",
    r"\bgo to polymarket\b",
    r"\bbuy\s+(?:yes|no)\b",
    r"\bguaranteed\b",
    r"\byou only get a few percent\b",
]

RETROSPECTIVE_PATTERNS = [
    r"\bwas on fire\b",
    r"\ball hit\b",
    r"\bnetting\b",
    r"\bbad beats?\b",
    r"\brough day\b",
    r"\bcashed\b",
    r"\bprofit(?:ed)?\b",
]

DOMAIN_KEYWORDS = {
    "Politics / election": [
        "election", "poll", "senate", "president", "congress", "vote", "primary",
        "trump", "biden", "harris", "democrat", "republican", "parliament",
    ],
    "Crypto price": [
        "btc", "bitcoin", "eth", "ethereum", "sol", "solana", "crypto", "token",
        "coin", "stablecoin", "onchain", "on-chain",
    ],
    "Crypto regulation": [
        "sec", "cftc", "etf", "regulation", "regulatory", "lawsuit", "approval",
        "ban crypto", "stablecoin bill",
    ],
    "Company earnings/performance": [
        "earnings", "revenue", "guidance", "eps", "stock", "shares", "company",
        "launch", "product", "margin", "sales",
    ],
    "Geopolitics / war / sanctions": [
        "war", "ceasefire", "sanction", "iran", "israel", "russia", "ukraine",
        "china", "taiwan", "missile", "airspace", "border", "nato",
    ],
    "Macro / Fed / CPI / rates": [
        "fed", "fomc", "cpi", "inflation", "rates", "rate cut", "rate hike",
        "jobs", "unemployment", "gdp", "treasury", "yield",
    ],
    "Health / pandemic": [
        "pandemic", "epidemic", "outbreak", "covid", "sars-cov-2", "variant",
        "vaccine", "vaccination", "booster", "h5n1", "bird flu", "avian flu",
        "mpox", "virus", "viral", "cdc", "who", "fda", "hospitalization",
        "case surge", "wastewater", "public health", "lockdown",
    ],
    "Sports": [
        "vs", "spread", "game", "match", "win", "championship", "nba", "nfl",
        "mlb", "nhl", "soccer", "football", "tennis", "aces", "sun",
    ],
}

MONTHS = {
    "jan": "01", "january": "01",
    "feb": "02", "february": "02",
    "mar": "03", "march": "03",
    "apr": "04", "april": "04",
    "may": "05",
    "jun": "06", "june": "06",
    "jul": "07", "july": "07",
    "aug": "08", "august": "08",
    "sep": "09", "sept": "09", "september": "09",
    "oct": "10", "october": "10",
    "nov": "11", "november": "11",
    "dec": "12", "december": "12",
}


def iter_jsonl(paths: List[str], max_rows: int) -> Iterable[Dict[str, Any]]:
    rows = 0
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"Invalid JSON in {path}:{line_no}: {exc}") from exc
                rows += 1
                if max_rows and rows >= max_rows:
                    return


def text_of(row: Dict[str, Any]) -> str:
    tweet = row.get("tweet") or {}
    return str(tweet.get("text") or "")


def count_pattern_hits(lower: str, patterns: List[str]) -> int:
    return sum(1 for pattern in patterns if re.search(pattern, lower))


def spam_filter_reason(text: str, tweet_type: str) -> Optional[str]:
    lower = text.lower()
    if tweet_type == "retweet":
        return "retweet"
    if len(lower) < 20:
        return "too_short"
    if re.search(r"https?://(?:www\.)?polymarket\.com/@[^ \n?]+[^\s]*[?&]r=", lower):
        return "polymarket_referral_link"
    if re.search(r"https?://(?:www\.)?predictparity\.com/[^\s]*[?&]code=", lower):
        return "predictparity_referral_link"
    if count_pattern_hits(lower, AUTOMATED_MARKET_SUMMARY_PATTERNS) >= 2:
        return "automated_market_summary"
    if count_pattern_hits(lower, LOW_VALUE_MARKET_PROMO_PATTERNS) >= 2:
        return "low_value_market_promo"
    if count_pattern_hits(lower, TRADER_FLEX_PATTERNS) >= 2:
        return "trader_flex_thread"
    if count_pattern_hits(lower, SPAM_PATTERNS):
        return "referral_or_engagement_spam"
    if lower.count("http") >= 3 and not any(w in lower for w in ("will", "likely", "odds", "market")):
        return "link_farm"
    return None


def is_spam_or_noise(text: str, tweet_type: str) -> bool:
    return spam_filter_reason(text, tweet_type) is not None


def predictive_score(text: str) -> int:
    lower = text.lower()
    return sum(1 for p in PREDICTIVE_PATTERNS if re.search(p, lower))


def has_forward_signal(text: str) -> bool:
    lower = text.lower()
    return any(re.search(p, lower) for p in FORWARD_PATTERNS)


def is_retrospective_noise(text: str) -> bool:
    lower = text.lower()
    return any(re.search(p, lower) for p in RETROSPECTIVE_PATTERNS) and not has_forward_signal(text)


def keyword_hit(lower: str, keyword: str) -> bool:
    if re.fullmatch(r"[a-z0-9_]+", keyword):
        return re.search(rf"\b{re.escape(keyword)}\b", lower) is not None
    return keyword in lower


def classify_domain(text: str) -> str:
    lower = text.lower()
    scores = {
        domain: sum(1 for kw in keywords if keyword_hit(lower, kw))
        for domain, keywords in DOMAIN_KEYWORDS.items()
    }
    domain, score = max(scores.items(), key=lambda item: item[1])
    return domain if score > 0 else "Other"


def classify_direction(text: str) -> str:
    lower = text.lower()
    if re.search(r"\b(no|not|won't|unlikely|doesn't|dont|don't|never)\b", lower):
        return "NO"
    if re.search(r"\b(over|above|higher|more than|greater than)\b", lower):
        return "OVER"
    if re.search(r"\b(under|below|lower|less than)\b", lower):
        return "UNDER"
    if re.search(r"\b(up|pump|rally|increase|rise|bullish)\b", lower):
        return "UP"
    if re.search(r"\b(down|dump|drop|fall|decline|bearish)\b", lower):
        return "DOWN"
    if re.search(r"\b(win|wins|winner|victory)\b", lower):
        return "WIN"
    if re.search(r"\b(lose|loses|loss|defeat)\b", lower):
        return "LOSE"
    if re.search(r"\b(delay|delayed|postpone|postponed)\b", lower):
        return "DELAY"
    return "YES"


def parse_created_year(row: Dict[str, Any]) -> int:
    raw = str(row.get("created_at") or (row.get("tweet") or {}).get("createdAt") or "")
    match = re.search(r"\b(20\d{2})\b", raw)
    return int(match.group(1)) if match else dt.datetime.now(dt.timezone.utc).year


def extract_deadline(text: str, row: Dict[str, Any]) -> Optional[str]:
    lower = text.lower()
    year = parse_created_year(row)
    match = re.search(
        r"\bby\s+("
        r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
        r")\.?\s+(\d{1,2})\b",
        lower,
    )
    if match:
        month = MONTHS[match.group(1).rstrip(".")]
        day = int(match.group(2))
        return f"{year:04d}-{month}-{day:02d}"
    match = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", lower)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    return None


def normalize_event(text: str) -> str:
    cleaned = re.sub(r"https?://\S+", "", text).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.strip("\"' ")
    if len(cleaned) > 220:
        cleaned = cleaned[:217].rstrip() + "..."
    return cleaned


def engagement(tweet: Dict[str, Any]) -> int:
    return sum(int(tweet.get(k) or 0) for k in ("likeCount", "retweetCount", "replyCount", "quoteCount", "bookmarkCount"))


def parse_tweet_datetime(value: Any) -> dt.datetime:
    if not value:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    raw = str(value)
    try:
        return email.utils.parsedate_to_datetime(raw).astimezone(dt.timezone.utc)
    except Exception:
        pass
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except Exception:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def provider_config(provider: str, model: str, api_key: Optional[str], base_url: Optional[str]) -> Dict[str, str]:
    p = provider.lower()
    if p == "deepseek":
        return {
            "api_key": api_key or os.getenv("DEEPSEEK_API_KEY", ""),
            "base_url": (base_url or "https://api.deepseek.com").rstrip("/"),
            "model": model or "deepseek-chat",
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
    raise ValueError(f"Unsupported LLM provider: {provider}")


def extract_json_object(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("empty LLM response content")
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


class TweetJudge:
    def __init__(
        self,
        enabled: bool,
        domain: str,
        min_score: int,
        provider: str,
        model: str,
        api_key: Optional[str],
        base_url: Optional[str],
        timeout: int,
        retries: int,
        max_calls: int,
    ):
        self.enabled = enabled
        self.domain = domain
        self.min_score = min_score
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.retries = retries
        self.max_calls = max_calls
        self.calls = 0
        self.skipped = 0
        self.failed = 0
        self.accepted = 0
        self.rejected = 0
        self._config: Optional[Dict[str, str]] = None

    def judge(self, signal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        if self.max_calls > 0 and self.calls >= self.max_calls:
            self.skipped += 1
            return None
        cfg = self._config or provider_config(self.provider, self.model, self.api_key, self.base_url)
        self._config = cfg
        if not cfg["api_key"]:
            env = "DEEPSEEK_API_KEY" if self.provider == "deepseek" else "DASHSCOPE_API_KEY" if self.provider == "qwen" else "OPENAI_API_KEY"
            raise RuntimeError(f"Missing tweet judge API key. Set {env} or pass --tweet_judge_api_key.")

        packet = {
            "requested_domain": self.domain,
            "heuristic_signal": {
                "handle": signal.get("handle"),
                "created_at": signal.get("created_at"),
                "heuristic_domain": signal.get("domain"),
                "heuristic_direction": signal.get("direction"),
                "heuristic_deadline": signal.get("deadline"),
                "heuristic_signal_score": signal.get("signal_score"),
                "engagement": signal.get("engagement"),
                "text": signal.get("text"),
            },
        }
        body: Dict[str, Any] = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": LLM_TWEET_JUDGE_PROMPT},
                {"role": "user", "content": "Return JSON.\n\nTweet packet:\n" + json.dumps(packet, ensure_ascii=False)},
            ],
            "max_tokens": 600,
        }
        if self.provider == "qwen":
            body["extra_body"] = {"enable_thinking": False}
        elif self.provider == "deepseek":
            body["extra_body"] = {"thinking": {"type": "disabled"}}

        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            cfg["base_url"] + "/chat/completions",
            data=payload,
            headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
            method="POST",
        )

        last_error: Optional[Exception] = None
        self.calls += 1
        for attempt in range(1, self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                choice = (data.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                content = message.get("content") or choice.get("text") or ""
                result = extract_json_object(content)
                return result
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in (429, 500, 502, 503, 504) or attempt >= self.retries:
                    self.failed += 1
                    body_text = exc.read().decode("utf-8", errors="replace")[:300]
                    raise RuntimeError(f"Tweet judge HTTP {exc.code}: {body_text}") from exc
                time.sleep(2 ** attempt)
            except Exception as exc:
                last_error = exc
                if attempt >= self.retries:
                    self.failed += 1
                    raise RuntimeError(f"Tweet judge failed: {exc}") from exc
                time.sleep(2 ** attempt)
        self.failed += 1
        raise RuntimeError(f"Tweet judge failed: {last_error}")


def apply_tweet_judgment(signal: Dict[str, Any], judge: Optional[TweetJudge]) -> Tuple[bool, str]:
    if judge is None or not judge.enabled:
        return True, "heuristic_only"
    result = judge.judge(signal)
    if result is None:
        signal["llm_tweet_judge_skipped"] = True
        return True, "llm_tweet_judge_skipped"

    keep = bool(result.get("keep"))
    quality_score = int(result.get("quality_score") or 0)
    signal["llm_tweet_judge"] = result
    signal["llm_tweet_quality_score"] = quality_score
    if result.get("domain"):
        signal["domain"] = str(result["domain"])
    if result.get("event"):
        signal["event"] = str(result["event"])
    if result.get("direction"):
        signal["direction"] = str(result["direction"])
    if result.get("deadline"):
        signal["deadline"] = str(result["deadline"])
    if "is_falsifiable" in result:
        signal["is_falsifiable"] = bool(result["is_falsifiable"])

    if keep and quality_score >= judge.min_score:
        judge.accepted += 1
        signal["signal_score"] = int(signal.get("signal_score") or 0) + max(0, quality_score - 5)
        return True, "llm_tweet_judge_keep"

    judge.rejected += 1
    reason = str(result.get("reason") or "llm_tweet_judge_reject")
    return False, f"llm_tweet_judge_reject:{reason[:120]}"


def is_predictive_candidate(text: str, tweet_type: str) -> Tuple[bool, str, int]:
    spam_reason = spam_filter_reason(text, tweet_type)
    if spam_reason:
        return False, spam_reason, 0
    if is_retrospective_noise(text):
        return False, "retrospective_noise", 0

    score = predictive_score(text)
    if score < 2:
        return False, "weak_prediction_language", score
    if not has_forward_signal(text):
        return False, "no_forward_signal", score
    return True, "candidate", score


def build_signal(row: Dict[str, Any], score: Optional[int] = None) -> Optional[Dict[str, Any]]:
    text = text_of(row)
    tweet = row.get("tweet") or {}
    tweet_type = str(row.get("tweet_type") or "")
    ok, _, candidate_score = is_predictive_candidate(text, tweet_type)
    if not ok:
        return None
    score = candidate_score if score is None else score

    domain = classify_domain(text)
    direction = classify_direction(text)
    deadline = extract_deadline(text, row)
    eng = engagement(tweet)
    has_deadline = deadline is not None
    falsifiable = score >= 2 and domain != "Other"
    signal_score = score + min(3, eng // 100) + (2 if has_deadline else 0) + (1 if falsifiable else 0)

    return {
        "handle": row.get("kol_username"),
        "tweet_id": tweet.get("id"),
        "tweet_url": tweet.get("url") or tweet.get("twitterUrl"),
        "created_at": row.get("created_at") or tweet.get("createdAt"),
        "tweet_type": tweet_type,
        "event": normalize_event(text),
        "direction": direction,
        "deadline": deadline,
        "confidence": None,
        "domain": domain,
        "is_falsifiable": falsifiable,
        "engagement": eng,
        "signal_score": signal_score,
        "market_match": None,
        "backtest": None,
        "text": text,
    }


def empty_kol_stats() -> Dict[str, Any]:
    return {
        "tweets_seen": 0,
        "noise_rejected": 0,
        "weak_rejected": 0,
        "signals": 0,
        "score": 0,
        "engagement": 0,
        "deadline_signals": 0,
        "falsifiable_signals": 0,
        "domains": Counter(),
        "examples": [],
    }


def rejected_sample(row: Dict[str, Any], reason: str) -> Dict[str, Any]:
    tweet = row.get("tweet") or {}
    return {
        "handle": row.get("kol_username"),
        "tweet_id": tweet.get("id"),
        "tweet_url": tweet.get("url") or tweet.get("twitterUrl"),
        "created_at": row.get("created_at") or tweet.get("createdAt"),
        "tweet_type": row.get("tweet_type"),
        "reason": reason,
        "text": text_of(row),
    }


def update_kol_stats_for_row(
    stats: Dict[str, Dict[str, Any]],
    row: Dict[str, Any],
    rejected_samples: List[Dict[str, Any]],
    rejected_limit_per_reason: int,
    rejected_reason_counts: Counter,
    tweet_judge: Optional[TweetJudge] = None,
) -> Optional[Dict[str, Any]]:
    handle = str(row.get("kol_username") or "")
    if not handle:
        return None

    data = stats[handle]
    data["tweets_seen"] += 1

    text = text_of(row)
    tweet_type = str(row.get("tweet_type") or "")
    ok, reason, score = is_predictive_candidate(text, tweet_type)
    if not ok:
        if reason not in ("weak_prediction_language", "no_forward_signal"):
            data["noise_rejected"] += 1
            if rejected_reason_counts[reason] < rejected_limit_per_reason:
                rejected_samples.append(rejected_sample(row, reason))
            rejected_reason_counts[reason] += 1
        else:
            data["weak_rejected"] += 1
        return None

    signal = build_signal(row, score=score)
    if not signal:
        return None
    keep, judge_reason = apply_tweet_judgment(signal, tweet_judge)
    if not keep:
        data["weak_rejected"] += 1
        if rejected_reason_counts[judge_reason] < rejected_limit_per_reason:
            sample = rejected_sample(row, judge_reason)
            sample["llm_tweet_judge"] = signal.get("llm_tweet_judge")
            rejected_samples.append(sample)
        rejected_reason_counts[judge_reason] += 1
        return None

    data["signals"] += 1
    data["score"] += int(signal.get("signal_score") or 0)
    data["engagement"] += int(signal.get("engagement") or 0)
    if signal.get("deadline"):
        data["deadline_signals"] += 1
    if signal.get("is_falsifiable"):
        data["falsifiable_signals"] += 1
    data["domains"][str(signal.get("domain") or "Other")] += 1
    if len(data["examples"]) < 3:
        data["examples"].append(signal)
    return signal


def proxy_verdict(total_score: float, signals_count: int, signal_density: float, falsifiability_rate: float) -> str:
    if signals_count >= 20 and total_score >= 150 and signal_density >= 0.03 and falsifiability_rate >= 0.45:
        return "high_priority_review"
    if signals_count >= 5 and total_score >= 30:
        return "candidate"
    if signals_count > 0:
        return "watchlist"
    return "no_signal"


def write_leaderboard(stats: Dict[str, Dict[str, Any]], path: str) -> None:
    rows = []
    for handle, data in stats.items():
        signals_count = data["signals"]
        top_domain = data["domains"].most_common(1)[0][0] if data["domains"] else "Other"
        tweets_seen = data["tweets_seen"]
        signal_density = signals_count / max(1, tweets_seen)
        deadline_rate = data["deadline_signals"] / max(1, signals_count)
        falsifiability_rate = data["falsifiable_signals"] / max(1, signals_count)
        domain_focus = data["domains"].most_common(1)[0][1] / max(1, signals_count) if data["domains"] else 0.0
        avg_signal_score = data["score"] / max(1, signals_count)
        avg_engagement = data["engagement"] / max(1, signals_count)
        proxy_score = (
            data["score"]
            + (signal_density * 100)
            + (falsifiability_rate * 20)
            + (deadline_rate * 10)
            + (domain_focus * 5)
            - (data["noise_rejected"] / max(1, tweets_seen) * 10)
        )
        rows.append({
            "handle": handle,
            "tweets_seen": tweets_seen,
            "signals": signals_count,
            "signal_density": round(signal_density, 5),
            "falsifiability_rate": round(falsifiability_rate, 3),
            "deadline_rate": round(deadline_rate, 3),
            "domain_focus": round(domain_focus, 3),
            "avg_signal_score": round(avg_signal_score, 3),
            "total_signal_score": data["score"],
            "avg_engagement": round(avg_engagement, 3),
            "top_domain": top_domain,
            "noise_rejected": data["noise_rejected"],
            "weak_rejected": data["weak_rejected"],
            "tweet_only_proxy_score": round(proxy_score, 3),
            "verdict": proxy_verdict(proxy_score, signals_count, signal_density, falsifiability_rate),
        })
    rows.sort(key=lambda r: (r["tweet_only_proxy_score"], r["total_signal_score"], r["signals"]), reverse=True)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "handle", "tweets_seen", "signals", "signal_density",
            "falsifiability_rate", "deadline_rate", "domain_focus",
            "avg_signal_score", "total_signal_score", "avg_engagement",
            "top_domain", "noise_rejected", "weak_rejected",
            "tweet_only_proxy_score", "verdict",
        ])
        writer.writeheader()
        writer.writerows(rows)


def write_signal_feed(signals: List[Dict[str, Any]], path: str, limit: int) -> None:
    rows = sorted(
        signals,
        key=lambda s: (parse_tweet_datetime(s.get("created_at")), int(s.get("signal_score") or 0)),
        reverse=True,
    )
    if limit > 0:
        rows = rows[:limit]

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "handle", "domain", "direction", "deadline", "signal_score",
            "engagement", "created_at", "event", "tweet_url",
        ])
        writer.writeheader()
        for signal in rows:
            writer.writerow({
                "handle": signal.get("handle"),
                "domain": signal.get("domain"),
                "direction": signal.get("direction"),
                "deadline": signal.get("deadline"),
                "signal_score": signal.get("signal_score"),
                "engagement": signal.get("engagement"),
                "created_at": signal.get("created_at"),
                "event": signal.get("event"),
                "tweet_url": signal.get("tweet_url"),
            })


def write_labeling_sample(signals: List[Dict[str, Any]], path: str, limit: int) -> None:
    sample = sorted(
        signals,
        key=lambda s: (int(s.get("signal_score") or 0), int(s.get("engagement") or 0)),
        reverse=True,
    )
    if limit > 0:
        sample = sample[:limit]

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for signal in sample:
            row = {
                "tweet_id": signal.get("tweet_id"),
                "handle": signal.get("handle"),
                "tweet_url": signal.get("tweet_url"),
                "text": signal.get("text"),
                "model_event": signal.get("event"),
                "model_direction": signal.get("direction"),
                "model_deadline": signal.get("deadline"),
                "model_domain": signal.get("domain"),
                "model_signal_score": signal.get("signal_score"),
                "label_is_predictive": None,
                "label_event": None,
                "label_direction": None,
                "label_deadline": None,
                "label_domain": None,
                "label_useful": None,
                "notes": None,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_rejected_samples(samples: List[Dict[str, Any]], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for sample in samples:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", required=True, help="Input tweet JSONL. Repeatable.")
    parser.add_argument("--signals_out", default="reports/kol_intelligence/signals.jsonl")
    parser.add_argument("--leaderboard_out", default="reports/kol_intelligence/leaderboard.csv")
    parser.add_argument("--signal_feed_out", default="reports/kol_intelligence/signal_feed.csv")
    parser.add_argument("--labeling_out", default="reports/kol_intelligence/labeling_sample.jsonl")
    parser.add_argument("--rejected_out", default="reports/kol_intelligence/rejected_spam_samples.jsonl")
    parser.add_argument("--signal_feed_limit", type=int, default=500)
    parser.add_argument("--labeling_limit", type=int, default=500)
    parser.add_argument("--rejected_limit_per_reason", type=int, default=50)
    parser.add_argument("--max_rows", type=int, default=0, help="0 means all rows.")
    parser.add_argument("--tweet_judge", choices=["heuristic", "llm"], default="heuristic")
    parser.add_argument("--tweet_judge_domain", default="any")
    parser.add_argument("--tweet_judge_min_score", type=int, default=6)
    parser.add_argument("--tweet_judge_provider", choices=["deepseek", "qwen", "openai"], default="deepseek")
    parser.add_argument("--tweet_judge_model", default="")
    parser.add_argument("--tweet_judge_api_key")
    parser.add_argument("--tweet_judge_base_url")
    parser.add_argument("--tweet_judge_timeout", type=int, default=60)
    parser.add_argument("--tweet_judge_retries", type=int, default=2)
    parser.add_argument("--tweet_judge_max_calls", type=int, default=0, help="0 means no cap.")
    args = parser.parse_args()

    stats: Dict[str, Dict[str, Any]] = defaultdict(empty_kol_stats)
    signals: List[Dict[str, Any]] = []
    rejected_samples: List[Dict[str, Any]] = []
    rejected_reason_counts: Counter = Counter()
    tweet_judge = TweetJudge(
        enabled=args.tweet_judge == "llm",
        domain=args.tweet_judge_domain,
        min_score=args.tweet_judge_min_score,
        provider=args.tweet_judge_provider,
        model=args.tweet_judge_model,
        api_key=args.tweet_judge_api_key,
        base_url=args.tweet_judge_base_url,
        timeout=args.tweet_judge_timeout,
        retries=args.tweet_judge_retries,
        max_calls=args.tweet_judge_max_calls,
    )
    for row in iter_jsonl(args.input, args.max_rows):
        signal = update_kol_stats_for_row(
            stats,
            row,
            rejected_samples,
            args.rejected_limit_per_reason,
            rejected_reason_counts,
            tweet_judge,
        )
        if signal:
            signals.append(signal)

    os.makedirs(os.path.dirname(args.signals_out), exist_ok=True)
    with open(args.signals_out, "w", encoding="utf-8") as f:
        for signal in signals:
            f.write(json.dumps(signal, ensure_ascii=False) + "\n")

    write_leaderboard(stats, args.leaderboard_out)
    write_signal_feed(signals, args.signal_feed_out, args.signal_feed_limit)
    write_labeling_sample(signals, args.labeling_out, args.labeling_limit)
    write_rejected_samples(rejected_samples, args.rejected_out)
    print(f"signals={len(signals)}")
    print(f"signals_out={args.signals_out}")
    print(f"leaderboard_out={args.leaderboard_out}")
    print(f"signal_feed_out={args.signal_feed_out}")
    print(f"labeling_out={args.labeling_out}")
    print(f"rejected_out={args.rejected_out}")
    print(f"rejected_samples={len(rejected_samples)}")
    if tweet_judge.enabled:
        print(
            "tweet_judge="
            f"calls={tweet_judge.calls} accepted={tweet_judge.accepted} "
            f"rejected={tweet_judge.rejected} skipped={tweet_judge.skipped} failed={tweet_judge.failed}"
        )


if __name__ == "__main__":
    main()
