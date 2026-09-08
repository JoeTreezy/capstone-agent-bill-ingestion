"""Environment-based configuration. All secrets come from env vars, never hardcoded."""
from typing import Literal
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    anthropic_api_key: str
    anthropic_model: str = "claude-sonnet-4-5"  # confirm current model ID in the Anthropic Console

    # Retool DB -- the writable staging tables (staged_bills, staged_bill_lines,
    # extracted_files, etc.). Used because there's no write access to Coyote's
    # production Postgres.
    retool_db_host: str
    retool_db_port: int = 5432
    retool_db_name: str
    retool_db_user: str
    retool_db_password: str
    retool_db_sslmode: str = "require"  # Retool DB requires this

    # Database where resource information exists
    coyote_db_host: str
    coyote_db_port: int = 5432
    coyote_db_name: str
    coyote_db_user: str
    coyote_db_password: str

    # Output toggle: write results to Retool DB directly, or export to a file
    # instead (e.g. for review before a manual import, or when running
    # somewhere without DB write access at all).
    output_mode: Literal["database", "csv", "excel"] = "database"
    output_dir: str = "./output"

    # Google Drive -- the folder bill PDFs get dropped into.
    drive_folder_id: str = ""
    drive_service_account_json_path: str = ""  # path to a service account key file
    poll_interval_seconds: int = 900  # 15 minutes, matching the original Retool schedule

    # Matching thresholds (resolution.py)
    fuzzy_match_high_threshold: float = 90.0
    fuzzy_match_gap_threshold: float = 10.0
    fuzzy_match_low_threshold: float = 70.0
    tot_beam_width: int = 3

    class Config:
        env_file = ".env"


settings = Settings()  # raises a clear validation error at startup if anything required is missing