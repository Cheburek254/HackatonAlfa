import os
from pathlib import Path


def account_database_path() -> Path:
    default = Path(__file__).resolve().parents[1] / "data" / "accounts" / "users.sqlite3"
    return Path(os.environ.get("ALFA_DATABASE_PATH", default))
