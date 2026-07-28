"""ApeWisdom discovery source: keyless Reddit mention counts."""

from __future__ import annotations

import json

from clav.integrations.discovery import ApeWisdomSource


class _Fetcher:
    def __init__(self, body: str | Exception) -> None:
        self._body = body
        self.calls: list[str] = []

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> str:
        self.calls.append(url)
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _payload(rows: list[tuple[str, int, int]]) -> str:
    return json.dumps(
        {
            "count": len(rows),
            "pages": 1,
            "current_page": 1,
            "results": [
                # The API returns counts as strings, not ints.
                {"rank": i + 1, "ticker": t, "name": t, "mentions": str(m),
                 "upvotes": "1", "rank_24h_ago": "1", "mentions_24h_ago": str(p)}
                for i, (t, m, p) in enumerate(rows)
            ],
        }
    )


def test_accelerating_smallcap_outranks_permanently_loud_megacap() -> None:
    """The whole reason this source exists: StockTwits trending ranks on
    absolute popularity and surfaces the same mega-caps forever."""
    source = ApeWisdomSource(
        fetcher=_Fetcher(_payload([("AAPL", 400, 395), ("SOFI", 300, 20)]))
    )
    by_symbol = {c.symbol: c for c in source.fetch()}
    assert by_symbol["SOFI"].score > by_symbol["AAPL"].score


def test_chatter_spike_sets_the_anomaly_flag() -> None:
    source = ApeWisdomSource(fetcher=_Fetcher(_payload([("GME", 300, 20), ("MSFT", 100, 98)])))
    by_symbol = {c.symbol: c for c in source.fetch()}
    assert by_symbol["GME"].anomaly_flag is True
    assert by_symbol["MSFT"].anomaly_flag is False


def test_tiny_counts_do_not_count_as_a_spike() -> None:
    """2 -> 8 mentions is a 4x multiple and still just noise."""
    source = ApeWisdomSource(fetcher=_Fetcher(_payload([("ABCD", 8, 2)])))
    assert source.fetch()[0].anomaly_flag is False


def test_scores_are_bounded_and_counts_parsed_from_strings() -> None:
    source = ApeWisdomSource(fetcher=_Fetcher(_payload([("A", 50, 0), ("B", 10, 10)])))
    for candidate in source.fetch():
        assert 0.0 <= candidate.score <= 1.0
        assert isinstance(candidate.mention_volume, int)


def test_dead_endpoint_fails_open() -> None:
    source = ApeWisdomSource(fetcher=_Fetcher(RuntimeError("403 Forbidden")))
    assert source.fetch() == []


def test_garbage_payload_fails_open() -> None:
    assert ApeWisdomSource(fetcher=_Fetcher("not json")).fetch() == []
    assert ApeWisdomSource(fetcher=_Fetcher('{"results": []}')).fetch() == []


def test_filter_is_applied_to_the_url() -> None:
    fetcher = _Fetcher(_payload([("X", 1, 1)]))
    ApeWisdomSource(subreddit_filter="wallstreetbets", fetcher=fetcher).fetch()
    assert "filter/wallstreetbets/" in fetcher.calls[0]
