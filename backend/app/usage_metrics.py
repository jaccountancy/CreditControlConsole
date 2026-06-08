import json
import os
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from .database import get_connection, utcnow

DEFAULT_DAYS = 30
TOP_FEATURE_LIMIT = 18
TOP_ENDPOINT_LIMIT = 18
USAGE_DETAIL_CROSS_LIMIT = 20

_OPENAI_RATE_TABLE = {
    "gpt-5": {"input": Decimal("1.25"), "output": Decimal("10.00")},
    "gpt-5-mini": {"input": Decimal("0.25"), "output": Decimal("2.00")},
    "gpt-5-nano": {"input": Decimal("0.05"), "output": Decimal("0.40")},
    "gpt-4.1": {"input": Decimal("2.00"), "output": Decimal("8.00")},
    "gpt-4.1-mini": {"input": Decimal("0.40"), "output": Decimal("1.60")},
    "gpt-4o-mini": {"input": Decimal("0.15"), "output": Decimal("0.60")},
}
_DEFAULT_OPENAI_RATE = {"input": Decimal("0.40"), "output": Decimal("1.60")}
FEATURE_DESCRIPTIONS = {
    "xero-contacts": "Reads contact records from Xero. Used during ledger sync and contact-driven workflows.",
    "xero-invoices": "Reads and writes invoice data used by the debtor ledger and follow-up workflows.",
    "xero-credit-allocation": "Handles credit notes and overpayment allocations tied to invoice balances.",
    "xero-payments": "Imports payment events so invoice balances and statuses stay accurate.",
    "xero-coa": "Loads Xero chart of accounts and posting settings metadata.",
    "xero-lock-date": "Loads or updates Xero organisation period lock dates.",
    "me-report": "Runs ME Report data pulls and AI analysis for client review packs.",
    "ignition": "Syncs Ignition reporting data and renewal automation state.",
    "insights": "Generates AI summaries and analytics in the Insights page.",
    "xero-api": "General Xero API traffic not mapped to a narrower feature bucket.",
}


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


def _usage_severity(requests: int, error_rate: float, avg_latency_ms: float) -> str:
    if requests >= 350 or error_rate >= 0.12 or avg_latency_ms >= 1200:
        return "extreme"
    if requests >= 140 or error_rate >= 0.05 or avg_latency_ms >= 700:
        return "high"
    if requests >= 40 or error_rate >= 0.02 or avg_latency_ms >= 350:
        return "elevated"
    return "normal"


def _usage_recommendations(*, provider: str, feature: str, endpoint: str, severity: str, error_rate: float, avg_latency_ms: float) -> list[str]:
    recommendations: list[str] = []
    endpoint_lower = endpoint.lower()
    feature_lower = feature.lower()
    if provider == "xero" and ("contacts" in feature_lower or "/contacts" in endpoint_lower):
        recommendations.append("Use incremental sync windows and avoid full contact re-pulls unless a manual refresh is required.")
        recommendations.append("Use cached customer rows for dropdowns and lookups, and only call Xero Contacts when cache is stale.")
    if provider == "xero" and "/invoices" in endpoint_lower:
        recommendations.append("Limit invoice calls with narrower `where` filters and lower page limits where possible.")
    if error_rate >= 0.05:
        recommendations.append("Review failed calls in Developer Log and clear retry queues before running additional syncs.")
    if avg_latency_ms >= 700:
        recommendations.append("Reduce concurrent sync jobs and increase poll intervals while long-running jobs are active.")
    if severity in {"high", "extreme"} and provider == "openai":
        recommendations.append("Lower prompt/file payload size and consolidate requests to reduce token and latency overhead.")
    if not recommendations:
        recommendations.append("Usage is within expected range; keep monitoring for spikes and regressions.")
    return recommendations[:5]


