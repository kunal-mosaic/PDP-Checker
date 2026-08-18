# PDP Monitor — Handoff Notes (2026-08-17)

Written at the end of a laptop-handoff day. Read this before doing anything else.

## First prompt to paste into a fresh Claude Code session on the Mac

Once the repo is cloned and this file exists on disk, open a new Claude Code session in the repo folder and paste exactly this:

```
Read HANDOFF.md in this repo fully before doing anything else. It's the
handoff record from a previous session on a different machine — don't
assume you know the context, everything you need is in that file. Once
you've read it, summarize back to me what state the project is in and
what the open items are, then wait for my instruction.
```

---

## Credential checklist — manual steps (do these yourself, nothing here is a secret value)

Do this BEFORE wiping the old laptop.

**Step 1 — Back up `.env` from the old machine.**
It lives at `C:\Users\Mosaic\PDP-Monitor\.env`. Copy it to a password manager (secure note / secure file attachment) or an encrypted USB drive. **Do not** email it, Slack it, or paste it into any chat — including to yourself — since that leaves a permanent copy sitting in a service's servers/logs outside your control.

**Step 2 — On the Mac, recreate `.env` in the repo root with these exact variable names** (values come from Step 1, or better, regenerate fresh — see note below):
```
ANTHROPIC_API_KEY=
MOSAIC_MCP_URL=
GITHUB_TOKEN=
MIXPANEL_PROJECT_ID=
MIXPANEL_SERVICE_ACCOUNT_USERNAME=
MIXPANEL_SERVICE_ACCOUNT_SECRET=
GMAIL_SENDER=
GMAIL_APP_PASSWORD=
GMAIL_RECIPIENT=
```

**Recommended instead of copying:** regenerate each key fresh on the Mac and revoke the old one — more secure than moving live secrets across devices at all:
- `ANTHROPIC_API_KEY` → console.anthropic.com → API Keys
- `GITHUB_TOKEN` → GitHub → Settings → Developer settings → Personal access tokens → generate new, then revoke the old one
- `MIXPANEL_*` → Mixpanel project settings → Service Accounts
- `GMAIL_APP_PASSWORD` → Google Account → Security → App passwords

**Step 3 — Google Sheets/Drive access (separate from `.env` entirely).**
This is the one credential that ISN'T a file you can copy — it's a login session tied to the machine. `scraper/sheets_connector.py` and `scraper/version_control_connector.py` both call `google.auth.default()`, which resolves Application Default Credentials from a local config folder, not from the repo. On the Mac:
```bash
brew install --cask google-cloud-sdk   # if gcloud isn't already installed
gcloud auth application-default login --scopes="https://www.googleapis.com/auth/spreadsheets.readonly,https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/cloud-platform"
```
This opens a browser login — sign in with the account that has access to the Umbrella Sheet / NPD Drive folder. Without this step, ad-data and packaging-artwork fetches fail; everything else in the project still works.

---

## What's on GitHub right now

Branch: `claude/project-revamp-brainstorm-bd1bd1` on `github.com/Purvagandhi18/PDP-Monitor` — fully pushed, nothing stranded on the old laptop.

## Moving the repo to your own GitHub account

Three real options — you need your own GitHub login for any of them, this isn't something that can be done on your behalf:

| Option | Who has to act | Notes |
|---|---|---|
| Transfer ownership | Purva (current owner) | Repo Settings → Transfer — needs her cooperation |
| Fork it | You | Fast, but stays tagged as a fork of hers |
| **Fresh repo, full history, your account** | You | **Recommended** — you own it outright, no dependency on Purva |

Steps for the recommended option:
1. Log into GitHub as yourself, create a new **empty** repo (no README/license) — e.g. `kunal-mosaic/pdp-intelligence`.
2. From inside the cloned repo:
   ```bash
   git remote add newrepo <your-new-repo-url>
   git push newrepo claude/project-revamp-brainstorm-bd1bd1:main
   ```
   This carries the entire commit history over — nothing lost, nothing redone.
3. Point future work (and Mind Matters integration, when that happens) at the new repo.

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

## Environment setup on the new machine

The Python venv is NOT committed to git (correctly gitignored) — every fresh clone needs this:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Then follow the Credential checklist above for `.env` and Google auth.

To run a single product's audit: `python main.py --run-now --product "Shilajit Gummies"`
To run the dashboard: `python -m uvicorn dashboard.app:app --port 8000` then open `http://localhost:8000`
To publish the combined report: `python -m tools.publish_all` (must be run as a module, not a script directly, or it fails on `ModuleNotFoundError: tools`)

## A few gotchas worth knowing (things that cost real time today)

- **GitHub Pages here deploys via a GitHub Actions workflow** (`.github/workflows/deploy-pages.yml` on the `gh-pages` branch), not classic branch-serving. Pushing to `gh-pages` triggers a deploy, but it can fail — check the Actions tab if the live site looks stale after a publish. (Today it failed due to a live GitHub-wide outage, confirmed via githubstatus.com — not a config issue.)
- On Windows, `python` on PATH may resolve to a Microsoft Store stub that does nothing — always use a real venv or the full interpreter path. Shouldn't apply on Mac, but worth knowing if anyone else touches a Windows box later.
- The report builder and the masterdoc-slug logic use different conventions for hyphenated product names (e.g. "Anti-Hairfall Kit S1" → `anti-hairfall_kit_s1.html` vs `anti_hairfall_kit_s1`) — if a script can't find a file, check both spellings before assuming it's missing.

## If picking this up with a fresh Claude session

Use the paste-in prompt at the top of this file. Don't assume any prior Claude memory carries over to a new machine — this file is what travels, nothing else does.
