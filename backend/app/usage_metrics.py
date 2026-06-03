import json
import os
import re
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from .database import get_connection, utcnow

DEFAULT_DAYS = 30
TOP_FEATURE_LIMIT = 18
TOP_ENDPOINT_LIMIT = 18

_OPENAI_RATE_TABLE = {
    "gpt-5": {"input": Decimal("1.25"), "output": Decimal("10.00")},
    "gpt-5-mini": {"input": Decimal("0.25"), "output": Decimal("2.00")},
    "gpt-5-nano": {"input": Decimal("0.05"), "output": Decimal("0.40")},
    "gpt-4.1": {"input": Decimal("2.00"), "output": Decimal("8.00")},
    "gpt-4.1-mini": {"input": Decimal("0.40"), "output": Decimal("1.60")},
    "gpt-4o-mini": {"input": Decimal("0.15"), "output": Decimal("0.60")},
}
_DEFAULT_OPENAI_RATE = {"input": Decimal("0.40"), "output": Decimal("1.60")}


def _to_int(value, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_decimal(value, default: Decimal = Decimal("0")) -> Decimal:
    try:
        if value in (None, ""):
            return default
        return Decimal(str(value))
    except Exception:
        return default


def _normalise_model_name(model: str | None) -> str:
    text = str(model or "").strip().lower()
    if not text:
        return ""
    return re.sub(r"[\s_]+", "-", text)


def _openai_rate_table() -> dict[str, dict[str, Decimal]]:
    table = dict(_OPENAI_RATE_TABLE)
    raw = str(os.getenv("OPENAI_USAGE_PRICE_OVERRIDES", "")).strip()
    if not raw:
        return table
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return table
    if not isinstance(payload, dict):
        return table
    for model, row in payload.items():
        if not isinstance(row, dict):
            continue
        input_rate = _to_decimal(row.get("input"), None)
        output_rate = _to_decimal(row.get("output"), None)
        if input_rate is None or output_rate is None:
            continue
        table[_normalise_model_name(model)] = {"input": input_rate, "output": output_rate}
    return table


def estimate_openai_cost_usd(model: str | None, input_tokens: int, output_tokens: int) -> float:
    rates = _openai_rate_table()
    normalised_model = _normalise_model_name(model)
    row = rates.get(normalised_model)
    if row is None:
        if "mini" in normalised_model:
            row = rates.get("gpt-4.1-mini", _DEFAULT_OPENAI_RATE)
        elif "nano" in normalised_model:
            row = rates.get("gpt-5-nano", _DEFAULT_OPENAI_RATE)
        elif normalised_model.startswith("gpt-5"):
            row = rates.get("gpt-5", _DEFAULT_OPENAI_RATE)
        else:
            row = _DEFAULT_OPENAI_RATE

    in_tokens = max(_to_int(input_tokens), 0)
    out_tokens = max(_to_int(output_tokens), 0)
    cost = (Decimal(in_tokens) / Decimal("1000000")) * row["input"]
    cost += (Decimal(out_tokens) / Decimal("1000000")) * row["output"]
    return float(cost.quantize(Decimal("0.000001")))


def parse_openai_usage_tokens(payload: dict | None) -> tuple[int, int, int]:
    usage = payload.get("usage") if isinstance(payload, dict) else {}
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = _to_int(usage.get("input_tokens") or usage.get("prompt_tokens"))
    output_tokens = _to_int(usage.get("output_tokens") or usage.get("completion_tokens"))
    total_tokens = _to_int(usage.get("total_tokens"))
    if total_tokens <= 0:
        total_tokens = input_tokens + output_tokens
    return max(input_tokens, 0), max(output_tokens, 0), max(total_tokens, 0)


def infer_openai_feature_page(purpose: str | None) -> tuple[str, str]:
    text = str(purpose or "").strip().lower()
    if not text:
        return "openai-core", "settings"
    if "insight" in text:
        return "insights", "insights"
    if "risk assessment" in text:
        return "risk-assessments", "risk-assessments"
    if "supplier" in text:
        return "supplier-reconciliation", "supplier-reconciliation"
    if "statement extraction" in text and "supplier" not in text:
        return "bank-statements", "bank-statements"
    if "ct comps" in text or "me report" in text:
        return "me-report", "me-report"
    if "practice pack" in text:
        return "practice-packs", "practice-packs"
    if "ignition" in text or "renewal" in text:
        return "ignition", "ignition"
    return "openai-core", "settings"


def infer_xero_feature_page(endpoint: str | None, operation: str | None = None) -> tuple[str, str]:
    path = str(endpoint or "").lower()
    action = str(operation or "").lower()
    text = f"{path} {action}"
    if "organisation" in text and ("lock" in text or "period" in text):
        return "xero-lock-date", "xero-lock-date"
    if "accounts" in text:
        return "xero-coa", "settings"
    if "attachment" in text and "contact" in text and "risk" in text:
        return "risk-assessments", "risk-assessments"
    if "history" in text:
        return "xero-history", "client"
    if "creditnote" in text or "overpayment" in text:
        return "xero-credit-allocation", "ledger"
    if "invoice" in text and ("late" in text or "charge" in text):
        return "late-charges", "late-charges"
    if "invoice" in text and ("bad debt" in text or "write off" in text):
        return "bad-debt", "bad-debt"
    if "invoice" in text and ("companies house" in text or "secretarial" in text):
        return "confirmation-statements", "confirmation-statements"
    if "invoice" in text and "jashflow" in text:
        return "jashflow", "jashflow"
    if "invoice" in text:
        return "xero-invoices", "ledger"
    if "contacts" in text:
        return "xero-contacts", "ledger"
    if "payments" in text:
        return "xero-payments", "ledger"
    return "xero-api", "settings"


def record_usage_event(
    *,
    provider: str,
    user_id: str | None,
    tenant_id: str | None = None,
    feature: str | None = None,
    page: str | None = None,
    operation: str | None = None,
    endpoint: str | None = None,
    model: str | None = None,
    request_units: int | None = 1,
    request_bytes: int | None = None,
    response_bytes: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    estimated_cost_usd: float | None = None,
    status_code: int | None = None,
    success: bool | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    duration_ms: int | None = None,
    metadata: dict | None = None,
) -> None:
    provider_value = str(provider or "").strip().lower()
    if provider_value not in {"openai", "xero"}:
        return

    try:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO usage_events (
                        provider, user_id, tenant_id, feature, page, operation, endpoint, model,
                        request_units, request_bytes, response_bytes,
                        input_tokens, output_tokens, total_tokens, estimated_cost_usd,
                        status_code, success, error_code, error_message, duration_ms, metadata
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s::jsonb
                    )
                    """,
                    (
                        provider_value,
                        user_id,
                        str(tenant_id or "").strip(),
                        str(feature or "").strip(),
                        str(page or "").strip(),
                        str(operation or "").strip(),
                        str(endpoint or "").strip(),
                        str(model or "").strip(),
                        max(_to_int(request_units, 1), 1),
                        max(_to_int(request_bytes), 0),
                        max(_to_int(response_bytes), 0),
                        max(_to_int(input_tokens), 0),
                        max(_to_int(output_tokens), 0),
                        max(_to_int(total_tokens), 0),
                        _to_decimal(estimated_cost_usd),
                        _to_int(status_code) if status_code is not None else None,
                        bool(success) if success is not None else False,
                        str(error_code or "").strip(),
                        str(error_message or "").strip()[:600],
                        max(_to_int(duration_ms), 0),
                        json.dumps(metadata or {}),
                    ),
                )
            connection.commit()
    except Exception:
        return


def _provider_summary_by_key(rows: list[dict]) -> dict[str, dict]:
    provider_map: dict[str, dict] = {}
    for row in rows:
        provider = str(row.get("provider") or "").strip().lower()
        if not provider:
            continue
        provider_map[provider] = {
            "provider": provider,
            "requests": _to_int(row.get("requests")),
            "success": _to_int(row.get("success")),
            "errors": _to_int(row.get("errors")),
            "avgLatencyMs": _to_float(row.get("avg_latency_ms")),
            "estimatedCostUsd": _to_float(row.get("estimated_cost_usd")),
            "avgCostPerRequestUsd": _to_float(row.get("avg_cost_per_request_usd")),
            "inputTokens": _to_int(row.get("input_tokens")),
            "outputTokens": _to_int(row.get("output_tokens")),
            "totalTokens": _to_int(row.get("total_tokens")),
        }
    for provider in ("openai", "xero"):
        provider_map.setdefault(
            provider,
            {
                "provider": provider,
                "requests": 0,
                "success": 0,
                "errors": 0,
                "avgLatencyMs": 0.0,
                "estimatedCostUsd": 0.0,
                "avgCostPerRequestUsd": 0.0,
                "inputTokens": 0,
                "outputTokens": 0,
                "totalTokens": 0,
            },
        )
    return provider_map


def usage_overview_payload(user: dict, days: int = DEFAULT_DAYS) -> dict:
    safe_days = max(min(_to_int(days, DEFAULT_DAYS), 180), 1)
    user_id = str(user.get("id") or "").strip() or None
    cutoff = utcnow() - timedelta(days=safe_days)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    provider,
                    COUNT(*) AS requests,
                    COUNT(*) FILTER (WHERE success) AS success,
                    COUNT(*) FILTER (WHERE NOT success) AS errors,
                    AVG(duration_ms)::numeric(12,2) AS avg_latency_ms,
                    COALESCE(SUM(estimated_cost_usd), 0)::numeric(14,6) AS estimated_cost_usd,
                    COALESCE(AVG(NULLIF(estimated_cost_usd, 0)), 0)::numeric(14,6) AS avg_cost_per_request_usd,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens
                FROM usage_events
                WHERE created_at >= %s
                  AND (%s::uuid IS NULL OR user_id = %s::uuid)
                GROUP BY provider
                ORDER BY provider
                """,
                (cutoff, user_id, user_id),
            )
            provider_rows = cursor.fetchall() or []

            cursor.execute(
                """
                SELECT
                    DATE_TRUNC('day', created_at)::date AS day,
                    provider,
                    COUNT(*) AS requests,
                    COUNT(*) FILTER (WHERE success) AS success,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(estimated_cost_usd), 0)::numeric(14,6) AS estimated_cost_usd,
                    COALESCE(AVG(duration_ms), 0)::numeric(12,2) AS avg_latency_ms
                FROM usage_events
                WHERE created_at >= %s
                  AND (%s::uuid IS NULL OR user_id = %s::uuid)
                GROUP BY day, provider
                ORDER BY day DESC, provider
                """,
                (cutoff, user_id, user_id),
            )
            trend_rows = cursor.fetchall() or []

            cursor.execute(
                """
                SELECT
                    provider,
                    NULLIF(feature, '') AS feature,
                    NULLIF(page, '') AS page,
                    COUNT(*) AS requests,
                    COUNT(*) FILTER (WHERE success) AS success,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(estimated_cost_usd), 0)::numeric(14,6) AS estimated_cost_usd,
                    COALESCE(AVG(duration_ms), 0)::numeric(12,2) AS avg_latency_ms
                FROM usage_events
                WHERE created_at >= %s
                  AND (%s::uuid IS NULL OR user_id = %s::uuid)
                GROUP BY provider, feature, page
                ORDER BY requests DESC, total_tokens DESC
                LIMIT %s
                """,
                (cutoff, user_id, user_id, TOP_FEATURE_LIMIT),
            )
            feature_rows = cursor.fetchall() or []

            cursor.execute(
                """
                SELECT
                    provider,
                    NULLIF(endpoint, '') AS endpoint,
                    COUNT(*) AS requests,
                    COUNT(*) FILTER (WHERE success) AS success,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(AVG(duration_ms), 0)::numeric(12,2) AS avg_latency_ms,
                    COALESCE(SUM(estimated_cost_usd), 0)::numeric(14,6) AS estimated_cost_usd
                FROM usage_events
                WHERE created_at >= %s
                  AND (%s::uuid IS NULL OR user_id = %s::uuid)
                GROUP BY provider, endpoint
                ORDER BY requests DESC, total_tokens DESC
                LIMIT %s
                """,
                (cutoff, user_id, user_id, TOP_ENDPOINT_LIMIT),
            )
            endpoint_rows = cursor.fetchall() or []

            cursor.execute(
                """
                SELECT
                    COALESCE(MAX(created_at), TIMESTAMPTZ 'epoch') AS latest_event_at,
                    COUNT(*) AS total_events
                FROM usage_events
                WHERE created_at >= %s
                  AND (%s::uuid IS NULL OR user_id = %s::uuid)
                """,
                (cutoff, user_id, user_id),
            )
            totals_row = cursor.fetchone() or {}

        connection.commit()

    provider_map = _provider_summary_by_key(provider_rows)

    trend_by_day: dict[str, dict] = defaultdict(lambda: {"providers": {}})
    for row in trend_rows:
        day_key = str(row.get("day") or "")
        provider = str(row.get("provider") or "").strip().lower()
        if not day_key or not provider:
            continue
        trend_by_day[day_key]["providers"][provider] = {
            "provider": provider,
            "requests": _to_int(row.get("requests")),
            "success": _to_int(row.get("success")),
            "totalTokens": _to_int(row.get("total_tokens")),
            "estimatedCostUsd": _to_float(row.get("estimated_cost_usd")),
            "avgLatencyMs": _to_float(row.get("avg_latency_ms")),
        }

    trend = []
    for day_key in sorted(trend_by_day.keys(), reverse=True):
        providers = trend_by_day[day_key]["providers"]
        trend.append(
            {
                "day": day_key,
                "providers": [providers[key] for key in sorted(providers.keys())],
                "requests": sum(_to_int(item.get("requests")) for item in providers.values()),
                "estimatedCostUsd": sum(_to_float(item.get("estimatedCostUsd")) for item in providers.values()),
            }
        )

    by_provider: dict[str, list[dict]] = {"openai": [], "xero": []}
    for row in feature_rows:
        provider = str(row.get("provider") or "").strip().lower()
        if provider not in by_provider:
            continue
        by_provider[provider].append(
            {
                "provider": provider,
                "feature": str(row.get("feature") or "unclassified"),
                "page": str(row.get("page") or "unknown"),
                "requests": _to_int(row.get("requests")),
                "success": _to_int(row.get("success")),
                "totalTokens": _to_int(row.get("total_tokens")),
                "estimatedCostUsd": _to_float(row.get("estimated_cost_usd")),
                "avgLatencyMs": _to_float(row.get("avg_latency_ms")),
            }
        )

    top_features = by_provider["openai"] + by_provider["xero"]

    top_endpoints = []
    for row in endpoint_rows:
        top_endpoints.append(
            {
                "provider": str(row.get("provider") or "").strip().lower(),
                "endpoint": str(row.get("endpoint") or "unclassified"),
                "requests": _to_int(row.get("requests")),
                "success": _to_int(row.get("success")),
                "totalTokens": _to_int(row.get("total_tokens")),
                "estimatedCostUsd": _to_float(row.get("estimated_cost_usd")),
                "avgLatencyMs": _to_float(row.get("avg_latency_ms")),
            }
        )

    latest_event_at = totals_row.get("latest_event_at")
    latest_event_text = "" if not latest_event_at else latest_event_at.isoformat()

    return {
        "status": "ok",
        "range": {
            "days": safe_days,
            "from": cutoff.date().isoformat(),
            "to": utcnow().date().isoformat(),
        },
        "summary": {
            "providers": [provider_map["openai"], provider_map["xero"]],
            "totalEvents": _to_int(totals_row.get("total_events")),
            "latestEventAt": latest_event_text,
        },
        "trends": trend,
        "topFeatures": top_features,
        "topFeaturesByProvider": by_provider,
        "topEndpoints": top_endpoints,
    }
