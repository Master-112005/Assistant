"""
Environment and project config loader.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import ValidationError, field_validator
from core.paths import DATA_DIR, LOG_DIR, MODELS_DIR
from core.errors import ConfigError

class Config(BaseSettings):
    """Main application configuration loaded from environment variables or .env."""
    APP_NAME: str = "Nova Assistant"
    APP_ENV: str = "development"
    DEBUG: bool = True
    VERSION: str = "0.1.0"
    
    # Path mappings for easier access from config
    DATA_DIR_PATH: str = str(DATA_DIR)
    LOG_DIR_PATH: str = str(LOG_DIR)
    MODELS_DIR_PATH: str = str(MODELS_DIR)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @field_validator("DEBUG", mode="before")
    @classmethod
    def normalize_debug(cls, value):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on", "debug", "development"}:
                return True
            if normalized in {"0", "false", "no", "off", "release", "production"}:
                return False
        return value

def load_config() -> Config:
    """Loads configuration and handles errors."""
    try:
        return Config()
    except ValidationError as e:
        raise ConfigError(f"Failed to load configuration: {e}")

# Global config instance
config = load_config()
