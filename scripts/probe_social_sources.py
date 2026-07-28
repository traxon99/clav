#!/usr/bin/env python
"""Answer "are the social sources actually returning anything?" from the machine
that runs CLAV.

This exists because the adapters are deliberately fail-open: ``RedditSource.fetch``
and ``StockTwitsSource.fetch`` swallow every exception and return ``[]`` (see
``clav/integrations/social/``). That is correct for the scan cycle -- a dead source
must never abort a cycle -- but it means a 403-blocked Reddit and a genuinely quiet
ticker produce the identical empty digest. The only difference is a log line.

So this probe deliberately does NOT use the fail-open path for the reachability
question. It first calls the raw HTTP endpoint and reports the real status code,
then runs the actual adapter + the real Stage-1 pipeline so you can see how many
posts survive filtering and what Gemini would have received.

It also re-measures the sentiment scorer's RAM and per-post latency, because the
numbers in docs/09-deployment.md were taken on an x86 dev box and the 2 GB Pi is
the machine the budget actually has to hold on.

Run it on the Pi (that is the only network whose answer matters):

    uv run python scripts/probe_social_sources.py            # defaults to NVDA
    uv run python scripts/probe_social_sources.py TSLA AAPL

Exit code is 0 if at least one source returned usable posts, 1 otherwise -- so it
can be dropped into a cron/health check.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta

import httpx

from clav.clock import SystemClock
from clav.domain.social import SocialFilterParams, build_digest, passes_stage1
from clav.integrations.social import RedditSource, StockTwitsSource

try:
    from clav.integrations.sentiment import LexiconScorer

    SCORE = LexiconScorer().score
except Exception as exc:  # optional dep absent -> word-tally fallback
    print(f"note: lexicon scorer unavailable ({exc}); using word-tally fallback")
    SCORE = None

UA = "clav/0.1 (personal paper-trading research)"
SUBREDDITS = ("wallstreetbets", "stocks", "investing")
PARAMS = SocialFilterParams()


def raw_probe(label: str, url: str) -> tuple[bool, int | None]:
    """Real status code, no fail-open. Returns (ok, item_count)."""
    try:
        resp = httpx.get(url, headers={"User-Agent": UA}, timeout=20, follow_redirects=True)
    except Exception as exc:
        print(f"  {label:32} NETWORK  {type(exc).__name__}: {str(exc)[:70]}")
        return False, None

    if resp.status_code != 200:
        hint = ""
        if resp.status_code in (403, 429):
            hint = "  <- IP/UA blocked or rate-limited, NOT an empty result"
        print(f"  {label:32} HTTP {resp.status_code}{hint}")
        return False, None

    try:
        payload = resp.json()
        if "reddit" in label:
            n = len(payload["data"]["children"])
        else:
            n = len(payload.get("messages", payload.get("symbols", [])))
    except Exception as exc:
        print(f"  {label:32} HTTP 200 but unparseable: {type(exc).__name__} {str(exc)[:50]}")
        return False, None

    print(f"  {label:32} HTTP 200  {len(resp.content):>7} bytes  {n} raw items")
    return True, n


def main(symbols: list[str]) -> int:
    clock = SystemClock()
    now = datetime.now(UTC)
    since = now - timedelta(hours=72)
    any_usable = False

    print(f"probe at {now.isoformat(timespec='seconds')}  window=72h\n")

    for symbol in symbols:
        print(f"=== {symbol} " + "=" * (60 - len(symbol)))

        print(" raw endpoint reachability")
        for sr in SUBREDDITS:
            raw_probe(
                f"reddit r/{sr}",
                f"https://www.reddit.com/r/{sr}/search.json"
                f"?q=%24{symbol}&restrict_sr=1&sort=new&limit=50",
            )
        raw_probe(
            "stocktwits stream",
            f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json",
        )
        raw_probe(
            "stocktwits trending",
            "https://api.stocktwits.com/api/2/trending/symbols.json",
        )

        print("\n through the real adapters (fail-open, as production runs them)")
        items = []
        for name, source in [
            ("RedditSource", RedditSource(clock=clock, subreddits=SUBREDDITS)),
            ("StockTwitsSource", StockTwitsSource(clock=clock)),
        ]:
            got = source.fetch(symbol, since)
            kept = [i for i in got if passes_stage1(i, PARAMS)]
            print(f"  {name:32} {len(got):>4} fetched -> {len(kept):>4} pass Stage-1")
            items.extend(got)

        digest = build_digest(
            symbol, items, baseline_volume=float(len(items) or 1),
            params=PARAMS, now=now, score_text=SCORE,
        )
        avg = "None" if digest.avg_sentiment is None else f"{digest.avg_sentiment:+.3f}"
        print(
            f"\n digest -> qualifying={digest.qualifying_post_count} "
            f"bull={digest.bull_count} bear={digest.bear_count} "
            f"ratio={digest.bull_bear_ratio:.2f} avg_sentiment={avg} "
            f"anomaly={digest.anomaly_flag}"
        )
        if digest.top_posts:
            any_usable = True
            print(" top post:", json.dumps(digest.top_posts[0].text[:100]))
        else:
            print("  (empty digest -- Gemini would get technical-only for this symbol)")
        print()

    bench_scorer()

    if not any_usable:
        print("RESULT: no source produced a usable post. Check the HTTP codes above --")
        print("        403/429 means blocked, not quiet.")
        return 1
    print("RESULT: at least one source is live and producing posts.")
    return 0


def bench_scorer() -> None:
    """The scorer's cost was measured on x86 during development (4.3 MB RSS,
    ~50 us/post). Re-measure on the real device: that budget claim is what
    justifies the lexicon scorer being on by default (docs/09-deployment.md §2)."""
    if SCORE is None:
        return
    import resource
    import time

    from clav.integrations.sentiment import LexiconScorer

    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    scorer = LexiconScorer()
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    sample = "NVDA absolutely beat earnings, this thing is ripping 🚀 not selling"
    t = time.perf_counter()
    for _ in range(500):
        scorer.score(sample)
    per_post_us = (time.perf_counter() - t) / 500 * 1e6

    print("=" * 64)
    print("scorer cost on THIS machine (x86 dev reference: 4.3 MB, ~50 us/post)")
    print(f"  peak RSS delta from loading the lexicon : {(after - before) / 1024:.1f} MB")
    print(f"  per-post latency                        : {per_post_us:.0f} us")
    print(f"  a 50-post digest therefore costs        : {per_post_us * 50 / 1000:.1f} ms")
    print()


if __name__ == "__main__":
    raise SystemExit(main([s.upper() for s in sys.argv[1:]] or ["NVDA"]))
