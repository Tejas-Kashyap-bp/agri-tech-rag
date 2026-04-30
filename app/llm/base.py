"""
Abstract LLM provider.

Every LLM we use (Groq, OpenAI, local Ollama, whatever) must implement this
interface. To swap the LLM, create a new class that implements LLMProvider
and change which class is instantiated in app/llm/__init__.py or wherever
it's wired up. No pipeline code needs to change.
"""

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    @abstractmethod
    def complete(self, prompt: str, system: str = "", timeout: float | None = None) -> str:
        """
        Send a prompt to the LLM and return the text response.

        Args:
            prompt: the user message
            system: optional system message to set behavior

        Returns:
            the LLM's text response

        Raises:
            provider-specific exceptions on API failure. The caller
            (usually a pipeline block) is responsible for wrapping these
            into PipelineError with the correct block name.
        """
        ...

    @abstractmethod
    def complete_json(self, prompt: str, system: str = "", timeout: float | None = None) -> str:
        """
        Same as complete, but instructs the LLM to return valid JSON.

        Returns the raw JSON string. Parsing is the caller's job so the
        caller can decide what to do on JSON parse failure (e.g. retry).
        """
        ...
