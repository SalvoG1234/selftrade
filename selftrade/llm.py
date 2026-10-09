"""Optional local LLM client (Ollama).

The LLM **never writes trading logic**: it chooses and modulates a bounded set
of parameters.  Every proposal is passed through
:func:`selftrade.genome.validate_genome`, so illegal output is impossible.

Every network call degrades gracefully: if Ollama is not running (or times
out), the client returns ``None`` and the caller falls back to deterministic
mutation.  The run report always declares which proposal source was used.
"""

from __future__ import annotations

import json
from typing import Any

import requests

from .params import FAMILY_ORDER


class OllamaUnavailable(Exception):
    """Raised internally when Ollama cannot be reached."""


# A permissive-but-bounded schema.  ``params`` keys depend on the family, so the
# value type is left open; validate_genome clamps and drops anything illegal.
PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "family": {"type": "string", "enum": list(FAMILY_ORDER)},
        "params": {
            "type": "object",
            "additionalProperties": {"type": ["number", "boolean"]},
        },
        "risk": {
            "type": "object",
            "properties": {
                "stop_loss_pct": {"type": ["number", "null"]},
                "take_profit_pct": {"type": ["number", "null"]},
                "trailing_pct": {"type": ["number", "null"]},
                "trade_shorts": {"type": "boolean"},
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["family"],
}


class OllamaClient:
    """Minimal Ollama HTTP client (``/api/tags``, ``/api/chat``, ``/api/embed``)."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen3-coder:30b",
        embed_model: str = "embeddinggemma",
        num_ctx: int = 8192,
        num_predict: int = 512,
        temperature: float = 0.2,
        timeout: float = 180.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.embed_model = embed_model
        self.num_ctx = int(num_ctx)
        self.num_predict = int(num_predict)
        self.temperature = float(temperature)
        self.timeout = float(timeout)

    # -------------------------------------------------- probing
    def available(self) -> bool:
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=min(5.0, self.timeout))
            return r.status_code == 200
        except requests.RequestException:
            return False

    def models(self) -> list[str]:
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=min(5.0, self.timeout))
            r.raise_for_status()
            return [m.get("name", "") for m in r.json().get("models", [])]
        except (requests.RequestException, ValueError):
            return []

    # -------------------------------------------------- chat
    def chat_json(
        self,
        system: str,
        user: str,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Ask for a structured JSON reply.  Returns ``None`` on any failure."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": schema or "json",
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
            },
        }
        try:
            r = requests.post(
                f"{self.base_url}/api/chat", json=payload, timeout=self.timeout
            )
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            return None

        content = (data.get("message") or {}).get("content", "")
        if not isinstance(content, str) or not content.strip():
            return None
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def chat_text(self, system: str, user: str) -> str | None:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
            },
        }
        try:
            r = requests.post(
                f"{self.base_url}/api/chat", json=payload, timeout=self.timeout
            )
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            return None
        content = (data.get("message") or {}).get("content", "")
        return content.strip() if isinstance(content, str) and content.strip() else None

    # -------------------------------------------------- embeddings
    def embed(self, text: str) -> list[float] | None:
        try:
            r = requests.post(
                f"{self.base_url}/api/embed",
                json={"model": self.embed_model, "input": text},
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            return None
        if "embeddings" in data and data["embeddings"]:
            return list(data["embeddings"][0])
        if "embedding" in data and data["embedding"]:
            return list(data["embedding"])
        return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
