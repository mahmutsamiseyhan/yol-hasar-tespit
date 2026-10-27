"""Yönetici girişi: iş takip paneli ve durum değişiklikleri yalnızca belediye personeline açıktır.

Herkese açık kalanlar: vatandaş bildirim sayfası (/bildir), bildirim takip sayfası (/takip) ve analiz uç noktası.
Oturum, sunucunun gizli anahtarıyla imzalanmış bir çerezde tutulur (HttpOnly, SameSite=Strict); oturum tablosu
gerekmez. Şifreler düz metin ya da PBKDF2 özeti olarak verilebilir; özet üretmek için:

    python -m app.auth
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import secrets
import time

COOKIE_NAME = "yh_oturum"
PBKDF2_PREFIX = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 260_000


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations).hex()
    return f"{PBKDF2_PREFIX}${iterations}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    if stored.startswith(PBKDF2_PREFIX + "$"):
        _, iterations, salt, digest = stored.split("$")
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations)).hex()
        return hmac.compare_digest(candidate, digest)
    return hmac.compare_digest(password.encode(), stored.encode())


def parse_users(raw: str | None, single_user: str = "yonetici", single_password: str | None = None) -> dict:
    """ADMIN_USERS="ali:sifre,veli:pbkdf2_sha256$..." ya da tek kullanıcı için ADMIN_PASSWORD."""
    users = {}
    for part in (raw or "").split(","):
        name, sep, password = part.strip().partition(":")
        if sep and name and password:
            users[name] = password
    if single_password:
        users.setdefault(single_user, single_password)
    return users


class Auth:
    def __init__(self, users: dict[str, str], secret: bytes, session_hours: float = 12):
        self.users = users
        self._secret = secret
        self.ttl_s = int(session_hours * 3600)
        # Bilinmeyen kullanıcıda da aynı süre harcansın: kullanıcı adı yoklaması yapılamasın
        self._dummy = hash_password(secrets.token_hex(8))

    @property
    def enabled(self) -> bool:
        return bool(self.users)

    def check(self, user: str, password: str) -> bool:
        stored = self.users.get(user)
        ok = verify_password(password, stored or self._dummy)
        return stored is not None and ok

    def _sign(self, payload: str) -> str:
        return hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self, user: str, now: float | None = None) -> str:
        payload = f"{user}|{int((now or time.time()) + self.ttl_s)}"
        return base64.urlsafe_b64encode(f"{payload}|{self._sign(payload)}".encode()).decode()

    def user_from(self, token: str | None, now: float | None = None) -> str | None:
        if not token:
            return None
        try:
            user, expires, sig = base64.urlsafe_b64decode(token.encode()).decode().rsplit("|", 2)
        except (ValueError, UnicodeDecodeError):
            return None
        if not hmac.compare_digest(sig, self._sign(f"{user}|{expires}")):
            return None
        if int(expires) < (now or time.time()) or user not in self.users:
            return None  # süresi dolmuş ya da kullanıcı yapılandırmadan kaldırılmış
        return user


if __name__ == "__main__":
    pw = getpass.getpass("Şifre: ")
    if pw != getpass.getpass("Şifre (tekrar): "):
        raise SystemExit("Şifreler eşleşmiyor.")
    if len(pw) < 10:
        raise SystemExit("Şifre en az 10 karakter olmalı.")
    print(hash_password(pw))
