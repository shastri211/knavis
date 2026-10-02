from dataclasses import dataclass
import base64
import httpx

@dataclass
class OCRDetection:
    text: str
    confidence: float | None
    bbox: list[dict]       # polygon points, normalised to 0-1 by the service
    page: int | None = None

class NVIDIAOCRClient:
    """
    NVIDIA hosted OCR adapter (NeMo Retriever OCR).

    POST <base_url> with {"input": [{"type": "image_url", "url": "data:<mime>;base64,..."}],
    "merge_levels": ["paragraph"]}. The service documents "word", "sentence" and "paragraph"
    merge levels; paragraph (its default) gives text a retriever can use, where "word" returned
    one detection per word. The hosted endpoint URL has not been verified against a live key.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://ai.api.nvidia.com/v1/ocr",
        merge_level: str = "paragraph",
        client_factory=None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.merge_level = merge_level
        self.client_factory = client_factory or httpx.AsyncClient

    async def image_bytes(self, image: bytes, mime_type="image/png", page=None):
        if not self.api_key:
            raise RuntimeError("NVIDIA API key is not configured")

        encoded = base64.b64encode(image).decode("ascii")
        payload = {
            "input": [{
                "type": "image_url",
                "url": f"data:{mime_type};base64,{encoded}",
            }],
            "merge_levels": [self.merge_level],
        }

        async with self.client_factory(timeout=120) as client:
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
