import sys
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from tg_signer.cli.signer import tg_signer


def test_webgui_forwards_runtime_settings(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "tg_signer.webui",
        SimpleNamespace(
            AUTH_CODE_ENV="TG_SIGNER_GUI_AUTHCODE",
            main=lambda **kwargs: calls.append(kwargs),
        ),
    )
    monkeypatch.delenv("TG_SESSION_STRING", raising=False)
    result = CliRunner().invoke(
        tg_signer,
        [
            "--account",
            "second",
            "--workdir",
            str(tmp_path),
            "--session_dir",
            str(tmp_path / "sessions"),
            "--proxy",
            "socks5://127.0.0.1:1080",
            "webgui",
            "--port",
            "8099",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["account"] == "second"
    assert calls[0]["workdir"] == str(tmp_path)
    assert calls[0]["session_dir"] == str(tmp_path / "sessions")
    assert calls[0]["proxy"]["port"] == 1080
    assert calls[0]["port"] == 8099


@pytest.mark.parametrize("options", [["--in-memory"], ["--session-string", "example"]])
def test_webgui_rejects_ambiguous_session_selection(monkeypatch, options):
    monkeypatch.setitem(
        sys.modules,
        "tg_signer.webui",
        SimpleNamespace(
            AUTH_CODE_ENV="TG_SIGNER_GUI_AUTHCODE",
            main=lambda **kwargs: pytest.fail("should not start"),
        ),
    )
    result = CliRunner().invoke(tg_signer, options + ["webgui"])
    assert result.exit_code == 2
    assert "本机 .session 文件" in result.output
