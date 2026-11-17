"""Generate the original synthetic PDF bundled as the ScholarFlow demo sample."""
from pathlib import Path
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUTPUT = Path(__file__).resolve().parents[1] / "app" / "samples" / "ScholarFlow-demo.pdf"
LINES = [
    "ScholarFlow Synthetic Research Sample",
    "Synthetic demonstration document. All records and numbers are fictional;",
    "this is not a scientific publication.",
    "",
    "Methods",
    "We evaluated a retrieval method on ten synthetic queries.",
    "",
    "Results",
    "Eight of the ten synthetic queries retrieved a relevant record within the",
    "top three results (8/10, 80%).",
    "",
    "Limitations",
    "The sample contains only ten synthetic queries. No baseline system,",
    "confidence interval, or significance test was evaluated.",
    "These fabricated results do not establish performance on real research.",
]

def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(OUTPUT), pagesize=A4, pageCompression=0)
    pdf.setTitle("ScholarFlow Synthetic Research Sample")
    pdf.setAuthor("Zhang Yifan")
    width, height = A4
    x, y = 54, height - 58
    for index, line in enumerate(LINES):
        if not line:
            y -= 12
            continue
        pdf.setFont("Helvetica-Bold" if line in {"Methods", "Results", "Limitations", LINES[0]} else "Helvetica",
                    15 if index == 0 else (12 if line in {"Methods", "Results", "Limitations"} else 10))
        pdf.drawString(x, y, line)
        y -= 22 if index == 0 else 16
    pdf.save()

if __name__ == "__main__":
    main()
