"""
store.py — the dashboard's data store (SQLite).

Why this exists: reading run JSON files directly does not scale to many concurrent
category managers. SQLite gives a real, queryable, concurrent-read store today with
zero setup, and swaps to Postgres later by changing only the connection here (the SQL
is standard). Every audit's scores + findings are ingested into rows once; the app
only ever queries this store.

Tables:
    runs        (run_id, product, run_ts, run_date)                one row per audit run
    pdps        (run_id, url, overall, status, scores)             one row per PDP in a run
    findings    (run_id, url, fid, layer, severity, title, ...)    one row per finding
    resolutions (product, fid, status, note, updated_at)           P3 review state (persists across runs)

The engine still writes its run JSON to outputs/runs/ (unchanged); `sync()` ingests any
new runs into the DB. This keeps the engine untouched while giving the app a real store.
"""

import json
import sqlite3
import threading

from dashboard import findings as findings_mod
from tools.build_dashboard import REPO_ROOT, RUNS_DIR
from utils.logger import get_logger

log = get_logger("dashboard.store")

DB_PATH = REPO_ROOT / "outputs" / "dashboard.db"
_init_lock = threading.Lock()
_initialized = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id   TEXT PRIMARY KEY,
    product  TEXT NOT NULL,
    run_ts   TEXT,
    run_date TEXT
);
CREATE TABLE IF NOT EXISTS pdps (
    run_id  TEXT NOT NULL,
    url     TEXT NOT NULL,
    overall REAL,
    status  TEXT,
    scores  TEXT,                      -- JSON {dimension: score}
    PRIMARY KEY (run_id, url)
);
CREATE TABLE IF NOT EXISTS findings (
    run_id     TEXT NOT NULL,
    url        TEXT NOT NULL,
    fid        TEXT NOT NULL,
    layer      TEXT,
    severity   TEXT,
    title      TEXT,
    detail     TEXT,
    suggestion TEXT,
    PRIMARY KEY (run_id, fid)
);
CREATE TABLE IF NOT EXISTS resolutions (
    product    TEXT NOT NULL,
    fid        TEXT NOT NULL,
    status     TEXT NOT NULL,          -- open | implemented | verified | dismissed
    note       TEXT,
    updated_at TEXT,
    PRIMARY KEY (product, fid)
);
CREATE INDEX IF NOT EXISTS ix_runs_product ON runs (product, run_ts);
CREATE INDEX IF NOT EXISTS ix_pdps_run ON pdps (run_id);
CREATE INDEX IF NOT EXISTS ix_findings_run ON findings (run_id);
"""


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")      # concurrent reads while one writer
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    global _initialized
    with _init_lock:
        if _initialized:
            return
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _conn() as con:
            con.executescript(SCHEMA)
        _initialized = True


# ── ingest ───────────────────────────────────────────────────────────────────

def _known_run_ids(con) -> set:
    return {r["run_id"] for r in con.execute("SELECT run_id FROM runs")}


def sync() -> int:
    """Ingest any run summaries in outputs/runs not yet in the DB. Idempotent.
    Returns the number of newly ingested runs."""
    init_db()
    ingested = 0
    with _conn() as con:
        known = _known_run_ids(con)
        for f in sorted(RUNS_DIR.glob("*.json")):
            if f.name.endswith(".full.json"):
                continue
            try:
                summary = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            run_id = summary.get("run_id") or f.stem
            if run_id in known:
                continue
            _ingest_run(con, run_id, summary, f)
            ingested += 1
        con.commit()
    if ingested:
        log.info(f"store.sync ingested {ingested} new run(s)")
    return ingested


def _ingest_run(con, run_id: str, summary: dict, summary_path):
    product = summary.get("product_name", "")
    con.execute(
        "INSERT OR REPLACE INTO runs (run_id, product, run_ts, run_date) VALUES (?,?,?,?)",
        (run_id, product, summary.get("run_ts"), summary.get("run_date")),
    )
    for pdp in summary.get("pdps", []):
        con.execute(
            "INSERT OR REPLACE INTO pdps (run_id, url, overall, status, scores) VALUES (?,?,?,?,?)",
            (run_id, pdp.get("url"), pdp.get("overall_score"), pdp.get("status"),
             json.dumps(pdp.get("scores", {}))),
        )

    # Findings come from the full snapshot (if present).
    full_path = summary_path.with_name(f"{run_id}.full.json")
    if not full_path.exists():
        return
    try:
        results = json.loads(full_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    for result in results:
        url = result.get("url", "")
        for fnd in findings_mod.extract_findings(result):
            con.execute(
                "INSERT OR REPLACE INTO findings "
                "(run_id, url, fid, layer, severity, title, detail, suggestion) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, url, fnd["id"], fnd["layer"], fnd["severity"],
                 fnd["title"], fnd["detail"], fnd["suggestion"]),
            )


# ── queries ──────────────────────────────────────────────────────────────────

def latest_run(product: str) -> dict:
    """Latest run row for a product, or {} if none."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM runs WHERE product=? ORDER BY run_ts DESC LIMIT 1", (product,)
        ).fetchone()
        return dict(row) if row else {}


def pdps_for_run(run_id: str) -> list:
    with _conn() as con:
        rows = con.execute(
            "SELECT url, overall, status, scores FROM pdps WHERE run_id=?", (run_id,)
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "url": r["url"], "overall_score": r["overall"], "status": r["status"],
            "scores": json.loads(r["scores"] or "{}"),
        })
    return out


def findings_for_run(run_id: str) -> dict:
    """All findings for a run, grouped by url: {url: [finding, ...]}."""
    with _conn() as con:
        rows = con.execute(
            "SELECT url, fid, layer, severity, title, detail, suggestion "
            "FROM findings WHERE run_id=?", (run_id,)
        ).fetchall()
    grouped: dict = {}
    rank = {"critical": 0, "warning": 1, "info": 2}
    for r in rows:
        grouped.setdefault(r["url"], []).append({
            "id": r["fid"], "layer": r["layer"], "severity": r["severity"],
            "title": r["title"], "detail": r["detail"], "suggestion": r["suggestion"],
        })
    for url in grouped:
        grouped[url].sort(key=lambda f: rank.get(f["severity"], 9))
    return grouped
