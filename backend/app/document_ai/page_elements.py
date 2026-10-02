import base64
import httpx
from dataclasses import dataclass

@dataclass
class PageElement:
    label: str
    confidence: float
    bbox: list[dict]
    page: int

class NVIDIAPageElementsClient:
    """
    Hosted NVIDIA Page Elements v3 adapter.

    Current NeMo Retriever quickstart documents:
      https://ai.api.nvidia.com/v1/cv/nvidia/nemotron-page-elements-v3
    """
    def __init__(
        self,
        api_key: str,
        invoke_url: str = "https://ai.api.nvidia.com/v1/cv/nvidia/nemotron-page-elements-v3",
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
        out = []
        for item in results:
            label = item.get("label") or item.get("class") or item.get("name")
            if not label:
                continue
            bbox = item.get("bounding_box") or item.get("bbox") or {}
            points = bbox.get("points", bbox) if isinstance(bbox, dict) else bbox
            out.append(PageElement(
                label=str(label),
                confidence=float(item.get("confidence", item.get("score", 0.0))),
                bbox=points if isinstance(points, list) else [],
                page=page,
            ))
        return out
