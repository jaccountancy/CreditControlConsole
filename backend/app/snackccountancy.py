from __future__ import annotations

import hashlib
import hmac
import json
import base64
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException, Request, Response, status

from .config import get_settings
from .database import get_connection, utcnow
from .security import hash_token, random_token

SNACK_SESSION_COOKIE_NAME = "snackccountancy_session"
SNACK_SESSION_LABEL = "Snackccountancy checkout"
SNACK_SESSION_TTL_DAYS = 180
UK_TZ = ZoneInfo("Europe/London")
SKU_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass
class SnackSessionContext:
    token: str | None
    customer: dict[str, Any] | None


def _money(value_pence: int) -> str:
    return f"{value_pence / 100:.2f}"


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _current_uk_week_start(today: date | None = None) -> date:
    if today is None:
        today = datetime.now(UK_TZ).date()
    return today - timedelta(days=today.weekday())


def _current_uk_week_bounds() -> tuple[datetime, datetime, date]:
    now_uk = datetime.now(UK_TZ)
    week_start_date = _current_uk_week_start(now_uk.date())
    week_start_uk = datetime.combine(week_start_date, time.min, tzinfo=UK_TZ)
    week_end_uk = week_start_uk + timedelta(days=7)
    return week_start_uk.astimezone(timezone.utc), week_end_uk.astimezone(timezone.utc), week_start_date


def _normalise_email(email: str | None) -> str:
    return str(email or "").strip().lower()


def _session_max_age_seconds() -> int:
    return SNACK_SESSION_TTL_DAYS * 24 * 60 * 60


def set_snack_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        SNACK_SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="none" if settings.base_url.startswith("https://") else "lax",
        secure=settings.base_url.startswith("https://"),
        max_age=_session_max_age_seconds(),
    )


def clear_snack_session_cookie(response: Response) -> None:
    response.delete_cookie(SNACK_SESSION_COOKIE_NAME)


def _create_snack_session(customer_id: str, device_label: str = "mobile-web") -> str:
    token = random_token()
    expires_at = utcnow() + timedelta(days=SNACK_SESSION_TTL_DAYS)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO snack_sessions (customer_id, session_token_hash, device_label, expires_at)
                VALUES (%s, %s, %s, %s)
                """,
                (customer_id, hash_token(token), device_label, expires_at),
            )
        connection.commit()
    return token


def _customer_from_snack_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    now = utcnow()
    extended_expiry = now + timedelta(days=SNACK_SESSION_TTL_DAYS)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT customers.*, sessions.id AS session_id
                FROM snack_sessions sessions
                JOIN snack_customers customers ON customers.id = sessions.customer_id
                WHERE sessions.session_token_hash = %s
                  AND sessions.revoked_at IS NULL
                  AND sessions.expires_at > NOW()
                """,
                (hash_token(token),),
            )
            row = cursor.fetchone()
            if not row:
                return None
            cursor.execute(
                """
                UPDATE snack_sessions
                SET last_seen_at = %s,
                    expires_at = %s
                WHERE id = %s
                """,
                (now, extended_expiry, row["session_id"]),
            )
        connection.commit()
    return row


def snack_session_context_from_request(request: Request) -> SnackSessionContext:
    token = request.cookies.get(SNACK_SESSION_COOKIE_NAME)
    customer = _customer_from_snack_token(token)
    return SnackSessionContext(token=token, customer=customer)


def snack_login_with_email(email: str, name: str | None = None) -> dict[str, Any]:
    normalised_email = _normalise_email(email)
    if not normalised_email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email is required.")

    clean_name = str(name or "").strip() or normalised_email.split("@")[0]
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO snack_customers (email, name, auth_provider, is_guest, created_at, last_login_at)
                VALUES (%s, %s, 'email', FALSE, NOW(), NOW())
                ON CONFLICT (email)
                DO UPDATE SET
                    name = EXCLUDED.name,
                    auth_provider = 'email',
                    is_guest = FALSE,
                    last_login_at = NOW(),
                    updated_at = NOW()
                RETURNING *
                """,
                (normalised_email, clean_name),
            )
            customer = cursor.fetchone()
        connection.commit()

    if not customer:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Unable to sign in.")
    return customer


def snack_login_with_social(
    auth_provider: str,
    provider_user_id: str,
    email: str | None,
    name: str | None = None,
) -> dict[str, Any]:
    provider = str(auth_provider or "").strip().lower()
    provider_id = str(provider_user_id or "").strip()
    if not provider or not provider_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid social login payload.")

    normalised_email = _normalise_email(email)
    if not normalised_email:
        normalised_email = f"{provider}-{provider_id}@snackccountancy.local"
    clean_name = str(name or "").strip() or normalised_email.split("@")[0]

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO snack_customers (email, name, auth_provider, provider_user_id, is_guest, created_at, last_login_at)
                VALUES (%s, %s, %s, %s, FALSE, NOW(), NOW())
                ON CONFLICT (email)
                DO UPDATE SET
                    name = COALESCE(NULLIF(EXCLUDED.name, ''), snack_customers.name),
                    auth_provider = EXCLUDED.auth_provider,
                    provider_user_id = EXCLUDED.provider_user_id,
                    is_guest = FALSE,
                    last_login_at = NOW(),
                    updated_at = NOW()
                RETURNING *
                """,
                (normalised_email, clean_name, provider, provider_id),
            )
            customer = cursor.fetchone()
        connection.commit()

    if not customer:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Unable to complete social sign in.")
    return customer


def snack_oauth_authorize_url(provider: str, state_token: str) -> str:
    settings = get_settings()
    provider_key = str(provider or "").strip().lower()
    if provider_key == "google":
        if not settings.snack_google_client_id or not settings.snack_google_redirect_uri:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Google OAuth is not configured.")
        query = urlencode(
            {
                "client_id": settings.snack_google_client_id,
                "redirect_uri": settings.snack_google_redirect_uri,
                "response_type": "code",
                "scope": "openid email profile",
                "access_type": "offline",
                "prompt": "select_account",
                "state": state_token,
            }
        )
        return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"

    if provider_key == "facebook":
        if not settings.snack_facebook_client_id or not settings.snack_facebook_redirect_uri:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Facebook OAuth is not configured.")
        query = urlencode(
            {
                "client_id": settings.snack_facebook_client_id,
                "redirect_uri": settings.snack_facebook_redirect_uri,
                "response_type": "code",
                "scope": "email,public_profile",
                "state": state_token,
            }
        )
        return f"https://www.facebook.com/v19.0/dialog/oauth?{query}"

    if provider_key == "apple":
        if not settings.snack_apple_client_id or not settings.snack_apple_redirect_uri:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Apple OAuth is not configured.")
        query = urlencode(
            {
                "client_id": settings.snack_apple_client_id,
                "redirect_uri": settings.snack_apple_redirect_uri,
                "response_type": "code",
                "response_mode": "form_post",
                "scope": "name email",
                "state": state_token,
            }
        )
        return f"https://appleid.apple.com/auth/authorize?{query}"

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported OAuth provider.")


