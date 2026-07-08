from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    shopify_api_key: str
    shopify_api_secret: str
    app_url: str

    model_config = {"env_file": ".env"}


settings = Settings()
