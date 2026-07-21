"""Story 3.6 — AnalystGateway: fetch -> dedup/store -> digest -> signal + provenance."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from clav.clock import FakeClock
from clav.common.cache import TtlCache
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import Engagement, NewsItem, SocialDigest, SocialItem
from clav.domain.social import SocialFilterParams
from clav.integrations.llm import AnalysisCapture, GeminiAnalyst, LLMResult
from clav.interfaces.analyst import AnalystSignal
from clav.services.analyst_gateway import AnalystGateway

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


class FakeNewsSource:
    def __init__(self, items: list[NewsItem]) -> None:
        self._items = items
        self.calls = 0

    def fetch(self, symbol: str, since: datetime) -> list[NewsItem]:
        self.calls += 1
        return list(self._items)


class FakeSocialSource:
    def __init__(self, items: list[SocialItem]) -> None:
        self._items = items
        self.calls = 0

    def fetch(self, symbol: str, since: datetime) -> list[SocialItem]:
        self.calls += 1
        return list(self._items)


class RecordingAnalyst:
    def __init__(self, signal: AnalystSignal) -> None:
        self._signal = signal
        self.seen_news: list[NewsItem] = []
        self.seen_digest: SocialDigest | None = None

    def analyze(self, symbol, news, social_digest, context) -> AnalystSignal:
        self.seen_news = news
        self.seen_digest = social_digest
        return self._signal


def _news(headline: str) -> NewsItem:
    return NewsItem(
        id=headline,
        symbol="AAPL",
        headline=headline,
        body="b",
        source="rss",
        published_at=NOW,
        fetched_at=NOW,
    )


def _social(text: str, score: int = 100, rep: float = 500.0) -> SocialItem:
    return SocialItem(
        symbol="AAPL",
        text=text,
        author="alice",
        author_reputation=rep,
        engagement=Engagement(score=score, replies=5),
        posted_at=NOW,
        source="reddit:stocks",
        sentiment="bull",
    )


def _factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _gateway(analyst, news_sources, social_sources, clock, cache=None) -> AnalystGateway:
    return AnalystGateway(
        analyst=analyst,
        news_sources=news_sources,
        social_sources=social_sources,
        filter_params=SocialFilterParams(),
        clock=clock,
        cache=cache or TtlCache(clock=clock, ttl_seconds=3600),
        max_age_hours=72,
        max_items_per_symbol=50,
        social_baseline_window=20,
    )


def test_signal_for_persists_dedups_and_returns_provenance(tmp_path) -> None:
    clock = FakeClock(NOW)
    news = FakeNewsSource([_news("Apple beats"), _news("Apple beats")])  # dup headline
    social = FakeSocialSource([_social("AAPL bullish buy"), _social("AAPL to the moon buy")])
    analyst = RecordingAnalyst(
        AnalystSignal(sentiment=0.8, conviction=0.7, rationale="strong", model="fake")
    )
    gateway = _gateway(analyst, [news], [social], clock)

    factory = _factory(tmp_path)
    with session_scope(factory) as session:
        repos = Repositories(session)
        inst = repos.instruments.get_or_create("AAPL")
        result = gateway.signal_for("AAPL", repos, inst.id, context={"technical_score": 0.3})

        assert result.signal.llm_signal > 0
        assert len(result.news_item_ids) == 1  # duplicate collapsed
        assert result.social_digest_id is not None
        # analyst saw the compact digest, not raw items
        assert analyst.seen_digest is not None
        assert analyst.seen_digest.qualifying_post_count >= 1
        assert len(analyst.seen_news) == 1

        stored = repos.social_digests.latest(inst.id)
        assert stored is not None


def test_ttl_cache_prevents_refetch_within_window(tmp_path) -> None:
    clock = FakeClock(NOW)
    news = FakeNewsSource([_news("Apple beats")])
    social = FakeSocialSource([_social("AAPL buy")])
    analyst = RecordingAnalyst(AnalystSignal(sentiment=0.1, conviction=0.1))
    cache = TtlCache(clock=clock, ttl_seconds=3600)
    gateway = _gateway(analyst, [news], [social], clock, cache=cache)

    factory = _factory(tmp_path)
    with session_scope(factory) as session:
        repos = Repositories(session)
        inst = repos.instruments.get_or_create("AAPL")
        gateway.signal_for("AAPL", repos, inst.id)
        gateway.signal_for("AAPL", repos, inst.id)  # within TTL
        assert news.calls == 1
        assert social.calls == 1


def test_reset_daily_invokes_hook(tmp_path) -> None:
    calls: dict[str, Any] = {"n": 0}

    def hook() -> None:
        calls["n"] += 1

    clock = FakeClock(NOW)
    gateway = AnalystGateway(
        analyst=RecordingAnalyst(AnalystSignal(sentiment=0, conviction=0)),
        news_sources=[],
        social_sources=[],
        filter_params=SocialFilterParams(),
        clock=clock,
        cache=TtlCache(clock=clock, ttl_seconds=3600),
        max_age_hours=72,
        max_items_per_symbol=50,
        social_baseline_window=20,
        reset_daily_hook=hook,
    )
    gateway.reset_daily()
    assert calls["n"] == 1


def test_no_social_sources_yields_neutral_digest_and_still_signals(tmp_path) -> None:
    clock = FakeClock(NOW)
    analyst = RecordingAnalyst(AnalystSignal(sentiment=0.2, conviction=0.5))
    gateway = _gateway(analyst, [FakeNewsSource([_news("x")])], [], clock)

    factory = _factory(tmp_path)
    with session_scope(factory) as session:
        repos = Repositories(session)
        inst = repos.instruments.get_or_create("AAPL")
        result = gateway.signal_for("AAPL", repos, inst.id)
        assert result.social_digest_id is None
        assert analyst.seen_digest is None
        assert result.signal.llm_signal == 0.1


def test_no_capture_wired_yields_none_analysis_result_id(tmp_path) -> None:
    clock = FakeClock(NOW)
    analyst = RecordingAnalyst(AnalystSignal(sentiment=0.2, conviction=0.5))
    gateway = _gateway(analyst, [FakeNewsSource([_news("x")])], [], clock)

    factory = _factory(tmp_path)
    with session_scope(factory) as session:
        repos = Repositories(session)
        inst = repos.instruments.get_or_create("AAPL")
        result = gateway.signal_for("AAPL", repos, inst.id)
        assert result.analysis_result_id is None


class _FixedClient:
    def __init__(self, *, text: str | None = None, error: Exception | None = None) -> None:
        self._text = text
        self._error = error

    def generate(self, prompt: str) -> LLMResult:
        if self._error is not None:
            raise self._error
        assert self._text is not None
        return LLMResult(text=self._text, prompt_tokens=50, completion_tokens=8, model="fake-model")


def _capture_gateway(clock, client, capture) -> AnalystGateway:
    analyst = GeminiAnalyst(client, provenance_sink=capture.record)
    return AnalystGateway(
        analyst=analyst,
        news_sources=[FakeNewsSource([_news("Apple beats")])],
        social_sources=[],
        filter_params=SocialFilterParams(),
        clock=clock,
        cache=TtlCache(clock=clock, ttl_seconds=3600),
        max_age_hours=72,
        max_items_per_symbol=50,
        social_baseline_window=20,
        analysis_capture=capture,
    )


def test_analysis_capture_persists_redacted_request_response(tmp_path) -> None:
    clock = FakeClock(NOW)
    capture = AnalysisCapture()
    client = _FixedClient(text='{"sentiment":0.4,"conviction":0.3,"rationale":"ok"}')
    gateway = _capture_gateway(clock, client, capture)

    factory = _factory(tmp_path)
    with session_scope(factory) as session:
        repos = Repositories(session)
        inst = repos.instruments.get_or_create("AAPL")
        result = gateway.signal_for("AAPL", repos, inst.id)

        assert result.analysis_result_id is not None
        row = repos.analysis_results.get(result.analysis_result_id)
        assert row is not None
        assert "UNTRUSTED_NEWS" in row.request  # the exact prompt
        assert '"sentiment":0.4' in row.response  # the exact response
        assert row.model == "fake-model"
        assert row.prompt_tokens == 50
        assert row.is_fallback is False


def test_client_failure_persists_no_analysis_row(tmp_path) -> None:
    """A hard client failure (timeout) never reaches GeminiAnalyst's sink, so
    the buffer is empty and no analysis_result row is written -- the decision
    still degrades to neutral (proven elsewhere), just with no request/response
    to persist."""
    clock = FakeClock(NOW)
    capture = AnalysisCapture()
    gateway = _capture_gateway(clock, _FixedClient(error=TimeoutError("deadline")), capture)

    factory = _factory(tmp_path)
    with session_scope(factory) as session:
        repos = Repositories(session)
        inst = repos.instruments.get_or_create("AAPL")
        result = gateway.signal_for("AAPL", repos, inst.id)

        assert result.signal.is_fallback is True
        assert result.analysis_result_id is None
        assert repos.analysis_results.prune(inst.id, keep=0) == 0  # nothing was stored