def _decode_jwt_payload_without_verification(token: str) -> dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8")
        return json.loads(decoded)
    except Exception:
        return {}


def snack_oauth_exchange_code(provider: str, code: str) -> dict[str, Any]:
    settings = get_settings()
    provider_key = str(provider or "").strip().lower()
    if not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing OAuth code.")

    with httpx.Client(timeout=20.0) as client:
        if provider_key == "google":
            if not settings.snack_google_client_id or not settings.snack_google_client_secret or not settings.snack_google_redirect_uri:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Google OAuth is not configured.")
            token_response = client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": settings.snack_google_client_id,
                    "client_secret": settings.snack_google_client_secret,
                    "redirect_uri": settings.snack_google_redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            if token_response.status_code >= 400:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google token exchange failed.")
            access_token = token_response.json().get("access_token")
            user_response = client.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if user_response.status_code >= 400:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google user profile fetch failed.")
            user = user_response.json()
            return {
                "provider": "google",
                "provider_user_id": str(user.get("sub") or ""),
                "email": user.get("email"),
                "name": user.get("name"),
            }

        if provider_key == "facebook":
            if not settings.snack_facebook_client_id or not settings.snack_facebook_client_secret or not settings.snack_facebook_redirect_uri:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Facebook OAuth is not configured.")
            token_response = client.get(
                "https://graph.facebook.com/v19.0/oauth/access_token",
                params={
                    "client_id": settings.snack_facebook_client_id,
                    "client_secret": settings.snack_facebook_client_secret,
                    "redirect_uri": settings.snack_facebook_redirect_uri,
                    "code": code,
                },
            )
            if token_response.status_code >= 400:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Facebook token exchange failed.")
            access_token = token_response.json().get("access_token")
            user_response = client.get(
                "https://graph.facebook.com/me",
                params={"fields": "id,name,email", "access_token": access_token},
            )
            if user_response.status_code >= 400:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Facebook user profile fetch failed.")
            user = user_response.json()
            return {
                "provider": "facebook",
                "provider_user_id": str(user.get("id") or ""),
                "email": user.get("email"),
                "name": user.get("name"),
            }

        if provider_key == "apple":
            if not settings.snack_apple_client_id or not settings.snack_apple_client_secret or not settings.snack_apple_redirect_uri:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Apple OAuth is not configured.")
            token_response = client.post(
                "https://appleid.apple.com/auth/token",
                data={
                    "client_id": settings.snack_apple_client_id,
                    "client_secret": settings.snack_apple_client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": settings.snack_apple_redirect_uri,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if token_response.status_code >= 400:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Apple token exchange failed.")
            payload = token_response.json()
            identity_payload = _decode_jwt_payload_without_verification(str(payload.get("id_token") or ""))
            return {
                "provider": "apple",
                "provider_user_id": str(identity_payload.get("sub") or ""),
                "email": identity_payload.get("email"),
                "name": None,
            }

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported OAuth provider.")


def snack_logout(token: str | None) -> None:
    if not token:
        return
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE snack_sessions
                SET revoked_at = NOW()
                WHERE session_token_hash = %s
                """,
                (hash_token(token),),
            )
        connection.commit()


def _seed_default_products() -> None:
    defaults = [
        ("can_of_pop", "Can of Pop", "Standard chilled can.", "standard_can", 100, 1),
        ("premium_can_of_pop", "Premium Can of Pop", "Premium branded can.", "premium_can", 169, 2),
        ("coffee_or_hot_chocolate", "Coffees or Hot Chocolate", "Fresh hot drinks station.", "hot_drink", 299, 3),
        ("noodle_snackpot", "Noodle Snackpot", "Quick hot noodle pot snack.", "snackpot", 350, 4),
        ("ice_lollys", "Ice Lollys", "Frozen sweet treat selection.", "ice_lolly", 185, 5),
    ]
    with get_connection() as connection:
        with connection.cursor() as cursor:
            for sku, name, description, category, price_pence, sort_order in defaults:
                cursor.execute(
                    """
                    INSERT INTO snack_products (sku, name, description, category, price_pence, active, sort_order)
                    VALUES (%s, %s, %s, %s, %s, TRUE, %s)
                    ON CONFLICT (sku)
                    DO UPDATE SET
                        name = EXCLUDED.name,
                        description = EXCLUDED.description,
                        category = EXCLUDED.category,
                        price_pence = EXCLUDED.price_pence,
                        sort_order = EXCLUDED.sort_order,
                        updated_at = NOW()
                    """,
                    (sku, name, description, category, price_pence, sort_order),
                )
        connection.commit()


def snack_products_payload() -> dict[str, Any]:
    _seed_default_products()
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, sku, name, description, category, price_pence, active, sort_order
                FROM snack_products
                WHERE active = TRUE
                ORDER BY sort_order ASC, name ASC
                """
            )
            products = cursor.fetchall() or []
        connection.commit()

    return {
        "products": [
            {
                "id": str(product["id"]),
                "sku": product["sku"],
                "name": product["name"],
                "description": product.get("description") or "",
                "category": product.get("category") or "",
                "price_pence": _to_int(product.get("price_pence")),
                "price_gbp": _money(_to_int(product.get("price_pence"))),
            }
            for product in products
        ]
    }


def _basket_items_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list) or not raw_items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Basket items are required.")

    cleaned: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        sku = str(item.get("sku") or "").strip()
        quantity = _to_int(item.get("quantity"), 0)
        if not sku or quantity <= 0:
            continue
        if quantity > 100:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Quantity too large for {sku}.")
        cleaned.append({"sku": sku, "quantity": quantity})

    if not cleaned:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No valid basket items were provided.")
    return cleaned


def _basket_product_rows_by_sku(skus: list[str]) -> dict[str, dict[str, Any]]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, sku, name, description, category, price_pence, active
                FROM snack_products
                WHERE sku = ANY(%s)
                  AND active = TRUE
                """,
                (skus,),
            )
            rows = cursor.fetchall() or []
        connection.commit()
    return {row["sku"]: row for row in rows}


def _customer_weekly_cans_before(customer_id: str, week_start: date) -> int:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COALESCE(SUM(items.quantity), 0) AS total
                FROM snack_orders orders
                JOIN snack_order_items items ON items.order_id = orders.id
                WHERE orders.customer_id = %s
                  AND orders.status = 'paid'
                  AND orders.week_start_date = %s
                """,
                (customer_id, week_start),
            )
            row = cursor.fetchone()
        connection.commit()
    return _to_int((row or {}).get("total"), 0)


