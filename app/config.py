from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    notion_token: str
    risk_db_id: str
    keypart_db_id: str
    order_db_id: str
    bom_db_id: str
    action_db_id: str
    app_host: str = "127.0.0.1"
    app_port: int = 8000

    class Config:
        env_file = ".env"

settings = Settings()
