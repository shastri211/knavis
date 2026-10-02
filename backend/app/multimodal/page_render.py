from pathlib import Path
import fitz

def render_pdf_pages(pdf_path: Path, output_dir: Path, dpi=144):
    """
    Render only pages that require visual/OCR processing.
    144 DPI is deliberately moderate for CPU-side rendering.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    with fitz.open(pdf_path) as doc:
        for page_no, page in enumerate(doc, 1):
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            target = output_dir / f"page_{page_no}.png"
            pix.save(target)
            results.append((page_no, target))
    return results