def _customer_completed_order_count(customer_id: str) -> int:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS total
                FROM snack_orders
                WHERE customer_id = %s
                  AND status = 'paid'
                """,
                (customer_id,),
            )
            row = cursor.fetchone()
        connection.commit()
    return _to_int((row or {}).get("total"), 0)


def calculate_snackccountancy_basket(payload: dict[str, Any], customer: dict[str, Any] | None = None) -> dict[str, Any]:
    _seed_default_products()
    settings = get_settings()
    items = _basket_items_from_payload(payload)
    sku_rows = _basket_product_rows_by_sku([item["sku"] for item in items])

    missing = [item["sku"] for item in items if item["sku"] not in sku_rows]
    if missing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown or inactive products: {', '.join(missing)}")

    week_start_utc, _, week_start_date = _current_uk_week_bounds()
    _ = week_start_utc

    customer_id = (customer or {}).get("id")
    rewards_allowed = bool(customer_id and not customer.get("is_guest"))

    weekly_cans_before = _customer_weekly_cans_before(str(customer_id), week_start_date) if rewards_allowed else 0
    completed_orders_before = _customer_completed_order_count(str(customer_id)) if rewards_allowed else 0
    order_number = completed_orders_before + 1
    milestone_reward_active = rewards_allowed and (order_number % max(settings.snack_milestone_interval_orders, 1) == 0)

    running_weekly_can_number = weekly_cans_before
    subtotal_pence = 0
    weekly_discount_pence = 0
    total_item_count = 0
    lines: list[dict[str, Any]] = []

    weekly_threshold = max(settings.snack_weekly_threshold_cans, 0)
    weekly_rate = max(settings.snack_weekly_discount_percent, 0) / Decimal("100")

    for item in items:
        row = sku_rows[item["sku"]]
        unit = _to_int(row.get("price_pence"), 0)
        quantity = item["quantity"]
        total_item_count += quantity

        line_subtotal = unit * quantity
        line_weekly_discount = 0
        full_price_qty = 0
        weekly_discount_qty = 0
        per_unit_discount = int((Decimal(unit) * weekly_rate).quantize(Decimal("1")))

        is_can = "can" in str(row.get("category") or "").lower() or "can" in str(row.get("sku") or "").lower()

        for _i in range(quantity):
            if rewards_allowed and is_can:
                running_weekly_can_number += 1
                if running_weekly_can_number > weekly_threshold:
                    line_weekly_discount += per_unit_discount
                    weekly_discount_qty += 1
                else:
                    full_price_qty += 1
            else:
                full_price_qty += 1

        subtotal_pence += line_subtotal
        weekly_discount_pence += line_weekly_discount

        lines.append(
            {
                "product_id": str(row["id"]),
                "sku": row["sku"],
                "name": row["name"],
                "quantity": quantity,
                "unit_price_pence": unit,
                "line_subtotal_pence": line_subtotal,
                "full_price_quantity": full_price_qty,
                "weekly_discount_quantity": weekly_discount_qty,
                "weekly_discount_pence": line_weekly_discount,
            }
        )

    after_weekly_pence = subtotal_pence - weekly_discount_pence
    milestone_discount_pence = 0
    if milestone_reward_active:
        milestone_rate = max(settings.snack_milestone_discount_percent, 0) / Decimal("100")
        milestone_discount_pence = int((Decimal(after_weekly_pence) * milestone_rate).quantize(Decimal("1")))

    total_paid_pence = max(after_weekly_pence - milestone_discount_pence, 0)
    total_discount_pence = weekly_discount_pence + milestone_discount_pence

    return {
        "currency": "gbp",
        "week_start_date": week_start_date.isoformat(),
        "weekly_cans_before": weekly_cans_before,
        "weekly_cans_after": running_weekly_can_number,
        "order_number": order_number,
        "milestone_reward_active": milestone_reward_active,
        "double_reward_active": milestone_reward_active and weekly_discount_pence > 0,
        "subtotal_pence": subtotal_pence,
        "weekly_discount_pence": weekly_discount_pence,
        "milestone_discount_pence": milestone_discount_pence,
        "total_discount_pence": total_discount_pence,
        "total_paid_pence": total_paid_pence,
        "item_count": total_item_count,
        "lines": lines,
        "display": {
            "subtotal_gbp": _money(subtotal_pence),
            "weekly_discount_gbp": _money(weekly_discount_pence),
            "milestone_discount_gbp": _money(milestone_discount_pence),
            "total_discount_gbp": _money(total_discount_pence),
            "total_paid_gbp": _money(total_paid_pence),
        },
    }


def snack_customer_summary(customer: dict[str, Any] | None) -> dict[str, Any]:
    if not customer:
        return {"authenticated": False, "customer": None, "rewards": None}

    week_start_utc, week_end_utc, week_start_date = _current_uk_week_bounds()
    _ = week_start_utc
    _ = week_end_utc

    customer_id = str(customer["id"])
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COALESCE(SUM(items.quantity), 0) AS weekly_cans
                FROM snack_orders orders
                JOIN snack_order_items items ON items.order_id = orders.id
                WHERE orders.customer_id = %s
                  AND orders.status = 'paid'
                  AND orders.week_start_date = %s
                """,
                (customer_id, week_start_date),
            )
            weekly = cursor.fetchone() or {}

            cursor.execute(
                """
                SELECT COUNT(*) AS completed_orders
                FROM snack_orders
                WHERE customer_id = %s
                  AND status = 'paid'
                """,
                (customer_id,),
            )
            completed = cursor.fetchone() or {}
        connection.commit()

    weekly_cans = _to_int(weekly.get("weekly_cans"), 0)
    completed_orders = _to_int(completed.get("completed_orders"), 0)

    return {
        "authenticated": True,
        "customer": {
            "id": customer_id,
            "name": customer.get("name") or "",
            "email": customer.get("email") or "",
            "auth_provider": customer.get("auth_provider") or "email",
            "is_guest": bool(customer.get("is_guest")),
            "total_orders": _to_int(customer.get("total_orders"), completed_orders),
            "total_cans": _to_int(customer.get("total_cans"), 0),
            "lifetime_spend_pence": _to_int(customer.get("lifetime_spend_pence"), 0),
            "lifetime_savings_pence": _to_int(customer.get("lifetime_savings_pence"), 0),
        },
        "rewards": {
            "week_start_date": week_start_date.isoformat(),
            "weekly_cans": weekly_cans,
            "weekly_reward_active": weekly_cans > max(get_settings().snack_weekly_threshold_cans, 0),
            "orders_completed": completed_orders,
            "next_milestone_in": max(get_settings().snack_milestone_interval_orders - (completed_orders % max(get_settings().snack_milestone_interval_orders, 1)), 1),
        },
    }


