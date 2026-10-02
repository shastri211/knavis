class VisualRouter:
    """
    Decide which specialist to call.

    Page detection runs first.
    OCR is called for text regions/pages where native text is insufficient.
    Table structure is called only for detected tables.
    A VLM is reserved for complex charts/infographics or visual questions.
    """

    def route(self, page_elements):
        labels = {e.label for e in page_elements}
        return {
            "needs_table_structure": "table" in labels,
            "needs_chart_understanding": "chart" in labels or "infographic" in labels,
            "has_text_region": "text" in labels,
        }
