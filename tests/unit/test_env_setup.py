import stat

import pytest

from clav.web.env_setup import env_key_is_set, write_env_values


def test_env_key_is_set_false_when_file_missing(tmp_path) -> None:
    assert env_key_is_set(tmp_path / ".env", "CLAV_ALPACA__API_KEY") is False


def test_env_key_is_set_false_when_commented_out(tmp_path) -> None:
    path = tmp_path / ".env"
    path.write_text("# CLAV_ALPACA__API_KEY=your-alpaca-paper-key-id\n")
    assert env_key_is_set(path, "CLAV_ALPACA__API_KEY") is False


def test_env_key_is_set_false_when_blank_value(tmp_path) -> None:
    path = tmp_path / ".env"
    path.write_text("CLAV_ALPACA__API_KEY=\n")
    assert env_key_is_set(path, "CLAV_ALPACA__API_KEY") is False


def test_env_key_is_set_true_when_present(tmp_path) -> None:
    path = tmp_path / ".env"
    path.write_text("CLAV_ALPACA__API_KEY=abc123\n")
    assert env_key_is_set(path, "CLAV_ALPACA__API_KEY") is True


def test_write_creates_file_with_restricted_permissions(tmp_path) -> None:
    path = tmp_path / ".env"
    write_env_values(path, {"CLAV_ALPACA__API_KEY": "abc123"})

    assert path.read_text() == "CLAV_ALPACA__API_KEY=abc123\n"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_write_updates_existing_key_in_place_preserving_other_lines(tmp_path) -> None:
    path = tmp_path / ".env"
    path.write_text("CLAV_ALPACA__API_KEY=old-key\nCLAV_LLM__API_KEY=gemini-key\n")

    write_env_values(path, {"CLAV_ALPACA__API_KEY": "new-key"})

    lines = path.read_text().splitlines()
    assert "CLAV_ALPACA__API_KEY=new-key" in lines
    assert "CLAV_LLM__API_KEY=gemini-key" in lines
    assert len(lines) == 2


def test_write_uncomments_a_commented_example_line(tmp_path) -> None:
    path = tmp_path / ".env"
    path.write_text("# CLAV_ALPACA_LIVE__API_KEY=your-alpaca-live-key-id\n")

    write_env_values(path, {"CLAV_ALPACA_LIVE__API_KEY": "live-key"})

    assert path.read_text().splitlines() == ["CLAV_ALPACA_LIVE__API_KEY=live-key"]


def test_write_appends_new_key_when_absent(tmp_path) -> None:
    path = tmp_path / ".env"
    path.write_text("CLAV_ALPACA__API_KEY=abc\n")

    write_env_values(path, {"CLAV_ALPACA__API_SECRET": "shh"})

    lines = path.read_text().splitlines()
    assert "CLAV_ALPACA__API_KEY=abc" in lines
    assert "CLAV_ALPACA__API_SECRET=shh" in lines


def test_write_rejects_a_value_containing_a_newline(tmp_path) -> None:
    path = tmp_path / ".env"
    with pytest.raises(ValueError, match="newline"):
        write_env_values(path, {"CLAV_ALPACA__API_KEY": "abc\nCLAV_EVIL__VAR=1"})

    assert not path.exists()