def snack_orders_for_customer(customer_id: str, limit: int = 50) -> dict[str, Any]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, order_number, status, subtotal_pence, weekly_discount_pence,
                       milestone_discount_pence, total_paid_pence, currency,
                       is_10th_order_reward, double_reward_active, created_at, paid_at
                FROM snack_orders
                WHERE customer_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (customer_id, limit),
            )
            orders = cursor.fetchall() or []
        connection.commit()

    return {
        "orders": [
            {
                "id": str(order["id"]),
                "order_number": order.get("order_number") or "",
                "status": order.get("status"),
                "subtotal_pence": _to_int(order.get("subtotal_pence")),
                "weekly_discount_pence": _to_int(order.get("weekly_discount_pence")),
                "milestone_discount_pence": _to_int(order.get("milestone_discount_pence")),
                "total_paid_pence": _to_int(order.get("total_paid_pence")),
                "currency": order.get("currency") or "gbp",
                "is_10th_order_reward": bool(order.get("is_10th_order_reward")),
                "double_reward_active": bool(order.get("double_reward_active")),
                "created_at": order.get("created_at"),
                "paid_at": order.get("paid_at"),
            }
            for order in orders
        ]
    }


def _create_order_record(
    basket: dict[str, Any],
    customer: dict[str, Any] | None,
    guest_email: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO snack_order_number_sequence DEFAULT VALUES RETURNING id")
            current = cursor.fetchone() or {}
            next_id = _to_int(current.get("id"), 0)
            order_number = f"SNK-{next_id:06d}"

            cursor.execute(
                """
                INSERT INTO snack_orders (
                    order_number,
                    customer_id,
                    guest_email,
                    status,
                    subtotal_pence,
                    weekly_discount_pence,
                    milestone_discount_pence,
                    total_discount_pence,
                    total_paid_pence,
                    currency,
                    week_start_date,
                    is_10th_order_reward,
                    double_reward_active
                )
                VALUES (
                    %s, %s, %s, 'pending_payment', %s, %s, %s, %s, %s, 'gbp', %s, %s, %s
                )
                RETURNING *
                """,
                (
                    order_number,
                    (customer or {}).get("id"),
                    guest_email,
                    basket["subtotal_pence"],
                    basket["weekly_discount_pence"],
                    basket["milestone_discount_pence"],
                    basket["total_discount_pence"],
                    basket["total_paid_pence"],
                    basket["week_start_date"],
                    basket["milestone_reward_active"],
                    basket["double_reward_active"],
                ),
            )
            order = cursor.fetchone()

            for line in basket["lines"]:
                line_after_weekly = line["line_subtotal_pence"] - line["weekly_discount_pence"]
                if basket["milestone_discount_pence"] > 0 and basket["subtotal_pence"] > 0:
                    ratio = Decimal(line_after_weekly) / Decimal(max(basket["subtotal_pence"] - basket["weekly_discount_pence"], 1))
                    line_milestone_discount = int((Decimal(basket["milestone_discount_pence"]) * ratio).quantize(Decimal("1")))
                else:
                    line_milestone_discount = 0

                final_line_total = max(line_after_weekly - line_milestone_discount, 0)

                cursor.execute(
                    """
                    INSERT INTO snack_order_items (
                        order_id,
                        product_id,
                        product_sku,
                        product_name,
                        quantity,
                        unit_price_pence,
                        full_price_quantity,
                        weekly_discount_quantity,
                        weekly_discount_pence,
                        milestone_discount_pence,
                        final_line_total_pence
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        order["id"],
                        line["product_id"],
                        line["sku"],
                        line["name"],
                        line["quantity"],
                        line["unit_price_pence"],
                        line["full_price_quantity"],
                        line["weekly_discount_quantity"],
                        line["weekly_discount_pence"],
                        line_milestone_discount,
                        final_line_total,
                    ),
                )

            cursor.execute(
                """
                SELECT *
                FROM snack_order_items
                WHERE order_id = %s
                ORDER BY created_at ASC
                """,
                (order["id"],),
            )
            items = cursor.fetchall() or []
        connection.commit()
    return order, items


def _stripe_create_payment_intent(amount_pence: int, order: dict[str, Any], customer: dict[str, Any] | None, guest_email: str | None) -> dict[str, Any]:
    settings = get_settings()
    if not settings.stripe_secret_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stripe is not configured yet (missing STRIPE_SECRET_KEY).",
        )

    metadata = {
        "snack_order_id": str(order["id"]),
        "snack_order_number": order["order_number"],
    }
    if customer and customer.get("id"):
        metadata["snack_customer_id"] = str(customer["id"])

    payload = {
        "amount": str(amount_pence),
        "currency": "gbp",
        "automatic_payment_methods[enabled]": "true",
        "description": f"Snackccountancy order {order['order_number']}",
    }

    for key, value in metadata.items():
        payload[f"metadata[{key}]"] = value

    receipt_email = (customer or {}).get("email") or guest_email
    if receipt_email:
        payload["receipt_email"] = str(receipt_email)

    with httpx.Client(timeout=20.0) as client:
        response = client.post(
            "https://api.stripe.com/v1/payment_intents",
            data=payload,
            headers={"Authorization": f"Bearer {settings.stripe_secret_key}"},
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": "Stripe PaymentIntent creation failed.",
                "status": response.status_code,
                "body": response.text,
            },
        )

    data = response.json()
    if not data.get("id") or not data.get("client_secret"):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Stripe response missing id/client_secret.")
    return data


def _stripe_retrieve_payment_intent(payment_intent_id: str) -> dict[str, Any]:
    settings = get_settings()
    if not settings.stripe_secret_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stripe is not configured yet (missing STRIPE_SECRET_KEY).",
        )
    with httpx.Client(timeout=20.0) as client:
        response = client.get(
            f"https://api.stripe.com/v1/payment_intents/{payment_intent_id}",
            params={"expand[]": ["charges.data.billing_details"]},
            headers={"Authorization": f"Bearer {settings.stripe_secret_key}"},
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unable to verify payment with Stripe.")
    return response.json()


def _email_from_payment_intent_object(payment_intent: dict[str, Any]) -> str:
    direct_email = _normalise_email(payment_intent.get("receipt_email"))
    if direct_email:
        return direct_email

    charges = ((payment_intent.get("charges") or {}).get("data") or [])
    for charge in charges:
        billing_email = _normalise_email(((charge.get("billing_details") or {}).get("email")))
        if billing_email:
            return billing_email
    return ""


def _ensure_customer_for_paid_order(
    order: dict[str, Any],
    payment_intent: dict[str, Any] | None = None,
    fallback_email: str | None = None,
    fallback_name: str | None = None,
) -> dict[str, Any] | None:
    if order.get("customer_id"):
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM snack_customers WHERE id = %s LIMIT 1", (order["customer_id"],))
                customer = cursor.fetchone()
            connection.commit()
        return customer

    email = _normalise_email(order.get("guest_email"))
    if not email and payment_intent:
        email = _email_from_payment_intent_object(payment_intent)
    if not email:
        email = _normalise_email(fallback_email)
    if not email:
        return None

    preferred_name = str(fallback_name or "").strip() or email.split("@")[0]
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO snack_customers (email, name, auth_provider, is_guest, created_at, last_login_at, updated_at)
                VALUES (%s, %s, 'wallet', FALSE, NOW(), NOW(), NOW())
                ON CONFLICT (email)
                DO UPDATE SET
                    is_guest = FALSE,
                    auth_provider = CASE
                        WHEN snack_customers.auth_provider IN ('', 'guest') THEN 'wallet'
                        ELSE snack_customers.auth_provider
                    END,
                    last_login_at = NOW(),
                    updated_at = NOW()
                RETURNING *
                """,
                (email, preferred_name),
            )
            customer = cursor.fetchone()
            cursor.execute(
                """
                UPDATE snack_orders
                SET customer_id = %s,
                    guest_email = COALESCE(NULLIF(guest_email, ''), %s),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (customer["id"], email, order["id"]),
            )
        connection.commit()
    return customer


def snack_create_payment(payload: dict[str, Any], customer: dict[str, Any] | None) -> dict[str, Any]:
    basket = calculate_snackccountancy_basket(payload, customer=customer)
    guest_email = _normalise_email((payload or {}).get("guest_email")) or None
    if not customer and not guest_email:
        guest_email = None

    order, order_items = _create_order_record(basket=basket, customer=customer, guest_email=guest_email)

    payment_intent = _stripe_create_payment_intent(
        amount_pence=basket["total_paid_pence"],
        order=order,
        customer=customer,
        guest_email=guest_email,
    )

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE snack_orders
                SET stripe_payment_intent_id = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (payment_intent["id"], order["id"]),
            )
        connection.commit()

    return {
        "order_id": str(order["id"]),
        "order_number": order["order_number"],
        "stripe_payment_intent_id": payment_intent["id"],
        "client_secret": payment_intent["client_secret"],
        "publishable_key": get_settings().stripe_publishable_key,
        "amount_pence": basket["total_paid_pence"],
        "currency": "gbp",
        "basket": basket,
        "items": [
            {
                "sku": item["product_sku"],
                "name": item["product_name"],
                "quantity": item["quantity"],
                "final_line_total_pence": _to_int(item["final_line_total_pence"]),
            }
            for item in order_items
        ],
    }


def _webhook_signature_is_valid(body: bytes, signature_header: str | None, secret: str | None) -> bool:
    if not signature_header or not secret:
        return False

    fields = {}
    for entry in signature_header.split(","):
        if "=" not in entry:
            continue
        key, value = entry.split("=", 1)
        fields[key.strip()] = value.strip()

    timestamp = fields.get("t")
    signature = fields.get("v1")
    if not timestamp or not signature:
        return False

    signed_payload = f"{timestamp}.{body.decode('utf-8')}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _mark_order_paid_by_payment_intent(payment_intent_id: str, amount_received: int | None = None) -> dict[str, Any] | None:
    payment_intent = _stripe_retrieve_payment_intent(payment_intent_id)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM snack_orders
                WHERE stripe_payment_intent_id = %s
                LIMIT 1
                """,
                (payment_intent_id,),
            )
            order = cursor.fetchone()
            if not order:
                connection.commit()
                return None

            if order["status"] == "paid":
                connection.commit()
                return order

            final_paid = _to_int(amount_received, _to_int(order.get("total_paid_pence"), 0))
            cursor.execute(
                """
                UPDATE snack_orders
                SET status = 'paid',
                    paid_at = NOW(),
                    total_paid_pence = %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (final_paid, order["id"]),
            )
            order = cursor.fetchone()

            cursor.execute(
                """
                SELECT
                    COALESCE(SUM(quantity), 0) AS cans,
                    COALESCE(SUM(weekly_discount_quantity), 0) AS weekly_discount_cans
                FROM snack_order_items
                WHERE order_id = %s
                """,
                (order["id"],),
            )
            totals = cursor.fetchone() or {}

            if order.get("customer_id"):
                cursor.execute(
                    """
                    UPDATE snack_customers
                    SET total_orders = COALESCE(total_orders, 0) + 1,
                        total_cans = COALESCE(total_cans, 0) + %s,
                        lifetime_spend_pence = COALESCE(lifetime_spend_pence, 0) + %s,
                        lifetime_savings_pence = COALESCE(lifetime_savings_pence, 0) + %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        _to_int(totals.get("cans"), 0),
                        _to_int(order.get("total_paid_pence"), 0),
                        _to_int(order.get("total_discount_pence"), 0),
                        order["customer_id"],
                    ),
                )

                cursor.execute(
                    """
                    INSERT INTO snack_loyalty_weeks (
                        customer_id,
                        week_start_date,
                        cans_purchased_count,
                        weekly_discount_cans_count,
                        weekly_savings_pence
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (customer_id, week_start_date)
                    DO UPDATE SET
                        cans_purchased_count = snack_loyalty_weeks.cans_purchased_count + EXCLUDED.cans_purchased_count,
                        weekly_discount_cans_count = snack_loyalty_weeks.weekly_discount_cans_count + EXCLUDED.weekly_discount_cans_count,
                        weekly_savings_pence = snack_loyalty_weeks.weekly_savings_pence + EXCLUDED.weekly_savings_pence,
                        updated_at = NOW()
                    """,
                    (
                        order["customer_id"],
                        order["week_start_date"],
                        _to_int(totals.get("cans"), 0),
                        _to_int(totals.get("weekly_discount_cans"), 0),
                        _to_int(order.get("weekly_discount_pence"), 0),
                    ),
                )

        connection.commit()
    _ensure_customer_for_paid_order(order, payment_intent=payment_intent)
    return order


