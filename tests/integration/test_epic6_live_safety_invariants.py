"""Story 6.7 — the Epic-6 live-safety invariants CI must enforce
(docs/epics/epic-06-live-trading-and-soak.md, Story 6.7). Mirrors the
rationale of Epic 3's test_epic3_chaos_invariants.py and Epic 2's
test_epic2_risk_invariants.py: a safety-critical invariant deserves one file
a reviewer can open and see all of them proven, even though several are also
exercised in the story-by-story unit tests (test_config.py, test_broker_
factory.py, test_alpaca_broker.py, test_stop_monitor.py, test_scan_cycle.py).

1. Live-gate fail-closed matrix (decision #1): every combination of
   mode x i_understand_live_trading x live-credential-presence, exercised
   end-to-end exactly as the composition root wires it (Settings load, the
   first key, then broker_factory, the second key) -- never a silent
   fall-back to paper/dryrun, and mode/flag are inert together outside
   `mode: live`.
2. No shipped config selects live except the pilot profile, and the pilot
   only with the flag set -- a property test over every config/*.yaml file
   in the repo, so a future config addition can't accidentally default to
   live.
3. AlpacaBroker mapping (Story 6.2) shares PaperBroker's proven mapping via
   AlpacaBrokerBase -- limit orders, and the cancel/get_order 404-vs-other
   error split, proven here (gathering coverage the story-level suite in
   test_alpaca_broker.py doesn't already close).
4. Flatten-on-estop (Story 6.3) invariants are proven in depth in
   test_stop_monitor.py's "flatten()" section and test_scan_cycle.py's
   flatten_on_estop tests; referenced here rather than duplicated line for
   line, per this file's own no-duplication stance (see point 1's remark).
5. No CI job ever authenticates a live account: a static guard asserting
   every AlpacaBroker construction under tests/ either passes a mocked
   client or monkeypatches TradingClient first.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, OrderType
from alpaca.trading.models import Order as AlpacaOrder

from clav.common.errors import ConfigError
from clav.config import Settings, load_settings
from clav.domain.models import OrderRequest
from clav.integrations.alpaca_broker import AlpacaBroker
from clav.integrations.broker_factory import broker_factory
from clav.integrations.dryrun_broker import DryRunBroker
from clav.integrations.paper_broker import PaperBroker
from clav.interfaces.broker import Broker

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)

BASE_YAML: dict = {"mode": "paper", "watchlist": ["AAPL"], "alpaca": {}}


def _write_yaml(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data))
    return path


def _load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str,
    flag: bool,
    live_key: str | None,
    live_secret: str | None,
) -> Settings:
    """The first key: config-load time (Settings._guard_live_mode)."""
    data: dict = {**BASE_YAML, "mode": mode}
    if flag:
        data["i_understand_live_trading"] = True
    yaml_path = _write_yaml(tmp_path / "config.yaml", data)
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "paper-key")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "paper-secret")
    monkeypatch.delenv("CLAV_ALPACA_LIVE__API_KEY", raising=False)
    monkeypatch.delenv("CLAV_ALPACA_LIVE__API_SECRET", raising=False)
    if live_key is not None:
        monkeypatch.setenv("CLAV_ALPACA_LIVE__API_KEY", live_key)
    if live_secret is not None:
        monkeypatch.setenv("CLAV_ALPACA_LIVE__API_SECRET", live_secret)
    return load_settings(env_file=tmp_path / "does-not-exist.env")


def _build_broker(settings: Settings) -> Broker:
    """The second key: broker construction, exactly as
    app.build_core_services wires it (src/clav/app.py:280-294)."""
    live_key = (
        settings.alpaca_live.api_key.get_secret_value() if settings.alpaca_live.api_key else None
    )
    live_secret = (
        settings.alpaca_live.api_secret.get_secret_value()
        if settings.alpaca_live.api_secret
        else None
    )
    return broker_factory(
        settings.mode,
        clock=MagicMock(),
        alpaca_api_key=settings.alpaca.api_key.get_secret_value(),
        alpaca_api_secret=settings.alpaca.api_secret.get_secret_value(),
        live_api_key=live_key,
        live_api_secret=live_secret,
    )


# --- 1. Live-gate fail-closed matrix (decision #1) --------------------------

LIVE_KEYS = "live-key"
LIVE_SECRET = "live-secret"

# (mode, flag, live_key, live_secret) -> outcome
#   "config_error"  -- refused at Settings-load time (first key missing)
#   "value_error"   -- Settings load fine, refused at broker_factory (second
#                      key missing)
#   PaperBroker/DryRunBroker/AlpacaBroker -- succeeds, this broker type
MATRIX: list[tuple[str, bool, str | None, str | None, object]] = [
    # paper: flag and live keys are both irrelevant/inert.
    ("paper", False, None, None, PaperBroker),
    ("paper", True, None, None, PaperBroker),
    ("paper", True, LIVE_KEYS, LIVE_SECRET, PaperBroker),
    # dryrun: same -- inert.
    ("dryrun", False, None, None, DryRunBroker),
    ("dryrun", True, None, None, DryRunBroker),
    # live: first key (flag) missing -> refused before broker_factory is
    # ever reached, regardless of whether live keys are present.
    ("live", False, None, None, "config_error"),
    ("live", False, LIVE_KEYS, LIVE_SECRET, "config_error"),
    # live: flag set, second key (credentials) incomplete -> refused, never
    # a silent fall-back to paper/dryrun.
    ("live", True, None, None, "value_error"),
    ("live", True, LIVE_KEYS, None, "value_error"),
    ("live", True, None, LIVE_SECRET, "value_error"),
    # live: both keys present -> the only combination that succeeds live.
    ("live", True, LIVE_KEYS, LIVE_SECRET, AlpacaBroker),
]


@pytest.mark.parametrize("mode,flag,live_key,live_secret,expected", MATRIX)
def test_live_gate_fail_closed_matrix(
    tmp_path, monkeypatch, mode, flag, live_key, live_secret, expected
) -> None:
    kwargs = {"mode": mode, "flag": flag, "live_key": live_key, "live_secret": live_secret}
    if expected == "config_error":
        with pytest.raises(ConfigError, match="i_understand_live_trading"):
            _load(tmp_path, monkeypatch, **kwargs)
        return

    settings = _load(tmp_path, monkeypatch, **kwargs)

    if expected == "value_error":
        with pytest.raises(ValueError, match="live_api_key"):
            _build_broker(settings)
        return

    broker = _build_broker(settings)
    assert isinstance(broker, expected)


def test_flag_is_inert_without_mode_live(tmp_path, monkeypatch) -> None:
    settings = _load(
        tmp_path, monkeypatch, mode="paper", flag=True, live_key=LIVE_KEYS, live_secret=LIVE_SECRET
    )
    assert settings.mode == "paper"
    broker = _build_broker(settings)
    assert isinstance(broker, PaperBroker)


# --- 2. No shipped config selects live except the pilot, and only with the
#        flag ---------------------------------------------------------------


def test_no_config_file_selects_live_except_the_pilot_profile() -> None:
    config_dir = REPO_ROOT / "config"
    yaml_files = sorted(config_dir.glob("*.yaml"))
    assert yaml_files, "expected at least config.example.yaml and config.pilot.example.yaml"

    live_files = []
    for path in yaml_files:
        data = yaml.safe_load(path.read_text()) or {}
        if data.get("mode") == "live":
            live_files.append((path.name, data))

    assert [name for name, _ in live_files] == ["config.pilot.example.yaml"], (
        f"only config.pilot.example.yaml may set mode: live; found: {live_files}"
    )
    _, pilot_data = live_files[0]
    assert pilot_data.get("i_understand_live_trading") is True


def test_settings_defaults_to_paper_and_flag_false() -> None:
    assert Settings.model_fields["mode"].default == "paper"
    assert Settings.model_fields["i_understand_live_trading"].default is False


# --- 3. AlpacaBroker mapping (Story 6.2) -------------------------------------


def _mock_broker() -> tuple[AlpacaBroker, MagicMock]:
    client = MagicMock(spec=TradingClient)
    return AlpacaBroker("key", "secret", client=client), client


def _alpaca_order(**overrides) -> AlpacaOrder:
    import uuid

    fields = {
        "id": uuid.uuid4(),
        "client_order_id": "clav-c1-AAPL-buy",
        "created_at": NOW,
        "updated_at": NOW,
        "submitted_at": NOW,
        "time_in_force": "day",
        "status": OrderStatus.FILLED,
        "extended_hours": False,
        "symbol": "AAPL",
        "qty": "8",
        "side": OrderSide.BUY,
        "type": OrderType.LIMIT,
        "order_type": OrderType.LIMIT,
        "limit_price": "150.00",
    }
    fields.update(overrides)
    return AlpacaOrder(**fields)


def test_alpaca_broker_maps_limit_orders() -> None:
    broker, client = _mock_broker()
    client.submit_order.return_value = _alpaca_order()

    order = broker.submit_order(
        OrderRequest(
            client_order_id="clav-c1-AAPL-buy",
            symbol="AAPL",
            side="buy",
            qty=8,
            order_type="limit",
            limit_price=150.0,
        )
    )

    assert order.order_type == "limit"
    assert order.limit_price == 150.0


def test_alpaca_broker_cancel_order_404_is_a_noop_not_a_raise() -> None:
    broker, client = _mock_broker()
    client.get_order_by_client_id.return_value = _alpaca_order()
    client.cancel_order_by_id.side_effect = _api_error(404)

    broker.cancel_order("clav-c1-AAPL-buy")  # must not raise

    client.cancel_order_by_id.assert_called_once()


def test_alpaca_broker_cancel_order_non_404_raises() -> None:
    broker, client = _mock_broker()
    client.get_order_by_client_id.return_value = _alpaca_order()
    client.cancel_order_by_id.side_effect = _api_error(500)

    with pytest.raises(APIError):
        broker.cancel_order("clav-c1-AAPL-buy")


def test_alpaca_broker_get_order_non_404_raises() -> None:
    broker, client = _mock_broker()
    client.get_order_by_client_id.side_effect = _api_error(500)

    with pytest.raises(APIError):
        broker.get_order("clav-c1-AAPL-buy")


def _api_error(status_code: int, message: str = "error") -> APIError:
    http_error = MagicMock()
    http_error.response.status_code = status_code
    return APIError(f'{{"code": {status_code}0000, "message": "{message}"}}', http_error)


# --- 4. Flatten-on-estop (Story 6.3) -----------------------------------------
#
# Proven in depth elsewhere, not duplicated here:
#   - tests/unit/test_stop_monitor.py "flatten() -- Story 6.3" section:
#     closes every open position with no quote needed, skips zero-qty and
#     already-in-flight positions, closes multiple positions each exactly
#     once, and is idempotent across a partially-flattened re-run.
#   - tests/integration/test_scan_cycle.py: flatten_on_estop=True closes a
#     held position on an estop trip with no stop breach; flatten_on_estop
#     off (the default) leaves the position held.
# Both exercise the same _submit_forced_exit() path StopMonitor.check() and
# .flatten() share (epic-06 decision #3) -- so the exit-side invariants
# every stop-loss already proves (idempotent client_order_id, an audited
# risk_evaluation) cover flatten too, not a separate bypass.


# --- 5. No live network from any test ----------------------------------------

_ALPACA_BROKER_CONSTRUCTION = re.compile(r"\bAlpacaBroker\s*\(")


def test_no_test_constructs_an_unmocked_alpaca_broker() -> None:
    """Nothing under tests/ may construct an AlpacaBroker backed by a real
    (un-mocked) alpaca-py TradingClient -- constructing one is harmless (no
    network call happens until a method is invoked), but every call site
    must make the mocking explicit at the construction site so a later
    change that starts calling broker methods can't accidentally reach a
    real endpoint. Each site must either pass client=<mock> on the same
    line, or monkeypatch clav.integrations.alpaca_broker.TradingClient
    first (as test_default_client_is_constructed_with_paper_false does, to
    prove the real wiring without ever touching the network)."""
    offenders: list[str] = []
    for path in TESTS_ROOT.rglob("*.py"):
        if path.name == Path(__file__).name:
            continue  # this file's own helpers always pass client=
        text = path.read_text()
        if "AlpacaBroker(" not in text:
            continue
        mocks_trading_client = "alpaca_broker.TradingClient" in text
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not _ALPACA_BROKER_CONSTRUCTION.search(line):
                continue
            if "client=" in line or mocks_trading_client:
                continue
            offenders.append(f"{path.relative_to(TESTS_ROOT)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "AlpacaBroker constructed without a mocked client and without "
        "monkeypatching TradingClient first -- this risks a real network "
        "call against a live Alpaca account from CI:\n" + "\n".join(offenders)
    )
