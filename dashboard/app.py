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

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dashboard import content, data, jobs, store

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


@app.get("/category/{slug}/pdp/{idx}", response_class=HTMLResponse)
def pdp_detail(request: Request, slug: str, idx: int):
    cat, pdp = data.get_pdp(slug, idx)
    if not pdp:
        return HTMLResponse("PDP not found", status_code=404)
    return templates.TemplateResponse(request=request, name="pdp.html", context={
        "cat": cat, "pdp": pdp, "labels": data.DIMENSION_LABELS,
        "tabs": data.PDP_TABS,
    })


@app.get("/pkgimg/{slug}/{filename}")
def packaging_image(slug: str, filename: str):
    """Serve an extracted packaging comparison image."""
    path = content.packaging_image_path(slug, filename)
    if not path:
        return Response(status_code=404)
    return FileResponse(str(path), headers={"Cache-Control": "public, max-age=86400"})


@app.get("/img")
def image_proxy(u: str):
    """Serve a Zeus CDN image same-origin (host-allowlisted, disk-cached)."""
    body, ctype = content.cached_image(u)
    if body is None:
        return Response(status_code=404)
    return Response(content=body, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=86400"})


@app.post("/finding/status")
def set_finding_status(product: str = Form(...), fid: str = Form(...), status: str = Form(...)):
    ok = store.set_resolution(product, fid, status)
    return JSONResponse({"ok": ok, "fid": fid, "status": status}, status_code=200 if ok else 400)


@app.get("/report/{slug}")
def report(slug: str):
    path = data.latest_report_path(slug)
    if not path:
        return HTMLResponse("No report generated yet for this category.", status_code=404)
    return FileResponse(str(path))