def _mark_order_failed_by_payment_intent(payment_intent_id: str) -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE snack_orders
                SET status = 'failed',
                    updated_at = NOW()
                WHERE stripe_payment_intent_id = %s
                  AND status IN ('pending_payment', 'draft')
                """,
                (payment_intent_id,),
            )
        connection.commit()


def snack_handle_stripe_webhook(body: bytes, signature: str | None) -> dict[str, Any]:
    settings = get_settings()
    if not settings.stripe_snack_webhook_secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Stripe webhook secret is not configured.")

    if not _webhook_signature_is_valid(body, signature, settings.stripe_snack_webhook_secret):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Stripe signature.")

    try:
        event = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid webhook JSON.") from exc

    event_type = str(event.get("type") or "")
    obj = ((event.get("data") or {}).get("object") or {})
    payment_intent_id = str(obj.get("id") or "")

    if event_type == "payment_intent.succeeded" and payment_intent_id:
        amount_received = _to_int(obj.get("amount_received"), None)
        order = _mark_order_paid_by_payment_intent(payment_intent_id, amount_received=amount_received)
        return {"status": "ok", "event": event_type, "order_id": str((order or {}).get("id") or "")}

    if event_type == "payment_intent.payment_failed" and payment_intent_id:
        _mark_order_failed_by_payment_intent(payment_intent_id)
        return {"status": "ok", "event": event_type}

    return {"status": "ignored", "event": event_type}


def snack_claim_paid_order_session(
    order_number: str,
    payment_intent_id: str,
    email: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    if not order_number or not payment_intent_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="order_number and payment_intent_id are required.")

    payment_intent = _stripe_retrieve_payment_intent(payment_intent_id)
    if str(payment_intent.get("status") or "") != "succeeded":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Payment is not confirmed yet.")

    metadata = payment_intent.get("metadata") or {}
    if metadata.get("snack_order_number") and metadata.get("snack_order_number") != order_number:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payment metadata does not match order.")

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM snack_orders
                WHERE order_number = %s
                  AND stripe_payment_intent_id = %s
                LIMIT 1
                """,
                (order_number, payment_intent_id),
            )
            order = cursor.fetchone()
        connection.commit()

    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found for this payment.")

    customer = _ensure_customer_for_paid_order(
        order,
        payment_intent=payment_intent,
        fallback_email=email,
        fallback_name=name,
    )
    if not customer:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No customer email was available to create an account.")

    session_token = _create_snack_session(str(customer["id"]), device_label="wallet-autologin")
    return {
        "status": "ok",
        "session_token": session_token,
        "customer": {
            "id": str(customer["id"]),
            "email": customer.get("email") or "",
            "name": customer.get("name") or "",
        },
    }


