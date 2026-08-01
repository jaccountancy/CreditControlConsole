from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "production"
    port: int
    base_url: str
    database_url: str
    app_secret: str
    widget_token: str
    xero_client_id: str
    xero_client_secret: str
    xero_redirect_uri: str
    xero_scopes: str = (
        "openid profile email offline_access "
        "accounting.invoices accounting.payments accounting.banktransactions accounting.manualjournals "
        "accounting.contacts accounting.settings accounting.attachments "
        "accounting.reports.banksummary.read accounting.reports.balancesheet.read "
        "accounting.reports.profitandloss.read accounting.reports.trialbalance.read "
        "payroll.employees payroll.employees.read payroll.payruns payroll.payruns.read "
        "payroll.settings payroll.settings.read"
    )
    xero_enable_payroll_scopes: bool = Field(
        default=False,
        validation_alias=AliasChoices("XERO_ENABLE_PAYROLL_SCOPES", "XERO_INCLUDE_PAYROLL_SCOPES"),
    )
    xero_primary_tenant_name: str = "jaccountancy"
    panel_allowed_origins: str = "https://jenius.jaccountancy.co.uk,https://jaccountancy.co.uk,https://www.jaccountancy.co.uk"
    xero_state_ttl_seconds: int = 900
    ignition_state_ttl_seconds: int = 3600
    device_code_ttl_minutes: int = 10
    session_ttl_days: int = 1
    statutory_interest_rate: float = 0.08
    late_payment_charge_account_code: str = "1222"
    late_payment_charge_tax_type: str = "OUTPUT2"
    bad_debt_write_off_account_code: str = "402"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    me_report_openai_model: str = "gpt-5"
    ignition_client_id: str | None = None
    ignition_client_secret: str | None = None
    ignition_redirect_uri: str | None = None
    ignition_redirect_url: str | None = None
    ignition_scopes: str = "reporting"
    ignition_authorize_url: str = "https://developers.ignitionapp.com/oauth2/authorize"
    ignition_token_url: str = "https://developers.ignitionapp.com/oauth2/token"
    ignition_api_base_url: str = "https://developers.ignitionapp.com/external/api/v1"
    ignition_renewals_recipient_email: str = "amie@jaccountancy.co.uk"
    barclays_client_id: str | None = None
    barclays_client_secret: str | None = None
    barclays_redirect_uri: str | None = None
    barclays_authorize_url: str = "https://api.barclays/open-banking/authorize"
    barclays_token_url: str = "https://api.barclays/open-banking/token"
    barclays_pisp_base_url: str = "https://telesto.api.barclays/open-banking/v4.0/pisp"
    barclays_scope: str = "payments"
    barclays_financial_id: str | None = None
    barclays_debtor_name: str | None = None
    barclays_debtor_account_scheme: str = "SortCodeAccountNumber"
    barclays_debtor_account_identification: str | None = None
    foxit_base_url: str | None = None
    foxit_client_id: str | None = None
    foxit_client_secret: str | None = None
    foxit_env: str = "sandbox"
    foxit_esign_send_path: str = "/api/esign/v1/requests"
    foxit_esign_resend_path: str = "/api/esign/v1/requests/:requestId/resend"
    foxit_esign_cancel_path: str = "/api/esign/v1/requests/:requestId/cancel"
    foxit_esign_status_path: str = "/api/esign/v1/requests/:requestId"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_email: str | None = None
    smtp_from_name: str = "Jaccountancy"
    smtp_use_tls: bool = True
    me_report_bcc_email: str = "fmfhdkgaptpyubgms@accountancymanager.co.uk"
    gmail_client_id: str | None = None
    gmail_client_secret: str | None = None
    gmail_redirect_uri: str | None = None
    gmail_scopes: str = (
        "openid email "
        "https://www.googleapis.com/auth/gmail.send "
        "https://www.googleapis.com/auth/gmail.readonly "
        "https://www.googleapis.com/auth/calendar.readonly"
    )
    gmail_workspace_service_account_json: str | None = None
    gmail_workspace_default_lookback_minutes: int = 180
    gmail_workspace_sync_max_per_mailbox: int = 120
    companies_house_environment: str = "sandbox"
    companies_house_api_key: str | None = None
    companies_house_presenter_id: str | None = None
    companies_house_presenter_auth: str | None = None
    companies_house_credit_account: str | None = None
    companies_house_package_reference: str | None = None
    companies_house_auth_method: str = "clear"
    companies_house_sandbox_api_base: str = "https://api-sandbox.company-information.service.gov.uk"
    companies_house_production_api_base: str = "https://api.company-information.service.gov.uk"
    ch_alert_webhook_url: str | None = None
    stripe_publishable_key: str | None = None
    stripe_secret_key: str | None = None
    stripe_snack_webhook_secret: str | None = None
    snack_weekly_threshold_cans: int = 4
    snack_weekly_discount_percent: int = 10
    snack_milestone_interval_orders: int = 10
    snack_milestone_discount_percent: int = 50
    snack_google_client_id: str | None = None
    snack_google_client_secret: str | None = None
    snack_google_redirect_uri: str | None = None
    snack_facebook_client_id: str | None = None
    snack_facebook_client_secret: str | None = None
    snack_facebook_redirect_uri: str | None = None
    snack_apple_client_id: str | None = None
    snack_apple_client_secret: str | None = None
    snack_apple_redirect_uri: str | None = None

    @field_validator("companies_house_auth_method")
    @classmethod
    def normalise_auth_method(cls, value: str | None) -> str:
        text = (value or "").strip()
        if not text:
            return "clear"
        return "clear"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator(
        "base_url",
        "database_url",
        "xero_redirect_uri",
        "ignition_redirect_uri",
        "ignition_redirect_url",
        "gmail_redirect_uri",
        "barclays_redirect_uri",
    )
    @classmethod
    def reject_local_values(cls, value: str | None) -> str | None:
        if value is None:
            return value
        lowered = value.lower()
        if "localhost" in lowered or "127.0.0.1" in lowered:
            raise ValueError("Local connection values are disabled; use Railway-hosted URLs only.")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
