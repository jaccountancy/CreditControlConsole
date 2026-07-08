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


def as_bool(value: str | None, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def iframe_embed_html(src_url: str) -> str:
    safe_src = src_url.strip()
    if not safe_src:
        raise RuntimeError("WP_EMBED_SRC_URL cannot be empty when iframe embed is enabled.")
    return f"""<div style="max-width:1080px;margin:0 auto;">
  <style>
    .snackccountancy-embed-wrap {{
      width: 100%;
      min-height: 100vh;
    }}
    .snackccountancy-embed {{
      width: 100%;
      min-height: 100vh;
      border: 0;
      display: block;
      background: #fff;
    }}
  </style>
  <div class="snackccountancy-embed-wrap">
    <iframe class="snackccountancy-embed" src="{safe_src}" title="Snackccountancy Checkoutout" loading="eager" referrerpolicy="strict-origin-when-cross-origin"></iframe>
  </div>
</div>"""


def main() -> int:
    wp_base_url = require_env("WP_BASE_URL").rstrip("/")
    wp_username = require_env("WP_USERNAME")
    wp_app_password = require_env("WP_APP_PASSWORD")
    wp_page_slug = os.getenv("WP_PAGE_SLUG", "snackccountancy").strip() or "snackccountancy"
    wp_status = os.getenv("WP_PAGE_STATUS", "publish").strip() or "publish"
    wp_title = os.getenv("WP_PAGE_TITLE", "").strip()
    use_iframe_embed = as_bool(os.getenv("WP_USE_IFRAME_EMBED"), default=True)
    embed_src_url = os.getenv("WP_EMBED_SRC_URL", "https://jenius.jaccountancy.co.uk/snackccountancy-checkoutout").strip()
    source_file = Path(os.getenv("WP_SOURCE_FILE", "backend/static/SnackccountancyCheckoutout.html"))

    if use_iframe_embed:
        content = iframe_embed_html(embed_src_url)
    else:
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
    publish_mode = f"iframe:{embed_src_url}" if use_iframe_embed else f"raw-html:{source_file}"
    print(f"Updated WordPress page id={page_id} slug={wp_page_slug} mode={publish_mode}")
    if link:
        print(f"Live URL: {link}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
