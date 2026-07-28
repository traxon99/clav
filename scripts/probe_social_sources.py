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
from clav.integrations.discovery import ApeWisdomSource, StockTwitsTrendingSource
from clav.integrations.news import EdgarNewsSource, RSSNewsSource
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

    # The news feeds are XML/Atom, the social ones JSON -- count what each has.
    try:
        if "rss" in label or "edgar" in label:
            n = resp.text.count("<item") + resp.text.count("<entry")
        elif "reddit" in label:
            n = len(resp.json()["data"]["children"])
        else:
            payload = resp.json()
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
        # Discovery-only (mention counts, no post text) -- but if this is blocked
        # too, StockTwits trending is the sole thing feeding the funnel.
        raw_probe("apewisdom", "https://apewisdom.io/api/v1.0/filter/all-stocks/page/1")
        report_discovery_sources()

        print("\n news endpoints (same fail-open design, same blind spot)")
        raw_probe(
            "yahoo rss",
            f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US",
        )
        raw_probe(
            "sec edgar",
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&ticker={symbol}&type=&dateb=&owner=include&count=40&output=atom",
        )
        for name, source in [
            ("RSSNewsSource", RSSNewsSource(clock=clock)),
            ("EdgarNewsSource", EdgarNewsSource(clock=clock)),
        ]:
            got = source.fetch(symbol, since)
            print(f"  {name:32} {len(got):>4} items")

        print("\n through the real social adapters (fail-open, as production runs them)")
        items = []
        for name, source in [
            ("RedditSource", RedditSource(clock=clock, subreddits=SUBREDDITS)),
            ("StockTwitsSource", StockTwitsSource(clock=clock)),
        ]:
            got = source.fetch(symbol, since)
            kept = [i for i in got if passes_stage1(i, PARAMS)]
            print(f"  {name:32} {len(got):>4} fetched -> {len(kept):>4} pass Stage-1")
            items.extend(got)

        report_label_coverage([i for i in items if passes_stage1(i, PARAMS)])

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


_discovery_reported = False


def report_discovery_sources() -> None:
    """Run the discovery adapters for real, once per probe.

    Reachability is only half the question. These adapters are fail-open, so a
    payload whose field names or types differ from what the parser expects
    yields ``[]`` -- indistinguishable from "blocked" and from "nothing
    trending". ``ApeWisdomSource`` in particular was written against the
    documented response shape, never against a live response, so a silent
    parse mismatch is a real possibility until this prints candidates.
    """
    global _discovery_reported
    if _discovery_reported:
        return
    _discovery_reported = True

    print("\n discovery adapters (not per-symbol -- market-wide, run once)")
    for name, source in [
        ("StockTwitsTrendingSource", StockTwitsTrendingSource()),
        ("ApeWisdomSource", ApeWisdomSource()),
    ]:
        got = source.fetch()
        print(f"  {name:32} {len(got):>4} candidates")
        if not got:
            print("      ^ empty: blocked, nothing trending, OR a payload-shape")
            print("        mismatch the fail-open path swallowed -- check the HTTP")
            print("        line above to tell which.")
            continue
        for c in sorted(got, key=lambda c: c.score, reverse=True)[:3]:
            flag = " [spike]" if c.anomaly_flag else ""
            print(f"      {c.symbol:<6} score={c.score:.3f} mentions={c.mention_volume}{flag}")


def report_label_coverage(qualifying: list) -> None:
    """How much work is the graded scorer actually doing on this network?

    ``classify_sentiment`` lets an explicit source label win, and StockTwits ships
    Bullish/Bearish -- but only on messages whose author bothered to tag one. Every
    untagged post falls through to the scorer. So this ratio is what decides how
    much of the bull/bear tally the scorer owns.

    Note ``avg_sentiment`` is unaffected either way: it is computed from post text
    for every qualifying post, labelled or not.
    """
    if not qualifying:
        return
    labelled = sum(1 for i in qualifying if i.sentiment is not None)
    unlabelled = len(qualifying) - labelled
    pct = unlabelled / len(qualifying) * 100
    print(
        f"\n  label coverage: {labelled} carry an explicit source label, "
        f"{unlabelled} do not ({pct:.0f}% scored by the lexicon)"
    )


def bench_scorer() -> None:
    """Measured on the real Pi 4 (2026-07-28): 1.4 MB RSS, 179 us/post -- ARM is
    ~3.5x slower per post than x86 and still trivial (a 50-post digest costs ~9 ms).
    Re-measured each run so a regression against the 150-350 MB clav-core budget
    (docs/09-deployment.md §2) shows up immediately."""
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
    print("scorer cost on THIS machine (Pi 4 reference: 1.4 MB, 179 us/post)")
    print(f"  peak RSS delta from loading the lexicon : {(after - before) / 1024:.1f} MB")
    print(f"  per-post latency                        : {per_post_us:.0f} us")
    print(f"  a 50-post digest therefore costs        : {per_post_us * 50 / 1000:.1f} ms")
    print()


if __name__ == "__main__":
    raise SystemExit(main([s.upper() for s in sys.argv[1:]] or ["NVDA"]))
