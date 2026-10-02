from dataclasses import dataclass
import httpx

@dataclass
class EmbeddingResult:
    vectors: list[list[float]]
    model: str

class NVIDIAEmbeddingClient:
    """
    Hosted NVIDIA embedding adapter.

    The selected model is intentionally configuration-driven. The API shape is
    kept behind this class so another provider/model can be added without
    changing the retrieval layer.
    """
    def __init__(self, api_key: str, base_url="https://integrate.api.nvidia.com/v1",
                 model="nvidia/llama-nemotron-embed-1b-v2"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def embed(self, texts: list[str], input_type="passage"):
        if not self.api_key:
            raise RuntimeError("NVIDIA API key is not configured")
        payload = {
            "model": self.model,
            "input": texts,
            "input_type": input_type,
            "encoding_format": "float",
            "truncate": "END",
        }
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json=payload,
            )
        r.raise_for_status()
        data = r.json()
        vectors = [x["embedding"] for x in sorted(data["data"], key=lambda x: x["index"])]
        return EmbeddingResult(vectors=vectors, model=self.model)