def snack_dashboard_payload() -> dict[str, Any]:
    now_uk = datetime.now(UK_TZ)
    today_start_uk = datetime.combine(now_uk.date(), time.min, tzinfo=UK_TZ)
    week_start_uk = datetime.combine(_current_uk_week_start(now_uk.date()), time.min, tzinfo=UK_TZ)

    today_start_utc = today_start_uk.astimezone(timezone.utc)
    week_start_utc = week_start_uk.astimezone(timezone.utc)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN paid_at >= %s THEN total_paid_pence ELSE 0 END), 0) AS today_revenue,
                    COALESCE(SUM(CASE WHEN paid_at >= %s THEN 1 ELSE 0 END), 0) AS today_orders,
                    COALESCE(SUM(CASE WHEN paid_at >= %s THEN total_paid_pence ELSE 0 END), 0) AS week_revenue,
                    COALESCE(SUM(total_discount_pence), 0) AS total_discounts,
                    COALESCE(SUM(CASE WHEN is_10th_order_reward THEN 1 ELSE 0 END), 0) AS milestone_rewards,
                    COALESCE(SUM(CASE WHEN weekly_discount_pence > 0 THEN 1 ELSE 0 END), 0) AS weekly_rewards,
                    COALESCE(SUM(CASE WHEN double_reward_active THEN 1 ELSE 0 END), 0) AS double_rewards,
                    COALESCE(AVG(total_paid_pence), 0) AS avg_order_value,
                    COALESCE(SUM(CASE WHEN customer_id IS NULL THEN 1 ELSE 0 END), 0) AS guest_orders,
                    COALESCE(SUM(CASE WHEN customer_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS logged_in_orders
                FROM snack_orders
                WHERE status = 'paid'
                """,
                (today_start_utc, today_start_utc, week_start_utc),
            )
            summary = cursor.fetchone() or {}

            cursor.execute(
                """
                SELECT COALESCE(SUM(items.quantity), 0) AS today_cans
                FROM snack_orders orders
                JOIN snack_order_items items ON items.order_id = orders.id
                WHERE orders.status = 'paid'
                  AND orders.paid_at >= %s
                """,
                (today_start_utc,),
            )
            today_cans = cursor.fetchone() or {}

            cursor.execute(
                """
                SELECT COALESCE(SUM(items.quantity), 0) AS week_cans
                FROM snack_orders orders
                JOIN snack_order_items items ON items.order_id = orders.id
                WHERE orders.status = 'paid'
                  AND orders.paid_at >= %s
                """,
                (week_start_utc,),
            )
            week_cans = cursor.fetchone() or {}

            cursor.execute(
                """
                SELECT COUNT(*) AS active_loyalty_customers
                FROM snack_customers
                WHERE total_orders > 0
                """
            )
            active_customers = cursor.fetchone() or {}
        connection.commit()

    return {
        "today_revenue_pence": _to_int(summary.get("today_revenue"), 0),
        "today_orders": _to_int(summary.get("today_orders"), 0),
        "today_cans_sold": _to_int(today_cans.get("today_cans"), 0),
        "week_revenue_pence": _to_int(summary.get("week_revenue"), 0),
        "week_cans_sold": _to_int(week_cans.get("week_cans"), 0),
        "active_loyalty_customers": _to_int(active_customers.get("active_loyalty_customers"), 0),
        "guest_orders": _to_int(summary.get("guest_orders"), 0),
        "logged_in_orders": _to_int(summary.get("logged_in_orders"), 0),
        "milestone_rewards_used": _to_int(summary.get("milestone_rewards"), 0),
        "weekly_rewards_used": _to_int(summary.get("weekly_rewards"), 0),
        "double_rewards_used": _to_int(summary.get("double_rewards"), 0),
        "total_discounts_pence": _to_int(summary.get("total_discounts"), 0),
        "average_order_value_pence": _to_int(summary.get("avg_order_value"), 0),
    }


def snack_orders_admin(
    limit: int = 200,
    date_from: str | None = None,
    date_to: str | None = None,
    status_filter: str | None = None,
    search: str | None = None,
    guests_only: bool = False,
    logged_in_only: bool = False,
    weekly_reward_only: bool = False,
    milestone_reward_only: bool = False,
    double_reward_only: bool = False,
) -> dict[str, Any]:
    filters: list[str] = []
    params: list[Any] = []
    if date_from:
        filters.append("orders.created_at >= %s::date")
        params.append(date_from)
    if date_to:
        filters.append("orders.created_at < (%s::date + INTERVAL '1 day')")
        params.append(date_to)
    if status_filter:
        filters.append("orders.status = %s")
        params.append(status_filter)
    if search:
        filters.append("(orders.order_number ILIKE %s OR COALESCE(customers.name, '') ILIKE %s OR COALESCE(customers.email, orders.guest_email, '') ILIKE %s)")
        term = f"%{search.strip()}%"
        params.extend([term, term, term])
    if guests_only:
        filters.append("orders.customer_id IS NULL")
    if logged_in_only:
        filters.append("orders.customer_id IS NOT NULL")
    if weekly_reward_only:
        filters.append("orders.weekly_discount_pence > 0")
    if milestone_reward_only:
        filters.append("orders.is_10th_order_reward = TRUE")
    if double_reward_only:
        filters.append("orders.double_reward_active = TRUE")

    where_clause = " AND ".join(filters)
    where_sql = f"WHERE {where_clause}" if where_clause else ""

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT orders.*, customers.name AS customer_name, customers.email AS customer_email
                FROM snack_orders orders
                LEFT JOIN snack_customers customers ON customers.id = orders.customer_id
                {where_sql}
                ORDER BY orders.created_at DESC
                LIMIT %s
                """,
                (*params, limit),
            )
            orders = cursor.fetchall() or []
        connection.commit()

    return {
        "orders": [
            {
                "id": str(order["id"]),
                "order_number": order.get("order_number"),
                "status": order.get("status"),
                "customer": order.get("customer_name") or order.get("guest_email") or "Guest",
                "customer_email": order.get("customer_email") or order.get("guest_email") or "",
                "subtotal_pence": _to_int(order.get("subtotal_pence"), 0),
                "weekly_discount_pence": _to_int(order.get("weekly_discount_pence"), 0),
                "milestone_discount_pence": _to_int(order.get("milestone_discount_pence"), 0),
                "total_paid_pence": _to_int(order.get("total_paid_pence"), 0),
                "currency": order.get("currency") or "gbp",
                "stripe_payment_intent_id": order.get("stripe_payment_intent_id") or "",
                "is_10th_order_reward": bool(order.get("is_10th_order_reward")),
                "double_reward_active": bool(order.get("double_reward_active")),
                "created_at": order.get("created_at"),
                "paid_at": order.get("paid_at"),
            }
            for order in orders
        ]
    }


def snack_reports_payload(date_from: str | None = None, date_to: str | None = None) -> dict[str, Any]:
    filters = ["status = 'paid'"]
    params: list[Any] = []
    if date_from:
        filters.append("created_at >= %s::date")
        params.append(date_from)
    if date_to:
        filters.append("created_at < (%s::date + INTERVAL '1 day')")
        params.append(date_to)
    where_sql = " AND ".join(filters)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT DATE_TRUNC('day', created_at) AS period_start,
                       COALESCE(SUM(total_paid_pence), 0) AS revenue_pence,
                       COUNT(*) AS orders
                FROM snack_orders
                WHERE {where_sql}
                GROUP BY 1
                ORDER BY 1 DESC
                LIMIT 120
                """,
                params,
            )
            sales_by_day = cursor.fetchall() or []

            cursor.execute(
                f"""
                SELECT DATE_TRUNC('week', created_at) AS period_start,
                       COALESCE(SUM(total_paid_pence), 0) AS revenue_pence,
                       COUNT(*) AS orders
                FROM snack_orders
                WHERE {where_sql}
                GROUP BY 1
                ORDER BY 1 DESC
                LIMIT 52
                """,
                params,
            )
            sales_by_week = cursor.fetchall() or []

            cursor.execute(
                f"""
                SELECT DATE_TRUNC('month', created_at) AS period_start,
                       COALESCE(SUM(total_paid_pence), 0) AS revenue_pence,
                       COUNT(*) AS orders
                FROM snack_orders
                WHERE {where_sql}
                GROUP BY 1
                ORDER BY 1 DESC
                LIMIT 36
                """,
                params,
            )
            sales_by_month = cursor.fetchall() or []

            cursor.execute(
                f"""
                SELECT COALESCE(customers.name, orders.guest_email, 'Guest') AS customer_name,
                       COUNT(*) AS order_count,
                       COALESCE(SUM(orders.total_paid_pence), 0) AS revenue_pence
                FROM snack_orders orders
                LEFT JOIN snack_customers customers ON customers.id = orders.customer_id
                WHERE orders.status = 'paid'
                GROUP BY 1
                ORDER BY revenue_pence DESC
                LIMIT 20
                """
            )
            top_customers = cursor.fetchall() or []

            cursor.execute(
                """
                SELECT items.product_sku,
                       items.product_name,
                       COALESCE(SUM(items.quantity), 0) AS units_sold,
                       COALESCE(SUM(items.final_line_total_pence), 0) AS revenue_pence
                FROM snack_order_items items
                JOIN snack_orders orders ON orders.id = items.order_id
                WHERE orders.status = 'paid'
                GROUP BY items.product_sku, items.product_name
                ORDER BY units_sold DESC, revenue_pence DESC
                LIMIT 20
                """
            )
            top_products = cursor.fetchall() or []

            cursor.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN customer_id IS NULL THEN total_paid_pence ELSE 0 END), 0) AS guest_revenue_pence,
                    COALESCE(SUM(CASE WHEN customer_id IS NOT NULL THEN total_paid_pence ELSE 0 END), 0) AS logged_in_revenue_pence,
                    COALESCE(SUM(CASE WHEN customer_id IS NULL THEN 1 ELSE 0 END), 0) AS guest_orders,
                    COALESCE(SUM(CASE WHEN customer_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS logged_in_orders,
                    COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS failed_payments,
                    COALESCE(SUM(CASE WHEN status = 'refunded' THEN 1 ELSE 0 END), 0) AS refunds,
                    COALESCE(AVG(CASE WHEN status = 'paid' THEN total_paid_pence ELSE NULL END), 0) AS average_order_value_pence,
                    COALESCE(SUM(CASE WHEN weekly_discount_pence > 0 THEN 1 ELSE 0 END), 0) AS weekly_discount_usage,
                    COALESCE(SUM(CASE WHEN is_10th_order_reward THEN 1 ELSE 0 END), 0) AS milestone_discount_usage,
                    COALESCE(SUM(CASE WHEN double_reward_active THEN 1 ELSE 0 END), 0) AS double_reward_usage,
                    COALESCE(SUM(total_discount_pence), 0) AS total_discounts_given_pence
                FROM snack_orders
                """
            )
            summary = cursor.fetchone() or {}
        connection.commit()

    def _period_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "period_start": row.get("period_start"),
                "revenue_pence": _to_int(row.get("revenue_pence"), 0),
                "orders": _to_int(row.get("orders"), 0),
            }
            for row in rows
        ]

    return {
        "sales_by_day": _period_rows(sales_by_day),
        "sales_by_week": _period_rows(sales_by_week),
        "sales_by_month": _period_rows(sales_by_month),
        "top_customers": [
            {
                "customer_name": row.get("customer_name") or "Guest",
                "order_count": _to_int(row.get("order_count"), 0),
                "revenue_pence": _to_int(row.get("revenue_pence"), 0),
            }
            for row in top_customers
        ],
        "top_products": [
            {
                "product_sku": row.get("product_sku") or "",
                "product_name": row.get("product_name") or "",
                "units_sold": _to_int(row.get("units_sold"), 0),
                "revenue_pence": _to_int(row.get("revenue_pence"), 0),
            }
            for row in top_products
        ],
        "summary": {
            "guest_revenue_pence": _to_int(summary.get("guest_revenue_pence"), 0),
            "logged_in_revenue_pence": _to_int(summary.get("logged_in_revenue_pence"), 0),
            "guest_orders": _to_int(summary.get("guest_orders"), 0),
            "logged_in_orders": _to_int(summary.get("logged_in_orders"), 0),
            "failed_payments": _to_int(summary.get("failed_payments"), 0),
            "refunds": _to_int(summary.get("refunds"), 0),
            "average_order_value_pence": _to_int(summary.get("average_order_value_pence"), 0),
            "weekly_discount_usage": _to_int(summary.get("weekly_discount_usage"), 0),
            "milestone_discount_usage": _to_int(summary.get("milestone_discount_usage"), 0),
            "double_reward_usage": _to_int(summary.get("double_reward_usage"), 0),
            "total_discounts_given_pence": _to_int(summary.get("total_discounts_given_pence"), 0),
        },
    }