def usage_detail_payload(
    user: dict,
    *,
    days: int = DEFAULT_DAYS,
    provider: str,
    feature: str | None = None,
    page: str | None = None,
    endpoint: str | None = None,
) -> dict:
    safe_days = max(min(_to_int(days, DEFAULT_DAYS), 180), 1)
    user_id = str(user.get("id") or "").strip() or None
    cutoff = utcnow() - timedelta(days=safe_days)
    provider_value = str(provider or "").strip().lower()
    if provider_value not in {"openai", "xero"}:
        provider_value = "xero"
    feature_value = str(feature or "").strip()
    page_value = str(page or "").strip()
    endpoint_value = str(endpoint or "").strip()
    match_mode = "endpoint" if endpoint_value else "feature"

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    COUNT(*) AS requests,
                    COUNT(*) FILTER (WHERE success) AS success,
                    COUNT(*) FILTER (WHERE NOT success) AS errors,
                    COALESCE(AVG(duration_ms), 0)::numeric(12,2) AS avg_latency_ms,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(estimated_cost_usd), 0)::numeric(14,6) AS estimated_cost_usd,
                    MIN(created_at) AS first_seen_at,
                    MAX(created_at) AS last_seen_at
                FROM usage_events
                WHERE created_at >= %s
                  AND provider = %s
                  AND (%s::uuid IS NULL OR user_id = %s::uuid)
                  AND (%s::text = '' OR feature = %s::text)
                  AND (%s::text = '' OR page = %s::text)
                  AND (%s::text = '' OR endpoint = %s::text)
                """,
                (
                    cutoff,
                    provider_value,
                    user_id,
                    user_id,
                    feature_value,
                    feature_value,
                    page_value,
                    page_value,
                    endpoint_value,
                    endpoint_value,
                ),
            )
            summary_row = cursor.fetchone() or {}

            cursor.execute(
                """
                SELECT
                    COALESCE(NULLIF(feature, ''), 'unclassified') AS feature,
                    COALESCE(NULLIF(page, ''), 'unknown') AS page,
                    COUNT(*) AS requests,
                    COALESCE(AVG(duration_ms), 0)::numeric(12,2) AS avg_latency_ms
                FROM usage_events
                WHERE created_at >= %s
                  AND provider = %s
                  AND (%s::uuid IS NULL OR user_id = %s::uuid)
                  AND (%s::text = '' OR endpoint = %s::text)
                GROUP BY feature, page
                ORDER BY requests DESC, avg_latency_ms DESC
                LIMIT %s
                """,
                (cutoff, provider_value, user_id, user_id, endpoint_value, endpoint_value, USAGE_DETAIL_CROSS_LIMIT),
            )
            feature_page_rows = cursor.fetchall() or []

            cursor.execute(
                """
                SELECT
                    COALESCE(NULLIF(endpoint, ''), 'unclassified') AS endpoint,
                    COUNT(*) AS requests,
                    COALESCE(AVG(duration_ms), 0)::numeric(12,2) AS avg_latency_ms
                FROM usage_events
                WHERE created_at >= %s
                  AND provider = %s
                  AND (%s::uuid IS NULL OR user_id = %s::uuid)
                  AND (%s::text = '' OR feature = %s::text)
                  AND (%s::text = '' OR page = %s::text)
                GROUP BY endpoint
                ORDER BY requests DESC, avg_latency_ms DESC
                LIMIT %s
                """,
                (cutoff, provider_value, user_id, user_id, feature_value, feature_value, page_value, page_value, USAGE_DETAIL_CROSS_LIMIT),
            )
            endpoint_rows = cursor.fetchall() or []

            cursor.execute(
                """
                SELECT
                    COALESCE(NULLIF(operation, ''), 'request') AS operation,
                    COUNT(*) AS requests,
                    COUNT(*) FILTER (WHERE NOT success) AS errors,
                    COALESCE(AVG(duration_ms), 0)::numeric(12,2) AS avg_latency_ms
                FROM usage_events
                WHERE created_at >= %s
                  AND provider = %s
                  AND (%s::uuid IS NULL OR user_id = %s::uuid)
                  AND (%s::text = '' OR feature = %s::text)
                  AND (%s::text = '' OR page = %s::text)
                  AND (%s::text = '' OR endpoint = %s::text)
                GROUP BY operation
                ORDER BY requests DESC, errors DESC
                LIMIT %s
                """,
                (
                    cutoff,
                    provider_value,
                    user_id,
                    user_id,
                    feature_value,
                    feature_value,
                    page_value,
                    page_value,
                    endpoint_value,
                    endpoint_value,
                    USAGE_DETAIL_CROSS_LIMIT,
                ),
            )
            operation_rows = cursor.fetchall() or []
        connection.commit()

    requests = _to_int(summary_row.get("requests"))
    success = _to_int(summary_row.get("success"))
    errors = _to_int(summary_row.get("errors"))
    avg_latency_ms = _to_float(summary_row.get("avg_latency_ms"))
    error_rate = (errors / requests) if requests > 0 else 0.0
    severity = _usage_severity(requests, error_rate, avg_latency_ms)
    dominant_feature = feature_value or (feature_page_rows[0].get("feature") if feature_page_rows else "")
    recommendations = _usage_recommendations(
        provider=provider_value,
        feature=dominant_feature,
        endpoint=endpoint_value,
        severity=severity,
        error_rate=error_rate,
        avg_latency_ms=avg_latency_ms,
    )
    return {
        "status": "ok",
        "range": {
            "days": safe_days,
            "from": cutoff.date().isoformat(),
            "to": utcnow().date().isoformat(),
        },
        "match": {
            "mode": match_mode,
            "provider": provider_value,
            "feature": feature_value,
            "page": page_value,
            "endpoint": endpoint_value,
        },
        "summary": {
            "requests": requests,
            "success": success,
            "errors": errors,
            "errorRate": round(error_rate, 4),
            "avgLatencyMs": avg_latency_ms,
            "totalTokens": _to_int(summary_row.get("total_tokens")),
            "estimatedCostUsd": _to_float(summary_row.get("estimated_cost_usd")),
            "firstSeenAt": "" if not summary_row.get("first_seen_at") else summary_row["first_seen_at"].isoformat(),
            "lastSeenAt": "" if not summary_row.get("last_seen_at") else summary_row["last_seen_at"].isoformat(),
            "severity": severity,
        },
        "explanation": {
            "whatIsThis": FEATURE_DESCRIPTIONS.get(dominant_feature, "Usage events grouped by provider, feature/page, endpoint, and operation."),
            "extremeThreshold": "Extreme when request volume, latency, or error rate crosses configured thresholds.",
        },
        "usedByFeatures": [
            {
                "feature": str(row.get("feature") or "unclassified"),
                "page": str(row.get("page") or "unknown"),
                "requests": _to_int(row.get("requests")),
                "avgLatencyMs": _to_float(row.get("avg_latency_ms")),
            }
            for row in feature_page_rows
        ],
        "usedByEndpoints": [
            {
                "endpoint": str(row.get("endpoint") or "unclassified"),
                "requests": _to_int(row.get("requests")),
                "avgLatencyMs": _to_float(row.get("avg_latency_ms")),
            }
            for row in endpoint_rows
        ],
        "operations": [
            {
                "operation": str(row.get("operation") or "request"),
                "requests": _to_int(row.get("requests")),
                "errors": _to_int(row.get("errors")),
                "avgLatencyMs": _to_float(row.get("avg_latency_ms")),
            }
            for row in operation_rows
        ],
        "help": {
            "isExtreme": severity == "extreme",
            "recommendations": recommendations,
        },
    }


def deployment_updates_payload(user: dict, limit: int = 120) -> dict:
    safe_limit = max(min(_to_int(limit, 120), 300), 1)
    user_id = str(user.get("id") or "").strip() or None
    _sync_git_push_release_updates(max(180, safe_limit))
    _sync_runtime_release_update()
    rows: list[dict] = []
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    title,
                    summary,
                    details,
                    deployment_id,
                    commit_sha,
                    source,
                    created_at,
                    updated_at
                FROM release_updates
                WHERE (%s::uuid IS NULL OR created_by_user_id IS NULL OR created_by_user_id = %s::uuid)
                ORDER BY COALESCE(created_at, updated_at) DESC, id DESC
                LIMIT %s
                """,
                (user_id, user_id, safe_limit),
            )
            rows = cursor.fetchall() or []
        connection.commit()

    deployments: list[dict] = []
    for row in rows:
        raw_details = row.get("details")
        details: list[str] = []
        if isinstance(raw_details, list):
            details = [str(item).strip() for item in raw_details if str(item).strip()]
        elif isinstance(raw_details, dict):
            details = [str(value).strip() for value in raw_details.values() if str(value).strip()]
        deployments.append(
            {
                "id": str(row.get("id") or ""),
                "title": str(row.get("title") or "").strip(),
                "summary": str(row.get("summary") or "").strip(),
                "details": details,
                "deploymentId": str(row.get("deployment_id") or "").strip(),
                "commitSha": str(row.get("commit_sha") or "").strip(),
                "source": str(row.get("source") or "manual").strip(),
                "deployedAt": "" if not row.get("created_at") else row["created_at"].isoformat(),
                "createdAt": "" if not row.get("created_at") else row["created_at"].isoformat(),
                "updatedAt": "" if not row.get("updated_at") else row["updated_at"].isoformat(),
            }
        )

    return {
        "status": "ok",
        "generatedAt": utcnow().isoformat(),
        "deployments": deployments[:safe_limit],
    }


