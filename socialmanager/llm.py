"""LLM providers: local by default, remote only when explicitly allowed.

The brief is precise about the failure mode: *if the local model is not
available, do not block and do not publish half-written content.*  So there is
no provider that "tries harder" here.  :func:`build_provider` returns one of

* :class:`OllamaProvider` - the local model named in ``social.yaml``;
* :class:`OpenAiCompatibleProvider` - a remote endpoint, used **only** when
  ``llm.allow_remote_fallback`` is true *and* its key and URL are configured;
* :class:`UnavailableProvider` - no model at all, which is a perfectly good
  state: the content engine then writes from deterministic templates and says so
  in the ``generator`` column of every draft.

A provider never sees a social token: the prompt contains repo facts and the
writing rules, nothing else.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from . import obs, secrets
from .http import header_value, require_safe_url

#: Fixed writing rules handed to every model.  Keeping them here (instead of in
#: the content engine) means a provider swap cannot silently drop a rule.
SYSTEM_RULES = """You write social media drafts for an open-source, experimental
quantitative-trading research project called SelfTrade.

Hard rules, in order of importance:
1. Never promise or imply profit, returns or performance. Never say "guaranteed",
   "risk-free", "beat the market".
2. Only state facts that appear in the FACTS block you are given. Never invent
   numbers, users, stars, followers, metrics or feedback. If a fact is missing,
   write around it.
3. Always distinguish historical backtests, simulations, paper trading and live
   results when you mention any result. Never present a planned feature as shipped.
4. Do not hide negative or inconclusive results; if the facts contain a failure,
   it is allowed - and usually interesting - to say so.
5. Never include an email address, phone number, API key or token.
6. Follow the platform limits exactly. Hashtags go in the separate field, not in
   the body.
7. Write naturally in the requested language. Do not translate word for word.

