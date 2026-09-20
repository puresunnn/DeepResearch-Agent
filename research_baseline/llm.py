from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from .config import Settings
from .trace import Trace


class APIError(RuntimeError):
    def __init__(self, message, status=None, *, retryable=None):
        super().__init__(message)
        self.status = status
        self.retryable = (status == 429 or (status is not None and status >= 500)) if retryable is None else retryable


@dataclass
class Reply:
    content: str
    finish_reason: str


class ModelClient:
    def __init__(self, settings: Settings, trace: Trace, http: httpx.AsyncClient):
        self.settings, self.trace, self.http = settings, trace, http
        self.calls = 0
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0}
        self.missing_usage_calls = 0

    async def complete(self, messages, *, role="agent", model=None, max_tokens=None):
        settings = self.settings
        model = model or settings.agent_model
        payload = {"model": model, "messages": messages, "temperature": settings.temperature,
                   "max_tokens": max_tokens or settings.llm_max_tokens, "stream": False,
                   **settings.llm_extra_body}
        for attempt in range(settings.llm_max_retries + 1):
            self.calls += 1
            call_id = self.calls
            started = time.monotonic()
            self.trace.emit("llm_request", call_id=call_id, role=role, attempt=attempt + 1, payload=payload)
            try:
                response = await self.http.post(settings.base_url + "/chat/completions", json=payload,
                                                headers={"Authorization": f"Bearer {settings.api_key}"},
                                                timeout=settings.llm_timeout_seconds)
                if response.status_code >= 400:
                    raise APIError(f"HTTP {response.status_code}: {response.text[:1500]}", response.status_code)
                data = response.json()
                if data.get("error"):
                    raise APIError(f"Gateway error: {data['error']}")
                self.trace.emit("llm_response", call_id=call_id, role=role, response=data,
                                request_id=response.headers.get("x-request-id"),
                                latency_seconds=round(time.monotonic() - started, 3))
                usage = data.get("usage")
                if isinstance(usage, dict):
                    for name in ("prompt_tokens", "completion_tokens"):
                        self.usage[name] += int(usage.get(name) or 0)
                    self.usage["cached_tokens"] += int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
                    self.usage["reasoning_tokens"] += int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
                else:
                    self.missing_usage_calls += 1
                choice = data["choices"][0]
                message = choice["message"]
                content = message.get("content") or ""
                if not isinstance(content, str) or not content.strip():
                    raise APIError("Model returned no actionable text; check thinking/stream options or model availability")
                # reasoning_content is logged as returned, never parsed as tool calls or final answers.
                return Reply(content.strip(), str(choice.get("finish_reason") or "unknown"))
            except asyncio.CancelledError:
                self.trace.emit("llm_cancelled", call_id=call_id, role=role)
                raise
            except (httpx.HTTPError, APIError, ValueError, KeyError, IndexError, TypeError) as error:
                status = getattr(error, "status", None)
                retryable = isinstance(error, httpx.TransportError) or status == 429 or (status is not None and status >= 500)
                self.trace.emit("llm_error", call_id=call_id, role=role, error=str(error), status=status, retryable=retryable)
                if not retryable or attempt == settings.llm_max_retries:
                    if isinstance(error, APIError):
                        raise
                    raise APIError(f"{type(error).__name__}: {error}", status, retryable=retryable) from error
                await asyncio.sleep(min(2 ** attempt, 4))
        raise APIError("Retry limit reached")

    def metrics(self):
        return {"llm_calls": self.calls, **self.usage, "missing_usage_calls": self.missing_usage_calls}
