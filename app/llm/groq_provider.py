"""
Groq implementation of LLMProvider.

To swap to another LLM: copy this file, change the client + model, done.
"""

from groq import Groq

from app.config import settings
from app.llm.base import LLMProvider


class GroqProvider(LLMProvider):
    def __init__(self, model: str | None = None):
        self._client = Groq(api_key=settings.GROQ_API_KEY)
        self._model = model or settings.GROQ_MODEL

    def complete(self, prompt: str, system: str = "", timeout: float | None = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict = {"model": self._model, "messages": messages}
        if timeout is not None:
            # Per-call timeout (seconds). The Groq SDK forwards this to the
            # underlying httpx client, so a hung LLM call cannot block the
            # advisory orchestrator past this budget.
            kwargs["timeout"] = timeout

        response = self._client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""

    def complete_json(self, prompt: str, system: str = "", timeout: float | None = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict = {
            "model": self._model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        if timeout is not None:
            kwargs["timeout"] = timeout

        response = self._client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""


# Module-level singleton so other modules can `from app.llm.groq_provider import llm`
# To swap providers, change this line to instantiate a different class.
llm: LLMProvider = GroqProvider()
