# Jenius Platform

Jenius is Jaccountancy's all-in-one AI operating system for practice workflows.

It began as a Credit Control Console, and now runs connected workflows across:

- debtors and collections
- client operations
- invoices and cash visibility
- document and filing processes
- Xero (accounting + payroll)
- HMRC
- Companies House

The platform is designed to replace fragmented tool chains with one integrated system, including workflows that were previously split across products such as Ignition, Dext, Adobe, BrightManager, and separate client portals.

OpenAI powers extraction, matching, drafting, and guided review flows across the workspace.

## Repository layout

- `backend/` FastAPI services, integrations, persistence, and web console assets
- `Credit Control Console/` macOS SwiftUI app shell and native surfaces
- `WebPanel/` web UI assets used by panel surfaces and standalone previews

## Product positioning

Jaccountancy is using Jenius as a unified operational platform instead of disconnected specialist apps, with one shared data and automation layer across the firm.

## Auto-publish Snackccountancy page

This repo can auto-publish the checkout page to WordPress when you push to `main`.

- Workflow: `.github/workflows/publish-snackccountancy.yml`
- Source file published: `backend/static/SnackccountancyCheckoutout.html`
- Target page slug: `snackccountancy` (so `https://www.jaccountancy.co.uk/snackccountancy`)

Set these GitHub repository secrets:

- `WP_BASE_URL` (example: `https://www.jaccountancy.co.uk`)
- `WP_USERNAME` (WordPress user with permission to edit that page)
- `WP_APP_PASSWORD` (WordPress Application Password for that user)
- `WP_PAGE_TITLE` (optional)
- `WP_PAGE_STATUS` (optional, default is `publish`)
- `WP_USE_IFRAME_EMBED` (optional, recommended: `true`)
- `WP_EMBED_SRC_URL` (optional, default: `https://jenius.jaccountancy.co.uk/snackccountancy-checkoutout`)

Recommended mode is iframe embed (`WP_USE_IFRAME_EMBED=true`) because many WordPress setups sanitize or block inline `<script>` in page content, which breaks checkout JavaScript.

After secrets are set, any push changing `backend/static/SnackccountancyCheckoutout.html` will trigger an automatic WordPress page update.
