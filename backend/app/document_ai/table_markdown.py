def structure_to_markdown(structure, cell_texts=None):
    """
    Converts a detected table grid into a conservative Markdown table.
    Cell OCR/text association is supplied separately because geometry-to-text
    matching depends on OCR bounding boxes.
    """
    cell_texts = cell_texts or []
    rows = structure.rows
    columns = structure.columns

    if not rows or not columns:
        return ""

    ncols = len(columns)
    matrix = [["" for _ in range(ncols)] for _ in range(len(rows))]

    for cell in cell_texts:
        r = int(cell.get("row", 0))
        c = int(cell.get("column", 0))
        if 0 <= r < len(matrix) and 0 <= c < ncols:
            matrix[r][c] = str(cell.get("text", ""))

    lines = []
    header = matrix[0]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(["---"] * ncols) + " |")
    for row in matrix[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)
