"""Generate persona.pdf and narrative.pdf for Beard Growth Kit from master brief."""
import docx
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from pathlib import Path

BASE = Path(__file__).parent.parent / "inputs" / "pdfs" / "beard"
BRIEF = BASE / "product_brief.docx"


def _make_pdf(lines, out_path, title):
    doc = SimpleDocTemplate(str(out_path), pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=14, spaceAfter=8)
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=10, spaceAfter=4, leading=14)
    story = [Paragraph(title, h1), Spacer(1, 0.3*cm)]
    for line in lines:
        if not line.strip():
            story.append(Spacer(1, 0.2*cm))
        else:
            safe = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            story.append(Paragraph(safe, body))
    doc.build(story)
    print(f"Created: {out_path}")


def main():
    d = docx.Document(str(BRIEF))
    all_text = [p.text for p in d.paragraphs if p.text.strip()]

    persona_lines, narrative_lines = [], []
    in_persona = in_narrative = False

    for line in all_text:
        low = line.lower()
        if "persona" in low and ("split" in low or "persona 1" in low or "persona -" in low
                                  or ("beard growth kit" in low and "persona" in low)):
            in_persona = True
            in_narrative = False
        if "narrative x persona" in low or "narrative framework" in low or "content bucket" in low:
            in_narrative = True
            in_persona = False
        if "messaging guardrails" in low:
            in_persona = False

        if in_persona:
            persona_lines.append(line)
        if in_narrative:
            narrative_lines.append(line)

    _make_pdf(persona_lines, BASE / "persona.pdf", "Beard Growth Kit — Persona Profile")
    _make_pdf(narrative_lines, BASE / "narrative.pdf", "Beard Growth Kit — Narrative Framework")


if __name__ == "__main__":
    main()
