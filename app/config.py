import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LIFEOS_")

    PROJECT_NAME: str = "LIFEOS"
    VERSION: str = "0.1.0"
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    DATA_DIR: Path = BASE_DIR / "data"
    DB_PATH: str = str(DATA_DIR / "lifeos.db")
    PLUGINS_DIR: Path = BASE_DIR / "app" / "plugins"

settings = Settings()
os.makedirs(settings.DATA_DIR, exist_ok=True)
os.makedirs(settings.PLUGINS_DIR, exist_ok=True)
