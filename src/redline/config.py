"""Configuration: load from environment variables (with optional .env file)."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Config:
    coinbase_api_key: str
    coinbase_api_secret: str
    coinbase_portfolio_id: str | None
    dry_run: bool
    db_path: Path
    log_level: str
    env: str
    rebalance_time_utc: str
    target_vol: float
    max_drawdown: float
    max_daily_loss: float
    min_trade_usd: float
    max_gross_notional: float
    max_per_asset_notional: float
    slack_webhook_url: str | None
    mock: bool  # use synthetic data instead of real API calls
    allow_test_overrides: bool  # must be true + env==dev to accept override flags


def load_config(env_file: str | Path | None = ".env") -> Config:
    if env_file:
        load_dotenv(env_file, override=False)

    def _bool(val: str) -> bool:
        return val.strip().lower() in ("1", "true", "yes")

    coinbase_api_key = os.environ.get("COINBASE_API_KEY", "")
    coinbase_api_secret = os.environ.get("COINBASE_API_SECRET", "")
    mock = _bool(os.environ.get("REDLINE_MOCK", "false")) or not coinbase_api_key

    env = os.environ.get("REDLINE_ENV", "dev")

    if env == "prod" and mock:
        print("Error: mock mode is not allowed when REDLINE_ENV=prod", file=sys.stderr)
        sys.exit(1)

    if mock:
        default_db = "./data/redline-mock.db"
    elif env == "dev":
        default_db = "./data/redline-dev.db"
    else:
        default_db = "./data/redline.db"
    db_path = Path(os.environ.get("REDLINE_DB_PATH", default_db))

    return Config(
        coinbase_api_key=coinbase_api_key,
        coinbase_api_secret=coinbase_api_secret,
        coinbase_portfolio_id=os.environ.get("COINBASE_PORTFOLIO_ID") or None,
        dry_run=_bool(os.environ.get("REDLINE_DRY_RUN", "true")),
        db_path=db_path,
        log_level=os.environ.get("REDLINE_LOG_LEVEL", "INFO"),
        env=env,
        rebalance_time_utc=os.environ.get("REDLINE_REBALANCE_TIME_UTC", "00:00"),
        target_vol=float(os.environ.get("REDLINE_TARGET_VOL", "0.25")),
        max_drawdown=float(os.environ.get("REDLINE_MAX_DRAWDOWN", "0.30")),
        max_daily_loss=float(os.environ.get("REDLINE_MAX_DAILY_LOSS", "0.05")),
        min_trade_usd=float(os.environ.get("REDLINE_MIN_TRADE_USD", "50")),
        max_gross_notional=float(os.environ.get("REDLINE_MAX_GROSS_NOTIONAL", "1.00")),
        max_per_asset_notional=float(os.environ.get("REDLINE_MAX_PER_ASSET_NOTIONAL", "0.70")),
        slack_webhook_url=os.environ.get("REDLINE_SLACK_WEBHOOK_URL") or None,
        mock=mock,
        allow_test_overrides=_bool(os.environ.get("REDLINE_ALLOW_TEST_OVERRIDES", "false")),
    )
