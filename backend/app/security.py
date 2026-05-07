import hashlib
import secrets
from datetime import timedelta

from .config import get_settings
from .database import get_connection, utcnow


def random_token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def hash_token(token: str) -> str:
    settings = get_settings()
    return hashlib.sha256(f"{settings.app_secret}:{token}".encode("utf-8")).hexdigest()


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
