import base64
import httpx
from dataclasses import dataclass

@dataclass
class TableStructure:
    page: int
    bbox: list[dict]
    cells: list[dict]
    rows: list[dict]
    columns: list[dict]

class NVIDIATableStructureClient:
    """
    Hosted NVIDIA Table Structure v1 adapter.

    Current NeMo Retriever quickstart documents:
      https://ai.api.nvidia.com/v1/cv/nvidia/nemotron-table-structure-v1
    """
    def __init__(
        self,
        api_key: str,
        invoke_url: str = "https://ai.api.nvidia.com/v1/cv/nvidia/nemotron-table-structure-v1",
    ):
        self.api_key = api_key
        self.invoke_url = invoke_url

    async def detect(self, image: bytes, page: int, mime_type="image/png"):
        if not self.api_key:
            raise RuntimeError("NVIDIA API key is not configured")

        encoded = base64.b64encode(image).decode("ascii")
        payload = {
            "input": [{
                "type": "image_url",
                "url": f"data:{mime_type};base64,{encoded}",
            }]
        }

        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                self.invoke_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        r.raise_for_status()
        data = r.json()

        results = data.get("data", data.get("results", []))
        cells, rows, columns = [], [], []
        page_bbox = []

        for item in results:
            label = str(item.get("label") or item.get("class") or "")
            bbox = item.get("bounding_box") or item.get("bbox") or {}
            points = bbox.get("points", bbox) if isinstance(bbox, dict) else bbox
            record = {
                "label": label,
                "confidence": float(item.get("confidence", item.get("score", 0.0))),
                "bbox": points if isinstance(points, list) else [],
            }
            if label in {"cell", "merged_cell"}:
                cells.append(record)
            elif label == "row":
                rows.append(record)
            elif label == "column":
                columns.append(record)
            else:
                page_bbox = record["bbox"]

        return TableStructure(
            page=page,
            bbox=page_bbox,
            cells=cells,
            rows=rows,
            columns=columns,
        )
