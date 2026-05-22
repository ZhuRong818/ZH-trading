#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import requests
from dateutil import parser as dtparser
from dateutil.relativedelta import relativedelta


BASE_URL = "https://api.twitterapi.io"
SEARCH_ENDPOINT = "/twitter/tweet/advanced_search"
DEFAULT_TIMEOUT = 120


class QpsLimiter:
    def __init__(self, qps: float):
        if qps <= 0:
            raise ValueError("qps must be > 0")
        self.interval = 1.0 / qps
        self.lock = threading.Lock()
        self.next_time = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.perf_counter()
            if now < self.next_time:
                time.sleep(self.next_time - now)
            jitter = random.uniform(0.0, self.interval * 0.15)
            self.next_time = max(self.next_time, time.perf_counter()) + self.interval + jitter


class SkipUser401(Exception):
    pass


_thread_local = threading.local()


def get_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        _thread_local.session = session
    return session


def read_usernames(path: str) -> List[str]:
    names: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            u = line.strip().lstrip("@")
            if u and not u.startswith("#"):
                names.append(u)

    seen = set()
    out: List[str] = []
    for u in names:
        k = u.lower()
        if k not in seen:
            seen.add(k)
            out.append(u)
    return out


def parse_created_at(tweet: Dict[str, Any]) -> Optional[dt.datetime]:
    s = tweet.get("createdAt") or tweet.get("created_at")
    if not s:
        return None
    try:
        return dtparser.parse(s).astimezone(dt.timezone.utc)
    except Exception:
        return None


def classify_tweet(tweet: Dict[str, Any]) -> str:
    if isinstance(tweet.get("retweeted_tweet"), dict):
        return "retweet"
    if isinstance(tweet.get("quoted_tweet"), dict):
        return "quote"
    if tweet.get("isReply") is True:
        return "reply"
    return "original"


