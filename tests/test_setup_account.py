from collections.abc import Callable
import sys
from types import SimpleNamespace

import pytest

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
