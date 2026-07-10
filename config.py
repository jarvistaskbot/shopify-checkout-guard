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
    # Haiku AI incident analysis
    ai_analysis_enabled: bool = True
    anthropic_api_key: str = ""
    # Monthly AI call cap per merchant (soft limit — skips call when exceeded)
    ai_monthly_call_cap: int = 200

    model_config = {"env_file": ".env"}


settings = Settings()
