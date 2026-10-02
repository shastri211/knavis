from dataclasses import dataclass
import base64
import httpx

@dataclass
class OCRDetection:
    text: str
    confidence: float | None
    bbox: list[dict]
    page: int | None = None

class NVIDIAOCRClient:
    """
    NVIDIA hosted Nemotron OCR v2 adapter.

    Current NVIDIA NIM OCR v2 API:
      POST /v1/ocr
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://ai.api.nvidia.com/v1/ocr",
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    async def image_bytes(self, image: bytes, mime_type="image/png", page=None):
        if not self.api_key:
            raise RuntimeError("NVIDIA API key is not configured")

        encoded = base64.b64encode(image).decode("ascii")
        payload = {
            "input": [{
                "type": "image_url",
                "url": f"data:{mime_type};base64,{encoded}",
            }],
            "merge_levels": ["word"],
        }

        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=payload,
            )
        r.raise_for_status()
        data = r.json()

        detections = []
        for item in data.get("data", []):
            for det in item.get("text_detections", []):
                pred = det.get("text_prediction", {})
                box = det.get("bounding_box", {}).get("points", [])
                detections.append(OCRDetection(
                    text=pred.get("text", ""),
                    confidence=pred.get("confidence"),
                    bbox=box,
                    page=page,
                ))
        return detections