def _git_history_rows(limit: int = 180) -> list[dict]:
    safe_limit = max(min(_to_int(limit, 180), 500), 1)
    module_dir = os.path.dirname(__file__)
    repo_root = os.path.abspath(os.path.join(module_dir, "..", ".."))
    refs = ("origin/main", "HEAD")
    for ref in refs:
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    repo_root,
                    "log",
                    ref,
                    "--date=iso-strict",
                    "--pretty=format:%H%x1f%cI%x1f%s",
                    "-n",
                    str(safe_limit),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        except Exception:
            continue
        if result.returncode != 0 or not str(result.stdout or "").strip():
            continue
        commits: list[dict] = []
        for line in str(result.stdout).splitlines():
            parts = line.split("\x1f")
            if len(parts) < 3:
                continue
            sha = str(parts[0] or "").strip()
            committed_at = str(parts[1] or "").strip()
            subject = str(parts[2] or "").strip()
            if not sha:
                continue
            commits.append(
                {
                    "sha": sha,
                    "committed_at": committed_at,
                    "subject": subject,
                }
            )
        if commits:
            return commits
    return []


def _sync_git_push_release_updates(limit: int = 180) -> None:
    commits = _git_history_rows(limit=limit)
    if not commits:
        return
    now = utcnow()
    with get_connection() as connection:
        with connection.cursor() as cursor:
            for commit in commits:
                sha = str(commit.get("sha") or "").strip()
                if not sha:
                    continue
                short_sha = sha[:8]
                committed_at = str(commit.get("committed_at") or "").strip()
                subject = str(commit.get("subject") or "").strip() or f"Pushed commit {short_sha}"
                cursor.execute(
                    """
                    INSERT INTO release_updates (
                        title,
                        summary,
                        details,
                        deployment_id,
                        commit_sha,
                        source,
                        created_at,
                        updated_at
                    )
                    VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s::timestamptz, %s)
                    ON CONFLICT (deployment_id) WHERE deployment_id <> '' DO UPDATE
                    SET title = EXCLUDED.title,
                        summary = EXCLUDED.summary,
                        details = EXCLUDED.details,
                        commit_sha = EXCLUDED.commit_sha,
                        source = EXCLUDED.source,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        f"Git push · {short_sha}",
                        subject,
                        json.dumps([f"Commit {short_sha} recorded from git history."]),
                        f"push:{sha}",
                        sha,
                        "git_push",
                        committed_at or now.isoformat(),
                        now,
                    ),
                )
        connection.commit()


def _sync_runtime_release_update() -> None:
    deployment_id = str(os.getenv("RAILWAY_DEPLOYMENT_ID") or "").strip()
    if not deployment_id:
        return
    now = utcnow()
    deployed_at_raw = str(os.getenv("RAILWAY_DEPLOYED_AT") or os.getenv("RAILWAY_CREATED_AT") or "").strip()
    deployed_at = now
    if deployed_at_raw:
        try:
            deployed_at = datetime.fromisoformat(deployed_at_raw.replace("Z", "+00:00"))
        except Exception:
            deployed_at = now
    commit_sha = str(os.getenv("RAILWAY_GIT_COMMIT_SHA") or "").strip()
    service_id = str(os.getenv("RAILWAY_SERVICE_ID") or "").strip()
    environment_id = str(os.getenv("RAILWAY_ENVIRONMENT_ID") or "").strip()
    service_name = str(os.getenv("RAILWAY_SERVICE_NAME") or "").strip()
    environment_name = str(os.getenv("RAILWAY_ENVIRONMENT_NAME") or "").strip()
    short_dep = deployment_id[:12]
    title_parts = ["Railway deployment"]
    if service_name:
        title_parts.append(service_name)
    if environment_name:
        title_parts.append(environment_name)
    title_parts.append(short_dep)
    details = [
        f"Deployment ID: {deployment_id}",
        f"Service: {service_name}" if service_name else "",
        f"Service ID: {service_id}" if service_id else "",
        f"Environment: {environment_name}" if environment_name else "",
        f"Environment ID: {environment_id}" if environment_id else "",
        f"Commit: {commit_sha[:12]}" if commit_sha else "",
    ]
    details = [line for line in details if line]
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO release_updates (
                    title,
                    summary,
                    details,
                    deployment_id,
                    commit_sha,
                    source,
                    created_at,
                    updated_at
                )
                VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s::timestamptz, %s)
                ON CONFLICT (deployment_id) WHERE deployment_id <> '' DO UPDATE
                SET title = EXCLUDED.title,
                    summary = EXCLUDED.summary,
                    details = EXCLUDED.details,
                    commit_sha = EXCLUDED.commit_sha,
                    source = EXCLUDED.source,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    " · ".join(title_parts),
                    "Published runtime deployment record.",
                    json.dumps(details),
                    deployment_id,
                    commit_sha,
                    "railway_runtime",
                    deployed_at.isoformat(),
                    now,
                ),
            )
        connection.commit()
