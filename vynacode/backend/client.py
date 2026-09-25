import json
from typing import List

from schema import ExpandQueryResponse

import httpx


class OllamaClient:
    def __init__(self, base_url: str = "http://localhost:11434", timeout: float = 300.0):
        self.base_url = base_url
        self._client = httpx.AsyncClient(timeout=timeout)

    async def ping(self):
        try:
            response = await self._client.get(self.base_url)
            return response.status_code == 200
        except httpx.ConnectError:
            return False

    async def expand_query(self, model: str, query: str) -> List[str]:
        """Generates 10-12 related single-word keywords from a prompt."""
        prompt = f"Respond with a JSON object with a single key 'keywords' containing a comma-separated list of 10-12 concise SINGLE-WORD keywords related to: '{query}'. No phrases, no spaces within keywords."
        content = await self.complete(model, "user", prompt, format="json")
        parsed = ExpandQueryResponse.model_validate_json(content)
        return [kw.strip() for kw in parsed.keywords.split(",") if kw.strip()]

    async def complete(self, model: str, role: str, prompt: str, think: bool = False, format=None):
        payload = {
            "model": model,
            "messages": [{"role": role, "content": prompt}],
            "stream": False,
            "think": think,
            "options": {"num_predict": 4096},
        }
        if format is not None:
            payload["format"] = format
        response = await self._client.post(f"{self.base_url}/api/chat", json=payload)
        response.raise_for_status()
        data = response.json()
        return data["message"]["content"]

    async def stream(self, model: str, role: str, prompt: str, think: bool = False, format=None):
        payload = {
            "model": model,
            "messages": [{"role": role, "content": prompt}],
            "stream": True,
            "think": think,
            "options": {"num_predict": 4096},
        }
        if format is not None:
            payload["format"] = format
        async with self._client.stream(
            "POST", f"{self.base_url}/api/chat", json=payload
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.strip():
                    try:
                        data = json.loads(line)
                        yield data["message"]["content"]
                    except json.JSONDecodeError:
                        continue

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._client.aclose()
