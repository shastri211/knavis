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


def render_page_png(pdf_path: Path, page_no: int, dpi=144, clip: tuple[float, float, float, float] | None = None) -> bytes:
    """One PDF page (or a clipped region of it) as PNG bytes, in memory: nothing is written to disk."""
    with fitz.open(pdf_path) as doc:
        page = doc[page_no - 1]
        pixmap = page.get_pixmap(dpi=dpi, alpha=False, clip=fitz.Rect(*clip) if clip else None)
        return pixmap.tobytes("png")


def subset_pdf(pdf_path: Path, pages: list[int]) -> bytes:
    """A new PDF holding only the given 1-based pages (in that order), so only those leave the machine."""
    with fitz.open(pdf_path) as source, fitz.open() as subset:
        for page in pages:
            subset.insert_pdf(source, from_page=page - 1, to_page=page - 1)
        return subset.tobytes()
