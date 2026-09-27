from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = PROJECT.parent / "xbench-evals/data/DeepSearch-2510.smoketest.csv"


@dataclass
class Settings:
    api_key: str = ""
    base_url: str = "https://llm.talkweb.com.cn/v1"
    agent_model: str = "gpu-deepseek-v4-flash"
    judge_model: str = "qwen3.7-max"
    search_provider: str = "serper"
    serper_api_key: str = ""
    iqs_api_key: str = ""
    jina_api_key: str = ""
    extractor_enabled: bool = False
    extractor_model: str = "qwen3.7-flash"
    extractor_threshold_chars: int = 20000
    llm_extra_body: dict = field(default_factory=dict)
    llm_timeout_seconds: float = 90
    llm_max_tokens: int = 4096
    llm_max_retries: int = 2
    task_api_recoveries: int = 1
    protocol_max_repairs: int = 2
    temperature: float = 0.1
    max_rounds: int = 30
    task_timeout_seconds: float = 600
    final_reserve_seconds: float = 45
    max_search_queries: int = 30
    max_visit_pages: int = 15
    max_tool_concurrency: int = 3
    tool_timeout_seconds: float = 25
    max_context_chars: int = 100000
    max_tool_result_chars: int = 20000
    max_download_bytes: int = 15000000
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    cached_input_price_per_million: float | None = None
    search_price_per_1000_requests: float | None = None
    research_as_of: str = ""

    @classmethod
    def load(cls, env_path: Path | None = None):
        load_dotenv(env_path or PROJECT / ".env", override=False)
        result = cls()
        aliases = {"api_key": "NEWAPI_API_KEY", "base_url": "NEWAPI_BASE_URL"}
        for name, current in asdict(result).items():
            value = os.getenv(aliases.get(name, name.upper()))
            if value is None:
                continue
            value = value.strip()
            if isinstance(current, bool):
                if value.lower() not in {"true", "false", "1", "0"}:
                    raise ValueError(f"Invalid boolean setting: {name}")
                parsed = value.lower() in {"true", "1"}
            elif isinstance(current, int):
                parsed = int(value)
            elif isinstance(current, float) or name.endswith("price_per_million") or name.endswith("price_per_1000_requests"):
                parsed = float(value) if value else None
            elif isinstance(current, dict):
                parsed = json.loads(value or "{}")
            else:
                parsed = value
            setattr(result, name, parsed)
        result.base_url = result.base_url.rstrip("/")
        result.validate()
        return result

    def validate(self):
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.query:
            raise ValueError("NEWAPI_BASE_URL must be an HTTP(S) API root, without credentials or query")
        if not isinstance(self.llm_extra_body, dict) or set(self.llm_extra_body) & {"model", "messages", "stream", "tools", "max_tokens"}:
            raise ValueError("LLM_EXTRA_BODY must not override core request fields")
        if self.search_provider not in {"serper", "iqs"}:
            raise ValueError("SEARCH_PROVIDER must be serper or iqs")
        for name in ("max_rounds", "max_search_queries", "max_visit_pages", "max_tool_concurrency", "llm_max_tokens", "max_context_chars", "max_tool_result_chars", "max_download_bytes"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not 0 <= self.llm_max_retries <= 5:
            raise ValueError("LLM_MAX_RETRIES must be between 0 and 5")
        if not 0 <= self.task_api_recoveries <= 2 or not 0 <= self.protocol_max_repairs <= 4:
            raise ValueError("Recovery budgets must be bounded: API 0..2, protocol 0..4")
        if not 0 < self.final_reserve_seconds < self.task_timeout_seconds:
            raise ValueError("Require 0 < FINAL_RESERVE_SECONDS < TASK_TIMEOUT_SECONDS")
        if min(self.llm_timeout_seconds, self.tool_timeout_seconds) <= 0:
            raise ValueError("Timeouts must be positive")
        for name in ("input_price_per_million", "output_price_per_million", "cached_input_price_per_million", "search_price_per_1000_requests"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")

    def missing_credentials(self, search=True):
        missing = []
        if not self.api_key:
            missing.append("NEWAPI_API_KEY")
        if search and not getattr(self, self.search_provider + "_api_key"):
            missing.append(self.search_provider.upper() + "_API_KEY")
        return missing

    def public(self):
        return {key: value for key, value in asdict(self).items() if not key.endswith("api_key")}

    def secrets(self):
        return [self.api_key, self.serper_api_key, self.iqs_api_key, self.jina_api_key]
