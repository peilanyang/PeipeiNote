from collections.abc import Callable
from pathlib import Path
import secrets
import sys
from types import SimpleNamespace

from dotenv import dotenv_values
import pytest

from app.auth import hasher

from app import setup_account


def test_masked_windows_input(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # 模擬 Windows 終端機按鍵，確認退格移除星號且密碼文字未輸出。
    keys = iter(("A", "b", "\b", "7", "!", "\r"))
    getwch: Callable[[], str] = keys.__next__
    terminal = SimpleNamespace(isatty=lambda: True, write=sys.stderr.write, flush=sys.stderr.flush)
    monkeypatch.setattr(setup_account, "sys", SimpleNamespace(
        stdin=terminal, stderr=terminal, version_info=(3, 13), platform="win32"
    ))
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(getwch=getwch))
    assert setup_account.read_masked("密碼：") == "A7!"
    assert capsys.readouterr().err == "密碼：**\b \b**\n"


def test_native_mask_option(monkeypatch: pytest.MonkeyPatch) -> None:
    # 新版 Python 交由標準函式顯示星號。
    terminal = SimpleNamespace(isatty=lambda: True)
    monkeypatch.setattr(setup_account, "sys", SimpleNamespace(
        stdin=terminal, stderr=terminal, version_info=(3, 14)
    ))

    def fake_getpass(prompt: str, *, echo_char: str) -> str:
        # 檢查呼叫時指定星號。
        assert prompt == "密碼："
        assert echo_char == "*"
        return "example"

    monkeypatch.setattr(setup_account, "getpass", fake_getpass)
    assert setup_account.read_masked("密碼：") == "example"


def test_add_partner_preserves_existing_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 確認新增伴侶不覆蓋主帳號、金鑰或網址，且輸出的密碼只有雜湊。
    env = tmp_path / ".env"
    primary_hash = hasher.hash(secrets.token_urlsafe(24))
    key = secrets.token_urlsafe(48)
    env.write_text(
        f"ADMIN_USERNAME=primary\nADMIN_PASSWORD_HASH={primary_hash}\n"
        f"SECRET_KEY={key}\nAPP_ORIGIN=https://notes.test\n",
        encoding="utf-8",
    )
    new_password = secrets.token_urlsafe(24)
    monkeypatch.setattr(setup_account, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["setup_account", "--partner"])
    monkeypatch.setattr("builtins.input", lambda prompt: "second")
    monkeypatch.setattr(setup_account, "read_masked", lambda prompt: new_password)
    setup_account.main()
    values = dotenv_values(env, interpolate=False)
    assert values["ADMIN_USERNAME"] == "primary"
    assert values["ADMIN_PASSWORD_HASH"] == primary_hash
    assert values["SECRET_KEY"] == key
    assert values["APP_ORIGIN"] == "https://notes.test"
    assert values["PARTNER_USERNAME"] == "second"
    assert hasher.verify(values["PARTNER_PASSWORD_HASH"], new_password)
    assert new_password not in env.read_text(encoding="utf-8")
    assert list(tmp_path.iterdir()) == [env]
    with pytest.raises(SystemExit, match="已有第二帳號"):
        setup_account.main()