def snack_order_items_export_rows() -> list[dict[str, Any]]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT orders.order_number,
                       orders.status,
                       orders.created_at,
                       items.product_sku,
                       items.product_name,
                       items.quantity,
                       items.unit_price_pence,
                       items.weekly_discount_pence,
                       items.milestone_discount_pence,
                       items.final_line_total_pence
                FROM snack_order_items items
                JOIN snack_orders orders ON orders.id = items.order_id
                ORDER BY orders.created_at DESC, items.created_at ASC
                """
            )
            rows = cursor.fetchall() or []
        connection.commit()
    return rows


def snack_product_sales_export_rows() -> list[dict[str, Any]]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT items.product_sku,
                       items.product_name,
                       COALESCE(SUM(items.quantity), 0) AS units_sold,
                       COALESCE(SUM(items.final_line_total_pence), 0) AS revenue_pence,
                       COALESCE(SUM(items.weekly_discount_pence + items.milestone_discount_pence), 0) AS discounts_pence
                FROM snack_order_items items
                JOIN snack_orders orders ON orders.id = items.order_id
                WHERE orders.status = 'paid'
                GROUP BY items.product_sku, items.product_name
                ORDER BY units_sold DESC, revenue_pence DESC
                """
            )
            rows = cursor.fetchall() or []
        connection.commit()
    return rows


