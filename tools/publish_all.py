"""
publish_all.py — wipe gh-pages clean and publish every product's latest report.

Removes ALL previously published report files (stale links) so the index lists
only the products currently in config.yaml. Each product is copied to
{slug}.html and a fresh index.html is rebuilt.

Usage:
    python3 tools/publish_all.py
"""

import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from tools.publish_report import (
    REPO_ROOT, REMOTE, PAGES_BRANCH, PAGES_BASE,
    _product_slug, _latest_report, _run, _rebuild_index, _authenticated_remote,
)


def publish_all() -> str:
    cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text())
    products = [p["name"] for p in cfg["products"]]

    _run(["git", "fetch", REMOTE, PAGES_BRANCH])
    worktree = REPO_ROOT / ".gh-pages-worktree"
    if worktree.exists():
        shutil.rmtree(worktree)
    _run(["git", "worktree", "add", str(worktree), f"{REMOTE}/{PAGES_BRANCH}"])

    try:
        # Wipe ALL existing html (stale links) — start clean
        removed = 0
        for f in worktree.glob("*.html"):
            f.unlink()
            removed += 1
        print(f"Wiped {removed} previously published file(s)")

        # Copy each product's latest report
        published = []
        for name in products:
            try:
                report = _latest_report(name)
            except FileNotFoundError:
                print(f"  ⚠ no report for '{name}' — skipping")
                continue
            dest = f"{_product_slug(name)}.html"
            shutil.copy(report, worktree / dest)
            published.append(name)
            print(f"  + {name} -> {dest}  ({report.name})")

        _rebuild_index(worktree)

        # GitHub Pages runs branches through Jekyll by default, which can choke on
        # stray {{ }} / {% %} sequences in raw report HTML and silently fail the build
        # (old content just keeps being served, with no visible error). .nojekyll
        # disables that processing so files are served as-is.
        (worktree / ".nojekyll").touch()

        _run(["git", "add", "-A"], cwd=worktree)
        status = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=worktree
        ).stdout.strip()
        if status:
            _run(["git", "commit", "-m", "republish: wipe stale links, publish all current products"], cwd=worktree)
            _run(["git", "push", _authenticated_remote(), f"HEAD:{PAGES_BRANCH}"], cwd=worktree)
            print(f"\nPublished {len(published)} products")
        else:
            print("\nNo changes to publish")

        print(f"  Index: {PAGES_BASE}/")
        return f"{PAGES_BASE}/"
    finally:
        _run(["git", "worktree", "remove", "--force", str(worktree)])


if __name__ == "__main__":
    print(publish_all())
