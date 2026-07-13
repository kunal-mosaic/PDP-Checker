#!/usr/bin/env python3
"""
Update config.yaml to point each product to its MasterDoc .md file.
Removes the `pdfs:` block and adds `masterDoc:` for each product that has a .md file.
Usage: python tools/wire_masterdocs_to_config.py
"""

import re
import sys
from pathlib import Path

CONFIG_PATH = Path("config.yaml")
MASTERDOC_DIR = Path("inputs/masterdocs")

SLUG_MAP = {
    "Shilajit Gummies":             "shilajit_gummies",
    "Hair Regrowth Kit S2":         "hair_regrowth_s2",
    "Hair Regrowth Kit S3":         "hair_regrowth_s3",
    "Advanced Hair Regrowth Regime":"advanced_hair_regrowth",
    "Anti-Hairfall Kit S1":         "anti_hairfall_s1",
    "Hair Gummies":                 "hair_gummies",
    "Beard Growth Kit":             "beard_growth_kit",
}


def main():
    content = CONFIG_PATH.read_text(encoding="utf-8")
    original = content

    for product_name, slug in SLUG_MAP.items():
        md_path = MASTERDOC_DIR / f"{slug}.md"
        if not md_path.exists():
            print(f"SKIP {product_name} — {md_path} not found")
            continue

        # Check if masterDoc already wired
        if f'masterDoc: "inputs/masterdocs/{slug}.md"' in content:
            print(f"SKIP {product_name} — masterDoc already set")
            continue

        # Find the product block and add masterDoc line after the name line
        # Pattern: find `- name: "Product Name"` and insert masterDoc after it
        name_pattern = re.compile(
            r'(  - name: "' + re.escape(product_name) + r'")',
            re.MULTILINE,
        )
        if not name_pattern.search(content):
            print(f"SKIP {product_name} — not found in config.yaml")
            continue

        replacement = f'\\1\n    masterDoc: "inputs/masterdocs/{slug}.md"'
        content = name_pattern.sub(replacement, content)
        print(f"OK   {product_name} -> inputs/masterdocs/{slug}.md")

    if content != original:
        CONFIG_PATH.write_text(content, encoding="utf-8")
        print("\nconfig.yaml updated.")
    else:
        print("\nNo changes made.")


if __name__ == "__main__":
    main()
