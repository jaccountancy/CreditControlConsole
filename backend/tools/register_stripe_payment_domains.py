#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


STRIPE_PMD_URL = "https://api.stripe.com/v1/payment_method_domains"


def _stripe_post(secret_key: str, domain_name: str) -> dict:
    payload = urllib.parse.urlencode(
        {
            "domain_name": domain_name,
            "enabled": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        STRIPE_PMD_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Stripe API error for {domain_name}: {exc.code} {body}") from exc


def _stripe_list(secret_key: str) -> dict:
    request = urllib.request.Request(
        STRIPE_PMD_URL,
        headers={"Authorization": f"Bearer {secret_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Stripe list call failed: {exc.code} {body}") from exc


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Register Stripe payment method domains (Apple Pay/Google Pay/Link)."
    )
    parser.add_argument(
        "domains",
        nargs="*",
        help="Domain names to register, for example: www.jaccountancy.co.uk jenius.jaccountancy.co.uk",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Only list existing registered domains.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    secret_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not secret_key:
        print("Missing STRIPE_SECRET_KEY environment variable.", file=sys.stderr)
        return 1

    if args.list_only:
        data = _stripe_list(secret_key)
        rows = data.get("data") or []
        if not rows:
            print("No Stripe payment method domains found.")
            return 0
        print("Registered payment method domains:")
        for row in rows:
            name = row.get("domain_name") or "-"
            enabled = row.get("enabled")
            apple_status = ((row.get("apple_pay") or {}).get("status")) or "-"
            google_status = ((row.get("google_pay") or {}).get("status")) or "-"
            link_status = ((row.get("link") or {}).get("status")) or "-"
            print(
                f"- {name} | enabled={enabled} | apple_pay={apple_status} | "
                f"google_pay={google_status} | link={link_status}"
            )
        return 0

    if not args.domains:
        print("Provide one or more domains, or use --list-only.", file=sys.stderr)
        return 1

    for domain in args.domains:
        domain_name = domain.strip().lower()
        if not domain_name:
            continue
        created = _stripe_post(secret_key, domain_name)
        print(
            f"Registered: {created.get('domain_name')} | "
            f"apple_pay={((created.get('apple_pay') or {}).get('status'))} | "
            f"google_pay={((created.get('google_pay') or {}).get('status'))} | "
            f"link={((created.get('link') or {}).get('status'))}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
