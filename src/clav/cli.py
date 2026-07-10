"""Operator CLI for system_control toggles (emergency stop / pause).

Separate from the clav-core daemon (``clav.app``, Story 1.13) so an operator can
trip/clear the estop even if the core process's own scheduler loop is
misbehaving — this just writes a row to ``system_control``; clav-core polls it
every cycle (Story 1.10 acceptance criteria).
"""

from __future__ import annotations

import click
from sqlalchemy.orm import Session, sessionmaker

from clav.clock import SystemClock
from clav.common.errors import ConfigError
from clav.config import Settings, load_settings
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories

EMERGENCY_STOP_KEY = "emergency_stop"
PAUSED_KEY = "paused"


def _session_factory(settings: Settings) -> sessionmaker[Session]:
    engine = make_engine(settings.data_dir / "clav.db")
    return make_session_factory(engine)


@click.group()
@click.pass_context
def cli(ctx: click.Context) -> None:
    """CLAV operator controls."""
    try:
        ctx.obj = load_settings()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command()
@click.pass_obj
def status(settings: Settings) -> None:
    """Show emergency_stop / paused state."""
    factory = _session_factory(settings)
    with session_scope(factory) as session:
        repos = Repositories(session)
        estop = repos.system_control.get(EMERGENCY_STOP_KEY, "false")
        paused = repos.system_control.get(PAUSED_KEY, "false")
    click.echo(f"emergency_stop: {estop}")
    click.echo(f"paused: {paused}")


def _set_flag(settings: Settings, key: str, value: bool, actor: str) -> None:
    factory = _session_factory(settings)
    clock = SystemClock()
    now = clock.now()
    with session_scope(factory) as session:
        repos = Repositories(session)
        before = repos.system_control.get(key, "false")
        after = "true" if value else "false"
        repos.system_control.set(key, after, updated_at=now, updated_by=actor)
        repos.audit_log.add(
            ts=now,
            actor=actor,
            action=f"{key}_set",
            entity_type="system_control",
            entity_id=key,
            before={"value": before},
            after={"value": after},
        )


@cli.command("estop-set")
@click.option("--actor", default="operator")
@click.pass_obj
def estop_set(settings: Settings, actor: str) -> None:
    """Trip the emergency stop: vetoes all new BUY entries. Exits still allowed."""
    _set_flag(settings, EMERGENCY_STOP_KEY, True, actor)
    click.echo("emergency_stop: true")


@cli.command("estop-clear")
@click.option("--actor", default="operator")
@click.pass_obj
def estop_clear(settings: Settings, actor: str) -> None:
    """Clear the emergency stop."""
    _set_flag(settings, EMERGENCY_STOP_KEY, False, actor)
    click.echo("emergency_stop: false")


@cli.command("pause")
@click.option("--actor", default="operator")
@click.pass_obj
def pause(settings: Settings, actor: str) -> None:
    """Pause: vetoes all new BUY entries. Exits still allowed."""
    _set_flag(settings, PAUSED_KEY, True, actor)
    click.echo("paused: true")


@cli.command("resume")
@click.option("--actor", default="operator")
@click.pass_obj
def resume(settings: Settings, actor: str) -> None:
    """Resume from pause."""
    _set_flag(settings, PAUSED_KEY, False, actor)
    click.echo("paused: false")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
