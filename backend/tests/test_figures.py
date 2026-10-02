import io
import random

import fitz

from app.ingest.elements import FIGURE
from app.ingest.extract import extract_document
from app.ingest.figures import MIN_SIDE, collect_figures, normalize_png


def noise_png(seed, size=300):
    from PIL import Image
    out = io.BytesIO()
    Image.frombytes("RGB", (size, size), random.Random(seed).randbytes(size * size * 3)).save(out, format="PNG")
    return out.getvalue()


def figures_of(path, kind="pdf", limit=8):
    result = extract_document(path)
    return result, collect_figures(path, kind, result.info, result.elements, limit)


def test_a_vector_drawn_chart_is_found_but_a_ruled_table_or_a_header_rule_is_not(tmp_path):
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_textbox(fitz.Rect(72, 72, 520, 140), "Sales by region this quarter. " * 4, fontsize=11)
    x0, y0 = 100, 520
    page.draw_line((x0, y0), (x0 + 400, y0))
    page.draw_line((x0, y0), (x0, y0 - 250))
    for i, value in enumerate([60, 120, 90, 200, 150, 180]):
        page.draw_rect(fitz.Rect(x0 + 20 + i * 60, y0 - value, x0 + 60 + i * 60, y0), fill=(0.2, 0.4, 0.8))
    for k in range(5):
        page.draw_line((x0, y0 - k * 50), (x0 + 400, y0 - k * 50), color=(0.8, 0.8, 0.8))
    pdf.save(tmp_path / "chart.pdf")

    table = fitz.open()
    sheet = table.new_page()
    sheet.insert_text((72, 72), "Heading text here")
    sheet.draw_line((72, 80), (520, 80))
    for r in range(4):
        for c in range(3):
            cell = fitz.Rect(72 + c * 120, 200 + r * 24, 72 + (c + 1) * 120, 224 + r * 24)
            sheet.draw_rect(cell)
            sheet.insert_text((cell.x0 + 4, cell.y0 + 16), f"v{r}{c}", fontsize=10)
    table.save(tmp_path / "table.pdf")

    _, chart = figures_of(tmp_path / "chart.pdf")
    assert [(f.key, f.locator) for f in chart] == [("p1:fig0", "page 1")]
    _, nothing = figures_of(tmp_path / "table.pdf")
    assert nothing == []


def test_scanned_pages_are_excluded_small_images_dropped_duplicates_merged_and_the_count_capped(tmp_path):
    pdf = fitz.open()
    scan = pdf.new_page()                                               # page-sized image, no text: OCR handles it
    scan.insert_image(scan.rect, stream=noise_png(1))
    for page_number, (seed, size) in enumerate([(2, 300), (2, 300), (3, 300), (4, 60)], 2):
        page = pdf.new_page()
        page.insert_textbox(fitz.Rect(72, 72, 520, 140), "Enough native text here so this page is not scanned at all. " * 3, fontsize=11)
        page.insert_image(fitz.Rect(72, 200, 472, 430), stream=noise_png(seed, size))
    pdf.save(tmp_path / "mixed.pdf")

    result, found = figures_of(tmp_path / "mixed.pdf")
    assert result.info["ocr_pages"] == [1]
    assert {f.page for f in found} <= {2, 3, 4, 5} and 1 not in {f.page for f in found}
    assert len(found) == 3 and len({f.sha for f in found}) == 3          # seed 2 appears twice -> one figure
    assert len(figures_of(tmp_path / "mixed.pdf", limit=2)[1]) == 2


def test_tiny_images_are_not_figures():
    assert normalize_png(noise_png(5, size=MIN_SIDE - 10)) is None
    assert normalize_png(b"not an image") is None
    png, width, height = normalize_png(noise_png(6, size=2000))
    assert max(width, height) == 1568                                    # large images are scaled down


def test_docx_and_pptx_images_are_collected_with_their_location(tmp_path):
    from docx import Document
    from pptx import Presentation
    from pptx.util import Inches

    doc = Document()
    doc.add_paragraph("Report")
    doc.add_picture(io.BytesIO(noise_png(7)))
    doc.add_picture(io.BytesIO(noise_png(8, size=40)))                   # an icon: dropped
    doc.save(tmp_path / "a.docx")
    _, docx_figures = figures_of(tmp_path / "a.docx", "docx")
    assert [f.locator for f in docx_figures] == ["figure 1"]

    (tmp_path / "pic.png").write_bytes(noise_png(9))
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_picture(str(tmp_path / "pic.png"), Inches(1), Inches(1))
    prs.save(tmp_path / "a.pptx")
    _, pptx_figures = figures_of(tmp_path / "a.pptx", "pptx")
    assert [(f.slide, f.locator, f.key) for f in pptx_figures] == [(1, "slide 1", "s1:fig0")]


def test_native_powerpoint_charts_are_read_without_any_model_call(tmp_path):
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    data = CategoryChartData()
    data.categories = ["North", "South"]
    data.add_series("Sales", (10, 20))
    data.add_series("Cost", (5, 8))
    chart = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1), Inches(6), Inches(4), data).chart
    chart.has_title = True
    chart.chart_title.text_frame.text = "Regional results"
    prs.save(tmp_path / "chart.pptx")

    result = extract_document(tmp_path / "chart.pptx")
    (element,) = [e for e in result.elements if e.kind == FIGURE]
    assert element.source == "native" and element.slide == 1 and element.locator == "slide 1, chart"
    assert element.text.splitlines() == [
        "Chart (column clustered): Regional results", "category | Sales | Cost", "North | 10 | 5", "South | 20 | 8"]
