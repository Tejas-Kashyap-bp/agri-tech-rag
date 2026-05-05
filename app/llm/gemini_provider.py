"""
Gemini implementation of LLMProvider.

Uses the google-generativeai SDK. To swap providers, change the singleton
at the bottom of this file (or the import in callers).
"""

import random
import time

import google.generativeai as genai

from app.config import settings
from app.llm.base import LLMProvider


# Substring match against str(exc).lower() — covers transient transport errors
# across SDK versions without depending on specific exception classes.
_TRANSIENT_KEYWORDS = (
    "503", "502", "504",
    "deadline", "unavailable", "exhausted", "rate limit", "timeout",
)


def _is_transient(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in _TRANSIENT_KEYWORDS)


class GeminiProvider(LLMProvider):
    def __init__(self, model: str | None = None):
        genai.configure(api_key=settings.GEMINI_API_KEY)
        self._model_name = model or settings.GEMINI_MODEL
        # Cache models keyed by (system_instruction, json_mode). Bounded by the
        # tiny number of distinct (system, json_mode) combos in this codebase.
        self._model_cache: dict[tuple[str, bool], "genai.GenerativeModel"] = {}

    def _get_model(self, system: str, json_mode: bool):
        key = (system or "", json_mode)
        cached = self._model_cache.get(key)
        if cached is not None:
            return cached
        generation_config: dict = {}
        if json_mode:
            generation_config["response_mime_type"] = "application/json"
        model = genai.GenerativeModel(
            model_name=self._model_name,
            system_instruction=system or None,
            generation_config=generation_config or None,
        )
        self._model_cache[key] = model
        return model

    def _generate_with_retry(self, model, prompt: str, request_options):
        try:
            return model.generate_content(prompt, request_options=request_options)
        except Exception as exc:
            if not _is_transient(exc):
                raise
            time.sleep(0.5 + random.random() * 0.5)
            return model.generate_content(prompt, request_options=request_options)

    @staticmethod
    def _safe_text(response) -> str:
        # response.text raises when finish_reason != STOP (SAFETY, MAX_TOKENS,
        # RECITATION). Surface the finish_reason so the caller's retry isn't
        # firing blind on a silent block.
        try:
            return response.text or ""
        except Exception:
            try:
                fr = response.candidates[0].finish_reason
            except Exception:
                fr = "unknown"
            raise RuntimeError(f"gemini_no_text finish_reason={fr}")

    def complete(self, prompt: str, system: str = "", timeout: float | None = None) -> str:
        model = self._get_model(system, json_mode=False)
        request_options = {"timeout": timeout} if timeout is not None else None
        response = self._generate_with_retry(model, prompt, request_options)
        return self._safe_text(response)

    def complete_json(self, prompt: str, system: str = "", timeout: float | None = None) -> str:
        model = self._get_model(system, json_mode=True)
        request_options = {"timeout": timeout} if timeout is not None else None
        response = self._generate_with_retry(model, prompt, request_options)
        return self._safe_text(response)


# Lazy singleton. Constructing GeminiProvider configures the SDK with the
# API key, so we defer that to first use — importing this module no longer
# fails on a missing key, which keeps tests and offline tools importable.
_llm: LLMProvider | None = None


def get_llm() -> LLMProvider:
    global _llm
    if _llm is None:
        _llm = GeminiProvider()
    return _llm


def __getattr__(name: str):
    if name == "llm":
        return get_llm()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
