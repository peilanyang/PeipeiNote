from getpass import GetPassWarning, getpass
import secrets
import sys
import warnings

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
    # 互動輸入密碼時隱藏文字，只寫入 Argon2id 雜湊與隨機金鑰。
    path = ROOT / ".env"
    if path.exists():
        raise SystemExit("已有 .env，帳號設定保持原樣。")
    username = input("管理者帳號：").strip()
    if not 1 <= len(username) <= 128 or not all(
        char.isalnum() or char in "_.-" for char in username
    ):
        raise SystemExit("帳號請使用 1 至 128 個文字、數字、底線、句點或連字號。")
    password = read_masked("密碼（至少 8 字元）：")
    if not 8 <= len(password) <= 1024:
        raise SystemExit("密碼長度須為 8 至 1024 字元。")
    if password != read_masked("再輸入一次密碼："):
        raise SystemExit("密碼不一致，請重試。")
    password_hash = hasher.hash(password)
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
