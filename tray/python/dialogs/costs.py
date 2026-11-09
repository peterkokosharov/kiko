# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-4-6
"""tray/dialogs/costs.py — billing data + cap logic.

DB schema (matches kiko_sqlite_logger.py):
  CREATE TABLE billing (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      model      TEXT    NOT NULL,
      tokens_in  INTEGER NOT NULL DEFAULT 0,
      tokens_out INTEGER NOT NULL DEFAULT 0,
      cost_usd   REAL    NOT NULL DEFAULT 0.0,
      ts         TEXT    NOT NULL,
      route      TEXT
  );

Default DB path: ~/.kircho/kircho.db
"""
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

_DEFAULT_DB = Path.home() / ".kircho" / "kircho.db"

_DDL = """
CREATE TABLE IF NOT EXISTS billing (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    model      TEXT    NOT NULL,
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd   REAL    NOT NULL DEFAULT 0.0,
    ts         TEXT    NOT NULL,
    route      TEXT
);
"""


@dataclass
class CostData:
    today_usd: float
    month_usd: float
    cap_usd: float | None
    cap_status: str          # "ok" | "warning" (>80%) | "exceeded"
    per_model: list[dict]    # [{"model": str, "month_usd": float}]


def _open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.execute(_DDL)
    con.commit()
    return con


def _cap_status(month_usd: float, cap_usd: float | None) -> str:
    if cap_usd is None or cap_usd <= 0:
        return "ok"
    if month_usd >= cap_usd:
        return "exceeded"
    if month_usd > 0.8 * cap_usd:
        return "warning"
    return "ok"


def get_costs(db_path: Path | None = None, cap_usd: float | None = None) -> CostData:
    """Query the billing DB and return a CostData summary.

    If *db_path* is None, uses the default path.
    Handles a missing or empty DB gracefully (returns zeros).
    """
    path = db_path if db_path is not None else _DEFAULT_DB
    today_str = date.today().isoformat()          # e.g. '2026-10-14'
    month_str = today_str[:7]                      # e.g. '2026-10'

    today_usd = 0.0
    month_usd = 0.0
    per_model: list[dict] = []

    try:
        con = _open_db(path)
        try:
            row = con.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM billing WHERE ts LIKE ?",
                (f"{today_str}%",),
            ).fetchone()
            today_usd = float(row[0]) if row else 0.0

            row = con.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM billing WHERE ts LIKE ?",
                (f"{month_str}%",),
            ).fetchone()
            month_usd = float(row[0]) if row else 0.0

            rows = con.execute(
                "SELECT model, COALESCE(SUM(cost_usd), 0) FROM billing "
                "WHERE ts LIKE ? GROUP BY model ORDER BY 2 DESC",
                (f"{month_str}%",),
            ).fetchall()
            per_model = [{"model": r[0], "month_usd": float(r[1])} for r in rows]
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        pass  # DB unavailable — return zeros

    return CostData(
        today_usd=today_usd,
        month_usd=month_usd,
        cap_usd=cap_usd,
        cap_status=_cap_status(month_usd, cap_usd),
        per_model=per_model,
    )
