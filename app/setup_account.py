from argparse import ArgumentParser
from getpass import GetPassWarning, getpass
from pathlib import Path
import secrets
import sys
from tempfile import NamedTemporaryFile
import warnings

from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from dotenv import dotenv_values

from app.auth import ROOT, hasher


def read_masked(prompt: str) -> str:
    # Windows 舊版 Python 逐鍵讀取，只顯示星號。
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise SystemExit("請在 PowerShell 或終端機直接執行帳號設定。")
    if sys.version_info >= (3, 14):
        with warnings.catch_warnings():
            warnings.simplefilter("error", GetPassWarning)
            return getpass(prompt, echo_char="*")
    if sys.platform != "win32":
        with warnings.catch_warnings():
            warnings.simplefilter("error", GetPassWarning)
            return getpass(prompt)

    from msvcrt import getwch

    print(prompt, end="", file=sys.stderr, flush=True)
    chars: list[str] = []
    try:
        while True:
            char = getwch()
            if char == "\r":
                return "".join(chars).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "ignore")
            if char in {"\x00", "\xe0"}:
                getwch()
            elif char == "\b":
                if chars:
                    chars.pop()
                    print("\b \b", end="", file=sys.stderr, flush=True)
            elif char == "\x03":
                raise KeyboardInterrupt
            elif char >= " " and char != "\x7f":
                chars.append(char)
                print("*", end="", file=sys.stderr, flush=True)
    finally:
        print(file=sys.stderr)


def main() -> None:
    # 互動輸入密碼時隱藏文字，只寫入 Argon2id 雜湊。
    parser = ArgumentParser(description="設定 PeipeiNote 的本機白名單帳號。")
    parser.add_argument("--partner", action="store_true", help="在現有設定加入第二帳號")
    partner = parser.parse_args().partner
    path = ROOT / ".env"
    existing = ""
    values = {}
    if partner:
        if not path.is_file():
            raise SystemExit("請先設定第一帳號。")
        existing = path.read_text(encoding="utf-8")
        values = dotenv_values(path, interpolate=False)
        if not values.get("ADMIN_USERNAME") or not values.get("ADMIN_PASSWORD_HASH"):
            raise SystemExit("請先設定第一帳號。")
        if values.get("PARTNER_USERNAME") or values.get("PARTNER_PASSWORD_HASH"):
            raise SystemExit("已有第二帳號設定，檔案保持原樣。")
    elif path.exists():
        raise SystemExit("已有 .env。新增第二帳號請使用 --partner。")
    username = input("伴侶帳號：" if partner else "主帳號：").strip()
    if not 1 <= len(username) <= 128 or not all(
        char.isalnum() or char in "_.-" for char in username
    ):
        raise SystemExit("帳號請使用 1 至 128 個文字、數字、底線、句點或連字號。")
    if partner and username == values.get("ADMIN_USERNAME"):
        raise SystemExit("兩組帳號名稱必須不同。")
    minimum = 12 if partner else 8
    password = read_masked(f"密碼（至少 {minimum} 字元）：")
    if not minimum <= len(password) <= 1024:
        raise SystemExit(f"密碼長度須為 {minimum} 至 1024 字元。")
    if partner:
        try:
            if hasher.verify(values["ADMIN_PASSWORD_HASH"], password):
                raise SystemExit("兩組帳號的密碼必須不同。")
        except VerifyMismatchError:
            pass
        except (InvalidHashError, VerificationError):
            raise SystemExit("主帳號的密碼雜湊無法驗證。") from None
    if password != read_masked("再輸入一次密碼："):
        raise SystemExit("密碼不一致，請重試。")
    password_hash = hasher.hash(password)
    if partner:
        # 完整寫入暫存檔後才取代設定，避免中斷時留下半組帳號。
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".env.", delete=False) as config:
            temporary = Path(config.name)
            config.write(existing.rstrip("\n") + f"\nPARTNER_USERNAME={username}\nPARTNER_PASSWORD_HASH={password_hash}\n")
        try:
            if path.read_text(encoding="utf-8") != existing:
                raise SystemExit("設定已被其他程序修改，請重跑指令。")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        with path.open("x", encoding="utf-8") as config:
            config.write(
                f"SECRET_KEY={secrets.token_urlsafe(48)}\n"
                f"ADMIN_USERNAME={username}\n"
                f"ADMIN_PASSWORD_HASH={password_hash}\n"
                "APP_ORIGIN=http://127.0.0.1:8000\n"
            )
    print("帳號已建立。請啟動或重新啟動 PeipeiNote。")


if __name__ == "__main__":
    main()
