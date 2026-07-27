"""
jobs.py — a tiny single-worker audit queue.

The dashboard's "Run" button enqueues category names here. One daemon worker
thread pulls them one at a time and calls the existing ``run_product`` pipeline
(scrape → analyse → report). Running one at a time avoids hammering the scraper
and the Claude API in parallel.

State is in-memory (single-process, local dashboard). Persisting run history /
findings across restarts is a later phase — the summary JSON + HTML report that
``run_product`` already writes survive on disk regardless.
"""

import queue
import threading
import traceback
from datetime import datetime

from utils.config_loader import load_config
from utils.logger import get_logger

log = get_logger("dashboard.jobs")

_jobs = {}                    # name -> {name, status, queued_at, started_at, finished_at, error, report}
_lock = threading.Lock()
_queue: "queue.Queue[str]" = queue.Queue()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _find_cfg(name: str):
    for p in load_config()["products"]:
        if p["name"] == name:
            return p
    return None


def _set(name: str, **fields):
    with _lock:
        _jobs.setdefault(name, {"name": name})
        _jobs[name].update(fields)


def enqueue(names) -> list:
    """Queue audits for the given category names. Skips ones already in flight."""
    started = []
    for name in names:
        if _find_cfg(name) is None:
            continue
        with _lock:
            if _jobs.get(name, {}).get("status") in ("queued", "running"):
                continue
        _set(name, status="queued", queued_at=_now(),
             started_at=None, finished_at=None, error=None, report=None)
        _queue.put(name)
        started.append(name)
    return started


def status_snapshot() -> dict:
    with _lock:
        return {k: dict(v) for k, v in _jobs.items()}


def _worker():
    # Imported here (not at module load) so the FastAPI process starts fast and
    # the heavy engine + Playwright load inside this worker thread.
    from main import run_product
    while True:
        name = _queue.get()
        try:
            cfg = _find_cfg(name)
            _set(name, status="running", started_at=_now())
            log.info(f"[job] starting {name}")
            report = run_product(cfg)
            if report:
                _set(name, status="done", finished_at=_now(), report=str(report))
                log.info(f"[job] done {name} -> {report}")
            else:
                _set(name, status="failed", finished_at=_now(),
                     error="Pipeline returned no report — check the run log.")
                log.warning(f"[job] {name} returned no report")
        except Exception as e:
            _set(name, status="failed", finished_at=_now(), error=str(e))
            log.error(f"[job] {name} failed: {e}\n{traceback.format_exc()}")
        finally:
            _queue.task_done()


_worker_thread = threading.Thread(target=_worker, daemon=True, name="audit-worker")
_worker_thread.start()
