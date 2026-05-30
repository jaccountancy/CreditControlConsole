import hashlib
import base64
import secrets
from datetime import timedelta

from .config import get_settings
from .database import get_connection, utcnow


def random_token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def hash_token(token: str) -> str:
    settings = get_settings()
    return hashlib.sha256(f"{settings.app_secret}:{token}".encode("utf-8")).hexdigest()


def _secret_stream(label: str, length: int) -> bytes:
    settings = get_settings()
    seed = f"{settings.app_secret}:{label}".encode("utf-8")
    output = b""
    counter = 0
    while len(output) < length:
        output += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return output[:length]


def encrypt_secret(value: str | None, label: str) -> str:
    plain = str(value or "").encode("utf-8")
    stream = _secret_stream(label, len(plain))
    cipher = bytes(a ^ b for a, b in zip(plain, stream))
    return base64.urlsafe_b64encode(cipher).decode("ascii")


def decrypt_secret(value: str | None, label: str) -> str:
    if not value:
        return ""
    cipher = base64.urlsafe_b64decode(str(value).encode("ascii"))
    stream = _secret_stream(label, len(cipher))
    plain = bytes(a ^ b for a, b in zip(cipher, stream))
    return plain.decode("utf-8")


def create_session(user_id: str, label: str) -> str:
    settings = get_settings()
    token = random_token()
    expires_at = utcnow() + timedelta(days=settings.session_ttl_days)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO sessions (user_id, token_hash, label, expires_at)
                VALUES (%s, %s, %s, %s)
                """,
                (user_id, hash_token(token), label, expires_at),
            )
        connection.commit()

    return token