Answer with JSON only, matching the requested schema."""


@dataclass
class ChatResult:
    ok: bool
    text: str = ""
    data: dict[str, Any] | None = None
    model: str = ""
    error: str = ""
    duration_ms: int = 0

    @property
    def generator(self) -> str:
        return f"llm:{self.model}" if self.model else "llm"


class LlmProvider:
    """The interface the content engine uses.  ``name`` ends up in the drafts."""

    name = "none"
    model = ""

    def available(self) -> bool:
        return False

    def chat_json(self, system: str, user: str, schema: dict[str, Any] | None = None,
                  **_: Any) -> ChatResult:
        return ChatResult(ok=False, error="provider not available", model=self.model)

    def chat_text(self, system: str, user: str, **_: Any) -> ChatResult:
        return ChatResult(ok=False, error="provider not available", model=self.model)

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model, "available": self.available()}


class UnavailableProvider(LlmProvider):
    """No model configured, or the configured one does not answer."""

    name = "none"

    def __init__(self, reason: str = "disabled in configuration"):
        self.reason = reason

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "model": "", "available": False, "reason": self.reason}


#: Why the model calls are hardened here and not only in ``HttpClient``: the providers talk to
#: an endpoint from configuration instead of through the shared client, so the two rules that
#: matter for a credential would otherwise not apply to the one request in this project that
#: carries a remote API key.  A redirect is never followed (it would re-send the key to
#: whatever host answered), and ``https`` is required exactly when the request carries a key -
#: a plain-``http`` Ollama on the local network sends no credential and stays allowed.
_NO_REDIRECT = {"allow_redirects": False}


class OllamaProvider(LlmProvider):
    """Local Ollama (``/api/tags``, ``/api/chat``).  Timeouts return, they never raise."""

    name = "ollama"

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen2.5-coder:1.5b",
        *,
        temperature: float = 0.4,
        num_ctx: int = 8192,
        num_predict: int = 900,
        timeout: float = 180.0,
        post: Callable[..., Any] | None = None,
        get: Callable[..., Any] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = float(temperature)
        self.num_ctx = int(num_ctx)
        self.num_predict = int(num_predict)
        self.timeout = float(timeout)
        self._post = post or requests.post
        self._get = get or requests.get
        self._probe: bool | None = None

    def available(self) -> bool:
        if self._probe is not None:
            return self._probe
        try:
            response = self._get(f"{self.base_url}/api/tags",
                                 timeout=min(3.0, self.timeout), **_NO_REDIRECT)
            self._probe = int(getattr(response, "status_code", 0)) == 200
        except Exception as exc:  # noqa: BLE001 - unreachable is a normal state
            obs.warning("llm.unreachable", detail={"provider": self.name,
                                                   "error": type(exc).__name__})
            self._probe = False
        return self._probe

    def models(self) -> list[str]:
        try:
            response = self._get(f"{self.base_url}/api/tags",
                                 timeout=min(5.0, self.timeout), **_NO_REDIRECT)
            return [str(m.get("name", "")) for m in (response.json().get("models") or [])]
        except Exception:  # noqa: BLE001
            return []

    def chat_json(self, system: str, user: str, schema: dict[str, Any] | None = None,
                  **_: Any) -> ChatResult:
        started = time.monotonic()
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
            response = self._post(
                f"{self.base_url}/api/chat", json=payload, timeout=self.timeout, **_NO_REDIRECT
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            return ChatResult(
                ok=False, model=self.model, error=secrets.redact(str(exc)),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        content = ((body.get("message") or {}).get("content") or "").strip()
        duration = int((time.monotonic() - started) * 1000)
        if not content:
            return ChatResult(ok=False, model=self.model, error="empty reply", duration_ms=duration)
        try:
            parsed = json.loads(content)
        except ValueError:
            return ChatResult(ok=False, model=self.model, error="reply is not JSON",
                              text=content, duration_ms=duration)
        if not isinstance(parsed, dict):
            return ChatResult(ok=False, model=self.model, error="reply is not an object",
                              text=content, duration_ms=duration)
        return ChatResult(ok=True, text=content, data=parsed, model=self.model,
                          duration_ms=duration)


class OpenAiCompatibleProvider(LlmProvider):
    """A remote ``/chat/completions`` endpoint.  Opt-in, and never a silent fallback."""

    name = "openai_compatible"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key_env: str = "",
        temperature: float = 0.4,
        timeout: float = 120.0,
        post: Callable[..., Any] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key_env = api_key_env
        self.temperature = float(temperature)
        self.timeout = float(timeout)
        self._post = post or requests.post
        self._probe: bool | None = None

    def available(self) -> bool:
        if self._probe is None:
            self._probe = bool(self.base_url and self.model and self.api_key_env
                               and secrets.present(self.api_key_env))
        return self._probe

    def chat_json(self, system: str, user: str, schema: dict[str, Any] | None = None,
                  **_: Any) -> ChatResult:
        started = time.monotonic()
        key = secrets.read(self.api_key_env)
        if not key:
            return ChatResult(ok=False, model=self.model, error="api key not set")
        prompt = user
        if schema:
            prompt = (
                f"{user}\n\nAnswer with a single JSON object matching this schema:\n"
                f"{json.dumps(schema, ensure_ascii=False)}"
            )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
        }
        try:
            # This is the one outbound request in the project that carries a remote API key,
            # and it does not go through HttpClient, so both rules are enforced here too: the
            # endpoint must not be plain http off the machine, and a redirect is never chased
            # with the Authorization header attached.
            url = require_safe_url(f"{self.base_url}/chat/completions")
            response = self._post(
                url,
                json=payload,
                headers={"Authorization": header_value("Authorization", f"Bearer {key}")},
                timeout=self.timeout,
                allow_redirects=False,
                verify=True,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            return ChatResult(ok=False, model=self.model, error=secrets.redact(str(exc)),
                              duration_ms=int((time.monotonic() - started) * 1000))
        duration = int((time.monotonic() - started) * 1000)
        choices = body.get("choices") or []
        content = ((choices[0].get("message") or {}).get("content") if choices else "") or ""
        try:
            parsed = json.loads(content)
        except ValueError:
            return ChatResult(ok=False, model=self.model, error="reply is not JSON",
                              text=str(content), duration_ms=duration)
        if not isinstance(parsed, dict):
            return ChatResult(ok=False, model=self.model, error="reply is not an object",
                              duration_ms=duration)
        return ChatResult(ok=True, text=str(content), data=parsed, model=self.model,
                          duration_ms=duration)


def build_provider(settings: Any, *, post: Callable[..., Any] | None = None,
                   get: Callable[..., Any] | None = None,
                   probe: bool = True) -> LlmProvider:
    """Pick the provider for this configuration, preferring the local one."""
    cfg = settings.llm
    if not cfg.enabled or str(cfg.provider) == "none":
        return UnavailableProvider("llm.enabled is false")

    local: LlmProvider | None = None
    if str(cfg.provider) == "ollama":
        local = OllamaProvider(
            cfg.base_url, cfg.model,
            temperature=cfg.temperature, num_ctx=cfg.num_ctx,
            num_predict=cfg.num_predict, timeout=cfg.timeout, post=post, get=get,
        )
    elif str(cfg.provider) == "openai_compatible":
        local = OpenAiCompatibleProvider(
            cfg.base_url, cfg.model, api_key_env=cfg.api_key_env,
            temperature=cfg.temperature, timeout=cfg.timeout, post=post,
        )

    if local is not None and (not probe or local.available()):
        return local

    if (cfg.allow_remote_fallback and cfg.remote_base_url and cfg.remote_model
            and secrets.present(cfg.remote_api_key_env)):
        remote = OpenAiCompatibleProvider(
            cfg.remote_base_url, cfg.remote_model, api_key_env=cfg.remote_api_key_env,
            temperature=cfg.temperature, timeout=cfg.timeout, post=post,
        )
        obs.warning("llm.fallback_remote", detail={"model": cfg.remote_model})
        return remote

    reason = (
        f"{cfg.provider} at {cfg.base_url} is not reachable"
        if local is not None else f"provider {cfg.provider!r} is unknown"
    )
    if cfg.allow_remote_fallback:
        reason += "; remote fallback is allowed but not configured"
    return UnavailableProvider(reason)


#: The JSON contract the content engine asks a model to fill, per variant.
VARIANT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "body": {"type": "string"},
        "hashtags": {"type": "string"},
        "alt_text": {"type": "string"},
        "risk": {"type": "string", "enum": ["low", "medium", "high", "sensitive"]},
        "unsupported": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["body"],
}

#: The contract for the weekly plan proposal (Mode C planning).
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "category": {"type": "string"},
                    "platforms": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string"},
                },
                "required": ["title", "category"],
            },
        }
    },
    "required": ["items"],
}
