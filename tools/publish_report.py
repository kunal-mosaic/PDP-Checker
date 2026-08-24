"""
publish_report.py — pushes the latest report to GitHub Pages (gh-pages branch).

After every run, the latest HTML report is copied to the gh-pages branch as
index.html and pushed. GitHub Pages serves it at:
  https://kunal-mosaic.github.io/PDP-Checker/

Usage:
    python3 tools/publish_report.py --product "Shilajit Gummies"

Returns the live GitHub Pages URL so send_report.py can include it in the email.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

REPO_ROOT    = Path(__file__).parent.parent
PAGES_BASE   = "https://kunal-mosaic.github.io/PDP-Checker"
REMOTE       = "origin"
PAGES_BRANCH = "gh-pages"
GITHUB_USER  = "kunal-mosaic"
GITHUB_REPO  = "PDP-Checker"


def _product_slug(name: str) -> str:
    return re.sub(r"[^\w]", "_", name.lower()).strip("_")


def _latest_report(product_name: str) -> Path:
    reports_dir = REPO_ROOT / "outputs" / "reports"
    # The report builder names files with name.lower().replace(' ','_'), which
    # KEEPS hyphens (e.g. "anti-hairfall_kit_s1"). _product_slug converts those
    # to underscores. Try both conventions so neither mismatches.
    builder_slug = product_name.lower().replace(" ", "_")
    candidates = []
    for slug in {builder_slug, _product_slug(product_name)}:
        candidates += list(reports_dir.glob(f"{slug}_*.html"))
    if not candidates:
        raise FileNotFoundError(f"No report found for '{product_name}'")
    return sorted(candidates)[-1]


def _run(cmd: list, cwd=None) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd or REPO_ROOT)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{result.stderr}")
    return result.stdout.strip()


def _rebuild_index(worktree_path: Path) -> None:
    """Regenerate index.html listing all product reports in the gh-pages branch."""
    reports = sorted(worktree_path.glob("*_gummies*.html")) + \
              sorted(worktree_path.glob("*_regrowth*.html")) + \
              sorted(worktree_path.glob("*_gummies*.html"))
    # Deduplicate while preserving order
    seen, unique = set(), []
    for r in sorted(worktree_path.glob("*.html")):
        if r.name != "index.html" and r.name not in seen:
            seen.add(r.name)
            unique.append(r)

    links = "\n".join(
        f'<li><a href="{r.name}">'
        f'{r.stem.replace("_", " ").title()}'
        f'</a></li>'
        for r in unique
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PDP Monitor — Reports</title>
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    background:#f5f5f3;color:#1a1a1a;max-width:600px;margin:60px auto;padding:0 24px}}
  h1{{font-size:22px;font-weight:800;margin-bottom:6px}}
  p{{color:#777;font-size:14px;margin-bottom:32px}}
  ul{{list-style:none;padding:0;display:flex;flex-direction:column;gap:12px}}
  li a{{display:block;background:#fff;border:1px solid #e5e5e2;border-radius:10px;
    padding:16px 20px;font-weight:700;font-size:15px;color:#1a1a1a;text-decoration:none}}
  li a:hover{{border-color:#aaa;background:#fafaf8}}
</style>
</head>
<body>
<h1>PDP Monitor</h1>
<p>Click a product to open its full interactive report.</p>
<ul>{links}</ul>
</body>
</html>"""

    (worktree_path / "index.html").write_text(html)


def _authenticated_remote() -> str:
    """Build remote URL with GitHub token for headless push."""
    token = os.getenv("GITHUB_TOKEN", "")
    if token:
        return f"https://{GITHUB_USER}:{token}@github.com/{GITHUB_USER}/{GITHUB_REPO}.git"
    return f"https://github.com/{GITHUB_USER}/{GITHUB_REPO}.git"


def publish(product_name: str) -> str:
    """
    Push the latest report for product_name to gh-pages as index.html.
    Returns the live GitHub Pages URL.
    """
    report = _latest_report(product_name)
    print(f"Publishing: {report.name}")

    # Save current branch to restore after
    current_branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])

    try:
        # Fetch latest gh-pages
        _run(["git", "fetch", REMOTE, PAGES_BRANCH])

        # Use worktree to avoid disturbing current branch
        worktree_path = REPO_ROOT / ".gh-pages-worktree"
        if worktree_path.exists():
            shutil.rmtree(worktree_path)

        _run(["git", "worktree", "add", str(worktree_path), f"{REMOTE}/{PAGES_BRANCH}"])

        try:
            # Each product gets its own file e.g. shilajit_gummies.html
            product_file = f"{_product_slug(product_name)}.html"
            shutil.copy(report, worktree_path / product_file)

            # Rebuild index.html listing all products
            _rebuild_index(worktree_path)

            # GitHub Pages runs branches through Jekyll by default, which can choke on
            # stray {{ }} / {% %} sequences in raw report HTML and silently fail the build
            # (old content just keeps being served, with no visible error). .nojekyll
            # disables that processing so files are served as-is.
            (worktree_path / ".nojekyll").touch()

            # Commit and push
            files_to_add = [product_file, "index.html", ".nojekyll"]
            _run(["git", "add"] + files_to_add, cwd=worktree_path)
            status = subprocess.run(
                ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=worktree_path
            ).stdout.strip()
            if status:
                _run(["git", "commit", "-m", f"report: {report.name}"], cwd=worktree_path)
                _run(["git", "push", _authenticated_remote(), f"HEAD:{PAGES_BRANCH}"], cwd=worktree_path)
            else:
                print("  No changes — report already published")

            product_url = f"{PAGES_BASE}/{product_file}"
            print(f"✓ Published to GitHub Pages")
            print(f"  URL: {product_url}")
            return product_url

        finally:
            _run(["git", "worktree", "remove", "--force", str(worktree_path)])

    except Exception as e:
        print(f"✗ Publish failed: {e}", file=sys.stderr)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", required=True)
    args = parser.parse_args()
    url = publish(args.product)
    print(url)
