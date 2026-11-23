from functools import lru_cache

from pydantic import AwareDatetime, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Smart Mobility Backend"
    environment: str = "development"
    api_v1_prefix: str = "/api/v1"
    database_url: str = "sqlite:///./smart_mobility.db"
    sqlite_foreign_keys: bool = True
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]
    session_hours: int = Field(8, gt=0)
    session_cookie_secure: bool = False
    battery_fresh_minutes: int = Field(5, gt=0)
    parking_no_show_minutes: int = Field(10, gt=0)
    charging_duration_minutes: int = Field(20, gt=0)
    charging_target_pct: float = Field(80, gt=0, le=100)
    operational_tick_seconds: int = Field(5, ge=0)
    demo_telemetry_seconds: int = Field(0, ge=0)
    simulation_step_minutes: int = Field(5, gt=0, le=60)
    simulation_baseline_rental_count: int = Field(2, ge=0, le=1000)
    demo_clock_utc: AwareDatetime | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
