import json
import re
from typing import List

from schema import ExpandQueryResponse

# Summaries are English prose, so a function word matches any block whose
# summary happens to contain it. Measured: 'the' alone retrieved four unrelated
# blocks and pushed a real target out of the budget.
_STOPWORDS = frozenset(
    "a an and are as at be but by for from has have if in into is it its of on "
    "or that the this to was were what when where which who will with you your".split()
)


def own_terms(text: str) -> List[str]:
    """Identifiers and content words of a prompt, deduplicated, in order.

    Shared with the step keyword extraction in cli.py so that a prompt and a
    planned step are tokenised by the same rules.
    """
    tokens = (w.lower() for w in re.findall(r"[A-Za-z0-9_]{3,}", text))
    return [w for w in dict.fromkeys(tokens) if not w.isdigit() and w not in _STOPWORDS]

import httpx


def _as_think(think: bool | str) -> bool | str:
    """Coerce think_level's "true"/"false" strings to JSON literals.

    The config stores levels as strings; Ollama wants bare true/false for on/off
    and strings only for effort levels. Other levels pass through untouched.
    """
    if isinstance(think, str) and think.lower() in ("true", "false"):
        return think.lower() == "true"
    return think


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

    async def list_models(self) -> List[str]:
        """Names of locally installed models, for `vynacode doctor`."""
        response = await self._client.get(f"{self.base_url}/api/tags")
        response.raise_for_status()
        return [m.get("name", "") for m in response.json().get("models", [])]

    async def expand_query(self, model: str, query: str, retry=None, verbose: bool = False):
        """Generates 10-12 related single-word keywords from a prompt."""
        # Non-thinking: related words need no reasoning, and own_terms already
        # covers the literal ones for free.
        prompt = (
            "<task>List 10-12 SINGLE-WORD keywords to search this codebase for the request.</task>\n"
            f"<request>{query}</request>\n"
            "<rules>\n"
            "- One word per keyword. No spaces, no phrases.\n"
            "- Copy every identifier the request names, exactly as written.\n"
            "- Add close synonyms and related type/API words.\n"
            "</rules>\n"
            'Output ONLY {"keywords": "word1, word2, word3"}\n'
        )
        if retry:
            parsed = await retry(
                model, prompt, ExpandQueryResponse,
                verbose=verbose, think=False,
            )
        else:
            parsed = ExpandQueryResponse.model_validate_json(
                await self.complete(model, "user", prompt, format=ExpandQueryResponse.model_json_schema())
            )
        if parsed is None:
            return list(own_terms(query))
        keywords = [kw.strip() for kw in parsed.keywords.split(",") if kw.strip()]
        # A small model asked for "related keywords" answers with abstractions and
        # drops the literal symbol, so the prompt's own words are searched too.
        return list(dict.fromkeys([*keywords, *own_terms(query)]))

    async def _chat(self, model: str, role: str, prompt: str, think: bool | str, format) -> dict:
        payload = {
            "model": model,
            "messages": [{"role": role, "content": prompt}],
            "stream": False,
            "think": _as_think(think),
            "options": {"num_predict": 4096},
        }
        if format is not None:
            payload["format"] = format
        response = await self._client.post(f"{self.base_url}/api/chat", json=payload)
        if response.status_code != 200:
            # raise_for_status drops the body, which is where ollama explains why.
            raise RuntimeError(f"Ollama {response.status_code}: {response.text[:300]}")
        return response.json()["message"]

    async def complete(self, model: str, role: str, prompt: str, think: bool | str = False, format=None):
        return (await self._chat(model, role, prompt, think, format))["content"]

    async def complete_with_thinking(self, model: str, role: str, prompt: str, think: bool | str = False, format=None):
        """Same as complete, but also returns the thinking block Ollama puts aside."""
        message = await self._chat(model, role, prompt, think, format)
        return message.get("content", ""), message.get("thinking", "") or ""

    async def stream(self, model: str, role: str, prompt: str, think: bool | str = False, format=None):
        payload = {
            "model": model,
            "messages": [{"role": role, "content": prompt}],
            "stream": True,
            "think": _as_think(think),
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
