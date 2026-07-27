"""
app.py — PDP Intelligence Dashboard (FastAPI).

Run locally:
    .venv\\Scripts\\python -m uvicorn dashboard.app:app --port 8000
Then open http://localhost:8000

Routes:
    GET  /                 portfolio overview + category selector + Run
    POST /run              enqueue audits for selected categories
    GET  /api/status       live job statuses (polled by the page)
    GET  /category/{slug}  per-PDP results for one category
    GET  /report/{slug}    serve that category's latest full HTML report
"""

from pathlib import Path
from typing import List

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dashboard import data, jobs

BASE = Path(__file__).parent

app = FastAPI(title="PDP Intelligence Dashboard")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    cats = data.list_categories()
    return templates.TemplateResponse(request=request, name="index.html", context={
        "categories": cats,
        "stats": data.portfolio_stats(cats),
        "jobs": jobs.status_snapshot(),
    })


@app.post("/run")
def run(categories: List[str] = Form(default=[])):
    jobs.enqueue(categories)
    return RedirectResponse("/", status_code=303)


@app.get("/api/status")
def status():
    return JSONResponse(jobs.status_snapshot())


@app.get("/category/{slug}", response_class=HTMLResponse)
def category(request: Request, slug: str):
    cat = data.get_category(slug)
    if not cat:
        return HTMLResponse("Category not found", status_code=404)
    return templates.TemplateResponse(request=request, name="category.html", context={
        "cat": cat,
        "labels": data.DIMENSION_LABELS,
    })


@app.get("/report/{slug}")
def report(slug: str):
    path = data.latest_report_path(slug)
    if not path:
        return HTMLResponse("No report generated yet for this category.", status_code=404)
    return FileResponse(str(path))
