# PDP Monitor — Handoff Notes (2026-08-17)

Written at the end of a laptop-handoff day. Read this first on the new machine before doing anything else — it tells you exactly where things stand and what to do next.

## What's on GitHub right now

Branch: `claude/project-revamp-brainstorm-bd1bd1` — fully pushed, nothing stranded locally. Clone the repo and checkout this branch on the new machine to pick up exactly where this session left off.

## What happened today (in order)

1. **Full audit run, all 10 products** — fresh reports generated and published:
   **https://purvagandhi18.github.io/PDP-Monitor/**
   Emailed to category managers/senior management already.

2. **Two real packaging bugs found and fixed** (both committed, both pushed):
   - `PyMuPDF` (import name `fitz`) was missing from the environment entirely — this is why packaging PDF artwork sometimes showed "no file present." Now installed and added to `requirements.txt`.
   - `analyser/packaging_scorer.py` — PDF-page packaging images were sent to Claude at full render resolution (often >2000px), silently failing Claude's multi-image size limit and leaving the Packaging tab empty with no visible error. Fixed by resizing PDF-page renders the same way other images already were (PIL `.thumbnail((1200,1200))`).
   - Confirmed fix works correctly: packaging now honestly reports *"SKU not visible on this PDP — skipping (not a fallback)"* instead of guessing when a variant isn't photographed.

3. **Masterdoc reconciliation** (earlier this week, see git log on this branch): all 7 source-backed product masterdocs (`inputs/masterdocs/*.md`) were rebuilt as true verbatim source (PDF/DOCX content word-for-word) + all team-added GM-compliance/email-driven claims, combined into one file per product. Verified against the actual engine parser (`ingester/extractor.py`), not just eyeballed.

4. **A dashboard exists** (`dashboard/` folder, FastAPI app) as a separate, newer effort — a browsable alternative to the static HTML reports, with select-categories-and-run, native report sections, and a resolve/verify workflow. It reads from a local SQLite file (`outputs/dashboard.db`, gitignored) that is **NOT populated on a fresh clone** — see setup below.

## Known open items — not yet done

- **Magnesium Gummies, Multivitamin Gummies, Plant Protein** still have no real source documents (persona/narrative/brief) — only a small claims-only masterdoc stub. Kunal was getting these from the office; unresolved as of this handoff.
- **Ad Gaps data is unverified** — the Umbrella Sheet's product-filter cell write fails (403, insufficient auth scope), so ad performance data pulled may reflect the wrong product. Flagged in the dashboard UI. Deliberately parked — planned to be resolved via a future "Mind Matters" internal-tool integration rather than patched now.
- **Google Doc as single source of truth** — a locked decision (one structured Doc per category, prose + claims tables, imported through a validate-and-snapshot gate) but not yet built. The verbatim masterdoc reconciliation (item 3 above) was the prep work for this.
- **Dashboard is local-only** — not hosted, not integrated into any internal tool yet. Long-term vision is to fold it into an existing internal tool ("Mind Matters"), not host it as a separate standalone thing.

## Environment setup on a fresh machine/worktree

The Python venv is NOT committed to git (correctly gitignored) — every fresh clone/worktree needs this:

```bash
python3 -m venv .venv
source .venv/bin/activate        # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
playwright install chromium
```

Then copy `.env` (API keys — Anthropic, Google service account, GitHub token) into the repo root. This file is never committed; get it from wherever it's currently backed up (it lived at `C:\Users\Mosaic\PDP-Monitor\.env` on the Windows machine — back it up somewhere before wiping that laptop).

To run a single product's audit: `python main.py --run-now --product "Shilajit Gummies"`
To run the dashboard: `python -m uvicorn dashboard.app:app --port 8000` then open `http://localhost:8000`
To publish the combined report: `python -m tools.publish_all` (must be run as a module, not a script directly, or it fails on `ModuleNotFoundError: tools`)

## A few gotchas worth knowing (things that cost real time today)

- **GitHub Pages here deploys via a GitHub Actions workflow** (`.github/workflows/deploy-pages.yml` on the `gh-pages` branch), not classic branch-serving. Pushing to `gh-pages` triggers a deploy, but it can fail — check the Actions tab if the live site looks stale after a publish.
- The repo currently lives under `Purvagandhi18`'s GitHub account, not Kunal's — a planned migration to `kunal-mosaic` never happened. Still pending.
- On Windows, `python` on PATH may resolve to a Microsoft Store stub that does nothing — always use a real venv or the full interpreter path.
- The report builder and the masterdoc-slug logic use different conventions for hyphenated product names (e.g. "Anti-Hairfall Kit S1" → `anti-hairfall_kit_s1.html` vs `anti_hairfall_kit_s1`) — if a script can't find a file, check both spellings before assuming it's missing.

## If picking this up with a fresh Claude session

Point it at this file and the git log on this branch — that's the durable record. Don't assume any prior Claude memory carries over to a new machine; this file is what travels.