def snack_discounts_export_rows() -> list[dict[str, Any]]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT order_number,
                       created_at,
                       status,
                       weekly_discount_pence,
                       milestone_discount_pence,
                       total_discount_pence,
                       is_10th_order_reward,
                       double_reward_active
                FROM snack_orders
                ORDER BY created_at DESC
                """
            )
            rows = cursor.fetchall() or []
        connection.commit()
    return rows


def snack_customers_admin(limit: int = 200) -> dict[str, Any]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM snack_customers
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            customers = cursor.fetchall() or []
        connection.commit()

    return {
        "customers": [
            {
                "id": str(customer["id"]),
                "name": customer.get("name") or "",
                "email": customer.get("email") or "",
                "auth_provider": customer.get("auth_provider") or "",
                "total_orders": _to_int(customer.get("total_orders"), 0),
                "total_cans": _to_int(customer.get("total_cans"), 0),
                "lifetime_spend_pence": _to_int(customer.get("lifetime_spend_pence"), 0),
                "lifetime_savings_pence": _to_int(customer.get("lifetime_savings_pence"), 0),
                "created_at": customer.get("created_at"),
                "last_login_at": customer.get("last_login_at"),
            }
            for customer in customers
        ]
    }


def snack_products_admin_upsert(payload: dict[str, Any]) -> dict[str, Any]:
    product_id = str(payload.get("id") or "").strip()
    sku = str(payload.get("sku") or "").strip()
    name = str(payload.get("name") or "").strip()
    description = str(payload.get("description") or "").strip()
    category = str(payload.get("category") or "general").strip()
    price_pence = _to_int(payload.get("price_pence"), -1)
    active = bool(payload.get("active", True))
    sort_order = _to_int(payload.get("sort_order"), 999)

    if price_pence < 0 and not product_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="price_pence must be >= 0.")
    if sku and not SKU_PATTERN.fullmatch(sku):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="sku must use lowercase letters, numbers, underscore or hyphen (max 64 chars).",
        )

    with get_connection() as connection:
        with connection.cursor() as cursor:
            if product_id:
                cursor.execute(
                    """
                    UPDATE snack_products
                    SET name = COALESCE(NULLIF(%s, ''), name),
                        description = COALESCE(NULLIF(%s, ''), description),
                        category = COALESCE(NULLIF(%s, ''), category),
                        price_pence = CASE WHEN %s < 0 THEN price_pence ELSE %s END,
                        active = %s,
                        sort_order = CASE WHEN %s < 0 THEN sort_order ELSE %s END,
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING *
                    """,
                    (
                        name,
                        description,
                        category,
                        price_pence,
                        price_pence,
                        active,
                        sort_order,
                        sort_order,
                        product_id,
                    ),
                )
            else:
                if not sku or not name:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="sku and name are required.")
                cursor.execute(
                    """
                    INSERT INTO snack_products (sku, name, description, category, price_pence, active, sort_order)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (sku)
                    DO UPDATE SET
                        name = EXCLUDED.name,
                        description = EXCLUDED.description,
                        category = EXCLUDED.category,
                        price_pence = EXCLUDED.price_pence,
                        active = EXCLUDED.active,
                        sort_order = EXCLUDED.sort_order,
                        updated_at = NOW()
                    RETURNING *
                    """,
                    (sku, name, description, category, price_pence, active, sort_order),
                )
            row = cursor.fetchone()
        connection.commit()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")

    return {
        "product": {
            "id": str(row["id"]),
            "sku": row["sku"],
            "name": row["name"],
            "description": row.get("description") or "",
            "category": row.get("category") or "",
            "price_pence": _to_int(row.get("price_pence"), 0),
            "active": bool(row.get("active")),
            "sort_order": _to_int(row.get("sort_order"), 0),
        }
    }


def snack_customer_admin_patch(customer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    phone = str(payload.get("phone") or "").strip()
    notes = str(payload.get("notes") or "").strip()
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE snack_customers
                SET name = CASE WHEN %s = '' THEN name ELSE %s END,
                    phone = CASE WHEN %s = '' THEN phone ELSE %s END,
                    notes = CASE WHEN %s = '' THEN notes ELSE %s END,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (name, name, phone, phone, notes, notes, customer_id),
            )
            row = cursor.fetchone()
        connection.commit()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found.")
    return {"customer_id": customer_id, "status": "updated"}


def snack_order_admin_patch(order_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    admin_notes = str(payload.get("admin_notes") or "").strip()
    new_status = str(payload.get("status") or "").strip().lower()
    allowed_status = {"draft", "pending_payment", "paid", "failed", "cancelled", "refunded"}
    if new_status and new_status not in allowed_status:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status.")
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE snack_orders
                SET admin_notes = CASE WHEN %s = '' THEN admin_notes ELSE %s END,
                    status = CASE WHEN %s = '' THEN status ELSE %s END,
                    refunded_at = CASE WHEN %s = 'refunded' THEN NOW() ELSE refunded_at END,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING id
                """,
                (admin_notes, admin_notes, new_status, new_status, new_status, order_id),
            )
            row = cursor.fetchone()
        connection.commit()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")
    return {"order_id": order_id, "status": "updated"}
