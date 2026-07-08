#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def http_json(url: str, method: str = "GET", data: dict | None = None, auth_header: str | None = None) -> dict | list:
    body = None
    headers = {
        "Accept": "application/json",
    }
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if auth_header:
        headers["Authorization"] = auth_header

    request = Request(url=url, method=method, data=body, headers=headers)
    try:
        with urlopen(request, timeout=40) as response:
            raw = response.read().decode("utf-8")
            if not raw:
                return {}
            return json.loads(raw)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {exc.reason} for {url}\n{detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error calling {url}: {exc}") from exc


def main() -> int:
    wp_base_url = require_env("WP_BASE_URL").rstrip("/")
    wp_username = require_env("WP_USERNAME")
    wp_app_password = require_env("WP_APP_PASSWORD")
    wp_page_slug = os.getenv("WP_PAGE_SLUG", "snackccountancy").strip() or "snackccountancy"
    wp_status = os.getenv("WP_PAGE_STATUS", "publish").strip() or "publish"
    wp_title = os.getenv("WP_PAGE_TITLE", "").strip()
    source_file = Path(os.getenv("WP_SOURCE_FILE", "backend/static/SnackccountancyCheckoutout.html"))

    if not source_file.exists():
        raise RuntimeError(f"Source file does not exist: {source_file}")

    content = source_file.read_text(encoding="utf-8")
    if not content.strip():
        raise RuntimeError(f"Source file is empty: {source_file}")

    token = base64.b64encode(f"{wp_username}:{wp_app_password}".encode("utf-8")).decode("ascii")
    auth_header = f"Basic {token}"

    slug_url = f"{wp_base_url}/wp-json/wp/v2/pages?slug={quote(wp_page_slug)}"
    pages = http_json(slug_url, auth_header=auth_header)
    if not isinstance(pages, list) or not pages:
        raise RuntimeError(
            f"Could not find WordPress page with slug '{wp_page_slug}'. "
            f"Create it first at {wp_base_url}/{wp_page_slug}/"
        )

    page = pages[0]
    page_id = page.get("id")
    if not page_id:
        raise RuntimeError(f"WordPress page payload missing 'id': {page}")

    update_payload: dict[str, object] = {
        "content": content,
        "status": wp_status,
    }
    if wp_title:
        update_payload["title"] = wp_title

    update_url = f"{wp_base_url}/wp-json/wp/v2/pages/{page_id}"
    updated = http_json(update_url, method="POST", data=update_payload, auth_header=auth_header)

    link = ""
    if isinstance(updated, dict):
        link = str(updated.get("link") or "")
    print(f"Updated WordPress page id={page_id} slug={wp_page_slug} source={source_file}")
    if link:
        print(f"Live URL: {link}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
