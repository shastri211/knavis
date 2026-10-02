from pathlib import Path
import fitz

def render_pdf_pages(pdf_path: Path, output_dir: Path, dpi=144, pages: set[int] | None = None):
    """
    Render PDF pages to PNG for OCR/vision. Pass ``pages`` (1-based) to render only the
    pages that need it; the rest are skipped without being rasterised.
    144 DPI is deliberately moderate for CPU-side rendering.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    with fitz.open(pdf_path) as doc:
        for page_no, page in enumerate(doc, 1):
            if pages is not None and page_no not in pages:
                continue
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            target = output_dir / f"page_{page_no}.png"
            pix.save(target)
            results.append((page_no, target))
    return results
