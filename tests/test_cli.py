import json

import pytest

from kalshitrader import cli


def test_estimate_and_report_commands(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text(f"DB_PATH={tmp_path / 'db.sqlite'}\nMANUAL_ESTIMATES_PATH={tmp_path / 'e.json'}\n")
    with pytest.raises(SystemExit) as e:
        cli.main(["--env-file", str(env), "estimate", "kxabc", "0.66", "--note", "hi"])
    assert e.value.code == 0
    assert json.loads((tmp_path / "e.json").read_text())["KXABC"]["p_yes"] == 0.66
    with pytest.raises(SystemExit):
        cli.main(["--env-file", str(env), "estimates"])
    assert "KXABC" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["--env-file", str(env), "report"])
    assert "equity" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["--env-file", str(env), "halt"])
    with pytest.raises(SystemExit):
        cli.main(["--env-file", str(env), "report", "--json"])
    assert "realized_pnl" in capsys.readouterr().out


def test_live_without_credentials_is_a_config_error(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TRADING_MODE=live\n")
    with pytest.raises(SystemExit) as e:
        cli.main(["--env-file", str(env), "report"])
    assert e.value.code == 2