def extract_media_from_one_tweet(tweet: Dict[str, Any]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    media = (tweet.get("extendedEntities") or tweet.get("extended_entities") or {}).get("media") or []

    for m in media:
        mtype = (m.get("type") or "").lower()

        if mtype == "photo":
            url = m.get("media_url_https") or m.get("media_url")
            if url:
                out.append({"kind": "image", "url": url})
            continue

        if mtype in ("video", "animated_gif"):
            vinfo = m.get("video_info") or {}
            variants = vinfo.get("variants") or []
            mp4s = [v.get("url") for v in variants if v.get("content_type") == "video/mp4" and v.get("url")]
            m3u8 = [v.get("url") for v in variants if "mpegURL" in (v.get("content_type") or "") and v.get("url")]

            for url in mp4s:
                out.append({"kind": "video" if mtype == "video" else "gif", "url": url})
            for url in m3u8:
                out.append({"kind": "hls", "url": url})

            thumb = m.get("media_url_https") or m.get("media_url")
            if thumb:
                out.append({"kind": "thumbnail", "url": thumb})

    return out


def extract_all_media(tweet: Dict[str, Any]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    out.extend(extract_media_from_one_tweet(tweet))

    qt = tweet.get("quoted_tweet")
    if isinstance(qt, dict):
        out.extend(extract_media_from_one_tweet(qt))

    rt = tweet.get("retweeted_tweet")
    if isinstance(rt, dict):
        out.extend(extract_media_from_one_tweet(rt))

    seen = set()
    deduped: List[Dict[str, str]] = []
    for item in out:
        key = (item.get("kind"), item.get("url"))
        if item.get("url") and key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def strip_extended_entities_fields(tweet: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(tweet, dict):
        return tweet

    cleaned = dict(tweet)
    cleaned.pop("extendedEntities", None)
    cleaned.pop("extended_entities", None)

    qt = cleaned.get("quoted_tweet")
    if isinstance(qt, dict):
        cleaned["quoted_tweet"] = strip_extended_entities_fields(qt)

    rt = cleaned.get("retweeted_tweet")
    if isinstance(rt, dict):
        cleaned["retweeted_tweet"] = strip_extended_entities_fields(rt)

    return cleaned


def safe_get_tweets(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    v = resp.get("tweets")
    if isinstance(v, list):
        return v

    data = resp.get("data")
    if isinstance(data, dict):
        for k in ("tweets", "items", "list"):
            vv = data.get(k)
            if isinstance(vv, list):
                return vv
    return []


def safe_get_pageinfo(resp: Dict[str, Any]) -> Tuple[bool, str]:
    if "has_next_page" in resp or "next_cursor" in resp:
        return bool(resp.get("has_next_page")), (resp.get("next_cursor") or "")

    data = resp.get("data")
    if isinstance(data, dict) and ("has_next_page" in data or "next_cursor" in data):
        return bool(data.get("has_next_page")), (data.get("next_cursor") or "")

    return False, ""


def request_json(
    api_key: str,
    endpoint: str,
    params: Dict[str, Any],
    limiter: QpsLimiter,
    timeout: int,
    max_retries: int = 5,
) -> Dict[str, Any]:
    url = f"{BASE_URL}{endpoint}"
    headers = {"X-API-Key": api_key}

    for attempt in range(1, max_retries + 1):
        limiter.wait()
        try:
            r = get_session().get(url, headers=headers, params=params, timeout=timeout)

            if r.status_code == 401:
                body = (r.text or "")[:400]
                if attempt < 2:
                    time.sleep(1.0 + random.random())
                    continue
                raise SkipUser401(f"HTTP 401: {body}")

            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                backoff = float(retry_after) if retry_after else (2 ** min(attempt, 6)) + random.random()
                time.sleep(backoff)
                continue

            if 500 <= r.status_code < 600:
                if attempt < max_retries:
                    backoff = (2 ** min(attempt, 6)) + random.random()
                    time.sleep(backoff)
                    continue

            if r.status_code != 200:
                body = (r.text or "")[:400]
                raise RuntimeError(f"HTTP {r.status_code}: {body}")

            return r.json()

        except (
            requests.exceptions.ReadTimeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
        ) as e:
            if attempt >= max_retries:
                raise RuntimeError(f"Network error after retries: {e}") from e
            backoff = (2 ** min(attempt, 6)) + random.random()
            time.sleep(backoff)

    raise RuntimeError("request_json failed unexpectedly")


def fmt_utc_for_query(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%d")


def day_slices(start_utc: dt.datetime, end_utc: dt.datetime) -> List[Tuple[dt.datetime, dt.datetime]]:
    slices: List[Tuple[dt.datetime, dt.datetime]] = []
    cur = start_utc.replace(microsecond=0)
    while cur < end_utc:
        nxt = min(cur + dt.timedelta(days=1), end_utc)
        slices.append((cur, nxt))
        cur = nxt
    return slices


def week_slices(start_utc: dt.datetime, end_utc: dt.datetime) -> List[Tuple[dt.datetime, dt.datetime]]:
    slices: List[Tuple[dt.datetime, dt.datetime]] = []
    cur = start_utc.replace(microsecond=0)
    while cur < end_utc:
        nxt = min(cur + dt.timedelta(days=7), end_utc)
        slices.append((cur, nxt))
        cur = nxt
    return slices


def month_slices(start_utc: dt.datetime, end_utc: dt.datetime) -> List[Tuple[dt.datetime, dt.datetime]]:
    slices: List[Tuple[dt.datetime, dt.datetime]] = []
    cur = start_utc.replace(microsecond=0)
    while cur < end_utc:
        nxt = (cur + relativedelta(months=1)).replace(microsecond=0)
        if nxt > end_utc:
            nxt = end_utc
        slices.append((cur, nxt))
        cur = nxt
    return slices


def build_slices(
    start_utc: dt.datetime,
    end_utc: dt.datetime,
    slice_mode: str,
) -> List[Tuple[dt.datetime, dt.datetime]]:
    if slice_mode == "day":
        return day_slices(start_utc, end_utc)
    if slice_mode == "week":
        return week_slices(start_utc, end_utc)
    if slice_mode == "month":
        return month_slices(start_utc, end_utc)
    raise ValueError(f"Unsupported slice mode: {slice_mode}")


def finer_slice_mode(slice_mode: str) -> Optional[str]:
    if slice_mode == "month":
        return "week"
    return None


def search_fetch_page(
    api_key: str,
    query: str,
    cursor: str,
    limiter: QpsLimiter,
    timeout: int,
) -> Dict[str, Any]:
    params = {
        "query": query,
        "queryType": "Latest",
        "cursor": cursor or "",
    }
    return request_json(api_key, SEARCH_ENDPOINT, params, limiter, timeout)


def fetch_slice_recursive(
    api_key: str,
    username: str,
    limiter: QpsLimiter,
    timeout: int,
    start_utc: dt.datetime,
    end_utc: dt.datetime,
    exclude_replies: bool,
    retweets_only: bool,
    exclude_retweets: bool,
    strip_extended_entities: bool,
    max_pages_per_slice: int,
    slice_mode: str,
    max_tweets_per_user: Optional[int],
    seen_ids: set[str],
    all_tweets: List[Dict[str, Any]],
    debug: bool,
) -> None:
    slices = list(reversed(build_slices(start_utc, end_utc, slice_mode)))
    for since_dt, until_dt in slices:
        if max_tweets_per_user is not None and len(all_tweets) >= max_tweets_per_user:
            return
        since_q = fmt_utc_for_query(since_dt)
        until_q = fmt_utc_for_query(until_dt)
        query = f"from:{username} since:{since_q} until:{until_q}"

        cursor = ""
        seen_cursors = set()
        seen_page_signatures = set()
        empty_streak = 0

        for page in range(1, max_pages_per_slice + 1):
            if max_tweets_per_user is not None and len(all_tweets) >= max_tweets_per_user:
                return
            resp = search_fetch_page(api_key, query, cursor, limiter, timeout)
            tweets = safe_get_tweets(resp)

            tweet_ids = tuple(str(t.get("id", "")) for t in tweets if t.get("id"))
            newest_created = ""
            oldest_created = ""
            if tweets:
                created_vals = [str(t.get("createdAt") or t.get("created_at") or "") for t in tweets]
                newest_created = created_vals[0]
                oldest_created = created_vals[-1]
            page_signature = (tweet_ids, newest_created, oldest_created)

            if not tweets:
                empty_streak += 1
                if empty_streak >= 2:
                    break
            else:
                empty_streak = 0
                if page_signature in seen_page_signatures:
                    finer = finer_slice_mode(slice_mode)
                    if finer and page > 1:
                        if debug:
                            print(
                                f"[INFO] @{username} {since_q}~{until_q}: repeated page signature -> "
                                f"refine {slice_mode} to {finer}"
                            )
                        fetch_slice_recursive(
                            api_key,
                            username,
                            limiter,
                            timeout,
                            since_dt,
                            until_dt,
                            exclude_replies,
                            retweets_only,
                            exclude_retweets,
                            strip_extended_entities,
                            max_pages_per_slice,
                            finer,
                            max_tweets_per_user,
                            seen_ids,
                            all_tweets,
                            debug,
                        )
                        break
                    if debug:
                        print(f"[INFO] @{username} {since_q}~{until_q}: repeated page signature -> stop slice")
                    break
                seen_page_signatures.add(page_signature)

                new_raw = 0
                kept_count = 0
                for t in tweets:
                    tid = str(t.get("id", ""))
                    if not tid or tid in seen_ids:
                        continue
                    seen_ids.add(tid)
                    new_raw += 1

                    ttype = classify_tweet(t)
                    if exclude_replies and ttype == "reply":
                        continue
                    if exclude_retweets and ttype == "retweet":
                        continue
                    if retweets_only and ttype != "retweet":
                        continue

                    cleaned = strip_extended_entities_fields(t) if strip_extended_entities else t
                    all_tweets.append(cleaned)
                    kept_count += 1
                    if max_tweets_per_user is not None and len(all_tweets) >= max_tweets_per_user:
                        break

                if debug:
                    print(
                        f"[DEBUG] @{username} {slice_mode} {since_q}~{until_q} page={page} "
                        f"tweets={len(tweets)} new_raw={new_raw} kept={kept_count}"
                    )

                if new_raw == 0:
                    finer = finer_slice_mode(slice_mode)
                    if finer and page > 1:
                        if debug:
                            print(
                                f"[INFO] @{username} {since_q}~{until_q}: zero-new page -> "
                                f"refine {slice_mode} to {finer}"
                            )
                        fetch_slice_recursive(
                            api_key,
                            username,
                            limiter,
                            timeout,
                            since_dt,
                            until_dt,
                            exclude_replies,
                            retweets_only,
                            exclude_retweets,
                            strip_extended_entities,
                            max_pages_per_slice,
                            finer,
                            max_tweets_per_user,
                            seen_ids,
                            all_tweets,
                            debug,
                        )
                        break
                    if debug:
                        print(f"[INFO] @{username} {since_q}~{until_q}: zero-new page -> stop slice")
                    break
            if not tweets and debug:
                print(f"[DEBUG] @{username} {slice_mode} {since_q}~{until_q} page={page} tweets=0")

            has_next, next_cursor = safe_get_pageinfo(resp)
            if not has_next or not next_cursor or next_cursor == cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor


def fetch_user_complete(
    api_key: str,
    username: str,
    limiter: QpsLimiter,
    timeout: int,
    start_utc: dt.datetime,
    end_utc: dt.datetime,
    exclude_replies: bool,
    retweets_only: bool,
    exclude_retweets: bool,
    strip_extended_entities: bool,
    max_pages_per_slice: int,
    slice_mode: str,
    max_tweets_per_user: Optional[int],
    debug: bool,
) -> List[Dict[str, Any]]:
    seen_ids: set[str] = set()
    all_tweets: List[Dict[str, Any]] = []
    fetch_slice_recursive(
        api_key,
        username,
        limiter,
        timeout,
        start_utc,
        end_utc,
        exclude_replies,
        retweets_only,
        exclude_retweets,
        strip_extended_entities,
        max_pages_per_slice,
        slice_mode,
        max_tweets_per_user,
        seen_ids,
        all_tweets,
        debug,
    )
    return all_tweets


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api_key", default=os.getenv("TWITTERAPI_IO_KEY"))
    ap.add_argument("--usernames", required=True, help="Path to username list file (one per line).")
    ap.add_argument("--out", default="reports/kol_intelligence/complete_fetch.jsonl")
    ap.add_argument("--failed_out", default="reports/kol_intelligence/complete_fetch_failed.txt")
    ap.add_argument("--start", default="2021-01-01", help="Inclusive start date YYYY-MM-DD")
    ap.add_argument("--until", default="2026-02-01", help="Inclusive end date YYYY-MM-DD")
    ap.add_argument(
        "--slice",
        choices=["day", "week", "month"],
        default="day",
        help="Time slice size used for search queries.",
    )
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--qps", type=float, default=6.0)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--exclude_replies", action="store_true")
    ap.add_argument("--retweets_only", action="store_true")
    ap.add_argument("--exclude_retweets", action="store_true")
    ap.add_argument("--strip_extended_entities", action="store_true")
    ap.add_argument(
        "--max_pages_per_slice",
        "--max_pages_per_day",
        dest="max_pages_per_slice",
        type=int,
        default=20,
        help="Max pages fetched for each time slice.",
    )
    ap.add_argument(
        "--max_tweets_per_user",
        type=int,
        default=0,
        help="Stop after this many kept tweets per username. 0 means no cap.",
    )
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    args.api_key = (args.api_key or "").strip()
    if not args.api_key:
        raise SystemExit("Missing API key. Provide --api_key or set TWITTERAPI_IO_KEY.")

    usernames = read_usernames(args.usernames)
    if not usernames:
        raise SystemExit("No usernames found in file.")
    max_tweets_per_user = args.max_tweets_per_user if args.max_tweets_per_user > 0 else None

    start_utc = dt.datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    end_utc = (
        dt.datetime.strptime(args.until, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
        + dt.timedelta(days=1)
    )

    limiter = QpsLimiter(args.qps)
    fetched_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    failed: List[str] = []
    total_written = 0

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    os.makedirs(os.path.dirname(args.failed_out), exist_ok=True)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        future_to_user = {
            ex.submit(
                fetch_user_complete,
                args.api_key,
                u,
                limiter,
                args.timeout,
                start_utc,
                end_utc,
                args.exclude_replies,
                args.retweets_only,
                args.exclude_retweets,
                args.strip_extended_entities,
                args.max_pages_per_slice,
                args.slice,
                max_tweets_per_user,
                args.debug,
            ): u
            for u in usernames
        }

        with open(args.out, "w", encoding="utf-8") as f_out:
            done_count = 0
            n = len(usernames)
            for fut in as_completed(future_to_user):
                u = future_to_user[fut]
                done_count += 1
                try:
                    tweets = fut.result()
                    for t in tweets:
                        line = {
                            "kol_username": u,
                            "fetched_at_utc": fetched_at,
                            "tweet_type": classify_tweet(t),
                            "created_at": t.get("createdAt") or t.get("created_at"),
                            "media": extract_all_media(t),
                            "tweet": t,
                        }
                        f_out.write(json.dumps(line, ensure_ascii=False) + "\n")
                    total_written += len(tweets)
                    print(f"[{done_count}/{n}] @{u}: wrote {len(tweets)} tweets")
                    f_out.flush()
                except SkipUser401 as e:
                    failed.append(u)
                    print(f"[{done_count}/{n}] @{u}: SKIP (401) {e}")
                except Exception as e:
                    failed.append(u)
                    print(f"[{done_count}/{n}] @{u}: ERROR {e}")

    if failed:
        seen = set()
        dedup = []
        for u in failed:
            if u.lower() not in seen:
                seen.add(u.lower())
                dedup.append(u)
        with open(args.failed_out, "w", encoding="utf-8") as f:
            for u in dedup:
                f.write(u + "\n")
        print(f"[WARN] Failed users: {len(dedup)} written to {args.failed_out}")

    print(f"Done. Wrote {total_written} tweets to {args.out}")
    print("Note: completeness still depends on TwitterAPI.io archive search behavior.")


if __name__ == "__main__":
    main()
