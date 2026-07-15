from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = ""
    shopify_api_key: str = ""
    shopify_api_secret: str = ""
    app_url: str = ""
    sendgrid_api_key: str = ""
    oos_enabled: bool = False
    # Session signing — must be set in production
    secret_key: str = "dev-secret-change-in-prod"
    # Billing — default False so prod charges are real; set True only in local .env
    billing_test_mode: bool = False
    # AI incident analysis via OpenRouter (Claude Haiku 4.5 by default)
    ai_analysis_enabled: bool = True
    openrouter_api_key: str = ""
    anthropic_api_key: str = ""  # legacy, unused — kept so old .env files don't error

    @property
    def ai_api_key(self) -> str:
        return self.openrouter_api_key
    # Monthly AI call cap per merchant (soft limit — skips call when exceeded)
    ai_monthly_call_cap: int = 200

    model_config = {"env_file": ".env"}


settings = Settings()
