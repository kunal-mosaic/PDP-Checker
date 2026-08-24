"""
send_report.py — sends the latest PDP Monitor report via Gmail.

Usage:
    python3 tools/send_report.py --product "Shilajit Gummies" --to recipient@email.com

The email body contains a plain-text score summary.
The full interactive HTML report is attached as a .html file.
Recipient downloads it and opens in any browser to see the full dashboard.
"""

import argparse
import json
import os
import re
import smtplib
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")


def _product_slug(name: str) -> str:
    return re.sub(r"[^\w]", "_", name.lower()).strip("_")


def _latest_report(product_name: str) -> Path:
    slug = _product_slug(product_name)
    reports = sorted(Path("outputs/reports").glob(f"{slug}_*.html"))
    if not reports:
        raise FileNotFoundError(f"No report found for '{product_name}' in outputs/reports/")
    return reports[-1]


def _latest_run_snapshot(product_name: str) -> dict:
    slug = _product_slug(product_name)
    runs = sorted(Path("outputs/runs").glob(f"{slug}_run_*.json"))
    if not runs:
        return {}
    with open(runs[-1]) as f:
        return json.load(f)


def _build_summary(product_name: str, snapshot: dict, pages_url: str = None) -> str:
    """Build a plain-text score summary for the email body."""
    pdps = snapshot.get("pdps", [])
    run_date = snapshot.get("run_date", datetime.utcnow().strftime("%Y-%m-%d"))

    lines = [
        f"Hi,",
        f"",
        f"This is an automated weekly PDP analysis report for {product_name}.",
        f"Run date: {run_date}",
        f"",
        f"{'→ View report: ' + pages_url if pages_url else '→ Full report attached below.'}",
        f"{'→ All reports: https://kunal-mosaic.github.io/PDP-Checker/' if pages_url else ''}",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"SCORES SUMMARY",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    for pdp in pdps:
        url = pdp.get("url", "")
        overall = pdp.get("overall_score", 0)
        status = pdp.get("status", "").upper()
        scores = pdp.get("scores", {})
        short_url = url.replace("https://", "").replace("http://", "")

        lines += [
            f"",
            f"URL: {short_url}",
            f"Overall: {overall:.1f}/10  [{status}]",
            f"  Reviews:          {scores.get('reviews', 0):.1f}",
            f"  Narrative×Persona:{scores.get('persona_narrative', 0):.1f}",
            f"  Copy Health:      {scores.get('copy_health', 0):.1f}",
            f"  Visual Design:    {scores.get('visual_design', 0):.1f}",
            f"  Ad Alignment:     {scores.get('ad_alignment', 0):.1f}",
        ]

    lines += [
        f"",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"",
        f"The full interactive report is attached as an HTML file.",
        f"Download it and open in any browser (Chrome/Safari) to see",
        f"the complete dashboard with tabs, scores, and recommendations.",
        f"",
        f"Regards,",
        f"PDP Monitor",
    ]

    return "\n".join(lines)


def send_report(product_name: str, recipient: str):
    sender   = os.getenv("GMAIL_SENDER")
    password = os.getenv("GMAIL_APP_PASSWORD")

    if not sender or not password:
        raise ValueError("GMAIL_SENDER or GMAIL_APP_PASSWORD not set in .env")

    report_path = _latest_report(product_name)
    snapshot    = _latest_run_snapshot(product_name)

    # Publish to GitHub Pages — get live URL
    pages_url = None
    try:
        import sys, pathlib
        sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
        from tools.publish_report import publish
        pages_url = publish(product_name)
        print(f"  GitHub Pages URL: {pages_url}")
    except Exception as e:
        print(f"  GitHub Pages publish failed: {e} — falling back to attachment")

    summary = _build_summary(product_name, snapshot, pages_url=pages_url)

    # Build email
    msg = MIMEMultipart()
    msg["From"]    = sender
    msg["To"]      = recipient
    msg["Subject"] = f"PDP Monitor — {product_name} | {datetime.utcnow().strftime('%d %b %Y')}"

    # Plain text body with link
    msg.attach(MIMEText(summary, "plain"))

    # Attach HTML as fallback if Pages publish failed
    if not pages_url:
        with open(report_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename={report_path.name}")
        msg.attach(part)

    # Send via Gmail SMTP
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())

    print(f"✓ Email sent to {recipient}")
    print(f"  Product: {product_name}")
    print(f"  Subject: {msg['Subject']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--product",  required=True, help='Product name e.g. "Shilajit Gummies"')
    parser.add_argument("--to",       required=True, help="Recipient email address")
    args = parser.parse_args()
    send_report(args.product, args.to)
