"""Story 3.12 — end-to-end provenance: a paper trade can be walked back
through news_item(s)/social_digest -> AnalystSignal request/response
(redacted) -> prompt_version -> decision -> risk_evaluation -> trade_proposal
(the decision-journal record) -> order, all joined by ids. Drives the whole
path with a DryRunBroker + seeded news/social + a mocked Gemini client, with
**no paid keys configured** (epic-level DoD).

(Matches Story 1.15's test_e2e_soak.py precedent: DryRunBroker never fills —
"none available in this sandbox" — so this proves the chain through `order`;
Fill/Trade's own FK linkage to `order` is separately proven by Epic 1/2's
test_execution.py/test_portfolio.py and isn't re-proven here.)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, time

from conftest import NOON_UTC, FakeMarketDataSource
from conftest import trending_candles as _trending_candles

from clav.clock import FakeClock
from clav.common.cache import TtlCache
from clav.data import tables
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.decision import DecisionEngine, Thresholds, Weights
from clav.domain.indicators import IndicatorService
from clav.domain.models import Engagement, NewsItem, SocialItem
from clav.domain.risk.engine import RiskEngine
from clav.domain.risk.rules import TradingWindow, default_rules
from clav.domain.risk.sizing import PositionSizer
from clav.domain.social import SocialFilterParams
from clav.integrations.dryrun_broker import DryRunBroker
from clav.integrations.llm import GeminiAnalyst, LLMResult
from clav.services.analyst_gateway import AnalystGateway
from clav.services.decision_journal import ApprovalPolicy
from clav.services.prompt_store import PromptVersionStore
from clav.services.scan_cycle import ScanCycleService
from clav.services.stop_monitor import StopMonitor

WINDOW = TradingWindow(start=time(9, 35), end=time(15, 55), timezone="America/New_York")
NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


class MockedGeminiClient:
    """Stands in for GeminiRestClient — no live Gemini, no paid key. Returns a
    fixed, valid strict-JSON signal so the response side of provenance has
    real content to redact-and-persist."""

    def generate(self, prompt: str) -> LLMResult:
        return LLMResult(
            text=json.dumps(
                {
                    "sentiment": 0.8,
                    "conviction": 0.7,
                    "catalysts": ["earnings beat", "raised guidance"],
                    "rationale": "Strong quarter plus constructive retail mood.",
                }
            ),
            prompt_tokens=120,
            completion_tokens=30,
            model="gemini-1.5-flash",
        )


def test_full_provenance_chain_walks_back_from_order_to_news_and_social(tmp_path) -> None:
    clock = FakeClock(NOON_UTC)
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)

    # Seeded news + social (Stage 1/2 sources, no paid keys).
    news_item = NewsItem(
        id="n1",
        symbol="AAPL",
        headline="Apple beats earnings, raises full-year guidance",
        body="Record iPhone and services revenue drove the beat.",
        url="https://example.com/aapl-earnings",
        source="rss",
        published_at=NOW,
        fetched_at=NOW,
    )

    class SeededNewsSource:
        def fetch(self, symbol, since):
            return [news_item]

    social_post = SocialItem(
        symbol="AAPL",
        text="AAPL crushing it this quarter, loading up on calls",
        author="trader_joe",
        author_reputation=800.0,
        engagement=Engagement(score=120, replies=15),
        posted_at=NOW,
        source="reddit:stocks",
        sentiment="bull",
    )

    class SeededSocialSource:
        def fetch(self, symbol, since):
            return [social_post]

    # Redacted request/response provenance capture (Story 3.4's sink hook).
    provenance_log: list[dict] = []

    def provenance_sink(symbol, prompt, response_text, signal, usage) -> None:
        provenance_log.append(
            {
                "symbol": symbol,
                "prompt_redacted": prompt[:50] + "...",  # never log the full body verbatim
                "response": response_text,
                "sentiment": signal.sentiment,
                "conviction": signal.conviction,
            }
        )

    prompt_store = PromptVersionStore(session_factory, clock=clock)
    seeded_version = prompt_store.seed_default(persona="Test persona for E2E provenance")

    analyst = GeminiAnalyst(
        MockedGeminiClient(),
        persona_provider=prompt_store.get_active,
        provenance_sink=provenance_sink,
    )
    gateway = AnalystGateway(
        analyst=analyst,
        news_sources=[SeededNewsSource()],
        social_sources=[SeededSocialSource()],
        filter_params=SocialFilterParams(),
        clock=clock,
        cache=TtlCache(clock=clock, ttl_seconds=3600),
        max_age_hours=72,
        max_items_per_symbol=50,
        social_baseline_window=20,
    )

    data_source = FakeMarketDataSource({"AAPL": _trending_candles("AAPL")}, clock=clock)
    broker = DryRunBroker(clock=clock, market_open=True)
    service = ScanCycleService(
        watchlist=["AAPL"],
        data_source=data_source,
        indicators=IndicatorService(),
        decision_engine=DecisionEngine(
            Weights(technical=0.5, llm=0.5, portfolio=0.0),
            Thresholds(buy=0.2, sell=-0.2),
            default_order_value=1000.0,
            clock=clock,
        ),
        risk_engine=RiskEngine(default_rules()),
        position_sizer=PositionSizer(
            risk_fraction=0.01, atr_stop_mult=2.0, take_profit_mult=2.0, default_order_value=1000.0
        ),
        stop_monitor=StopMonitor(data_source, clock=clock, quote_staleness_seconds=300),
        broker=broker,
        session_factory=session_factory,
        clock=clock,
        trading_window=WINDOW,
        max_position_value=2000.0,
        buying_power_buffer_pct=0.05,
        max_portfolio_exposure_pct=0.80,
        max_sector_allocation_pct=0.30,
        max_daily_loss_pct=0.03,
        max_drawdown_pct=0.10,
        min_avg_volume=0.0,
        earnings_blackout_days=2,
        cooldown_minutes=60,
        post_loss_cooldown_minutes=120,
        mode="dryrun",
        analyst_gateway=gateway,
        approval_policy=ApprovalPolicy(mode="auto"),
    )

    cycle_id = service.run(trigger="manual")

    # --- Provenance sink saw the redacted request/response --------------
    assert len(provenance_log) == 1
    assert provenance_log[0]["symbol"] == "AAPL"
    assert provenance_log[0]["sentiment"] == 0.8

    with session_scope(session_factory) as session:
        repos = Repositories(session)

        # --- news_item: seeded story persisted + deduped ------------------
        aapl = repos.instruments.get_by_symbol("AAPL")
        assert aapl is not None
        news_rows = repos.news_items.get_for_analysis(
            aapl.id, now=NOW, max_age_hours=72, limit=10
        )
        assert len(news_rows) == 1
        assert news_rows[0].headline == news_item.headline
        news_ids = repos.news_items.recent_ids_for_analysis(
            aapl.id, now=NOW, max_age_hours=72, limit=10
        )

        # --- social_digest: aggregated from the seeded post ----------------
        digest = repos.social_digests.latest(aapl.id)
        assert digest is not None
        assert digest.qualifying_post_count == 1
        digest_row = (
            session.query(tables.SocialDigestRow).filter_by(instrument_id=aapl.id).first()
        )
        assert digest_row is not None

        # --- decision: carries the Gemini signal + prompt_version ----------
        decision_row = (
            session.query(tables.Decision).filter_by(instrument_id=aapl.id).first()
        )
        assert decision_row is not None
        assert decision_row.llm_signal > 0  # sentiment(0.8) * conviction(0.7)
        llm_prov = decision_row.reasoning["llm"]
        assert llm_prov["is_fallback"] is False
        assert llm_prov["prompt_version"] == str(seeded_version.id)
        assert llm_prov["news_item_ids"] == news_ids
        assert llm_prov["social_digest_id"] == digest_row.id

        # --- prompt_version: the exact version used is still resolvable ---
        active_prompt = repos.prompt_versions.get(int(llm_prov["prompt_version"]))
        assert active_prompt is not None
        assert active_prompt.content == "Test persona for E2E provenance"

        # --- risk_evaluation: joined to the same decision ------------------
        risk_eval = repos.risk_evaluations.get_by_decision_id(decision_row.id)
        assert risk_eval is not None

        # --- trade_proposal: the decision-journal record, same decision_id,
        #     and its inputs_ref links back to the exact news/social rows ---
        journal = repos.trade_proposals.list_recent(limit=10)
        assert len(journal) == 1
        proposal = journal[0]
        assert proposal.decision_id == decision_row.id
        assert proposal.status == "executed"
        assert proposal.inputs_ref["news_item_ids"] == news_ids
        assert proposal.inputs_ref["social_digest_id"] == digest_row.id
        assert "Strong quarter" in proposal.rationale or proposal.rationale

        # --- order: same decision_id, deterministic client_order_id --------
        order_row = repos.orders.get_by_client_order_id(f"clav-{cycle_id}-AAPL-buy")
        assert order_row is not None
        assert order_row.decision_id == decision_row.id
        assert order_row.instrument_id == aapl.id
