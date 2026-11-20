from getpass import getpass
import secrets

from app.auth import ROOT, hasher


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
    password = getpass("密碼（至少 12 字元）：")
    if not 12 <= len(password) <= 1024:
        raise SystemExit("密碼長度須為 12 至 1024 字元。")
    if password != getpass("再輸入一次密碼："):
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
