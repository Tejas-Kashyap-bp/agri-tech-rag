"""
Block: Embedding

Converts the text string produced by Text Generation into a vector.

Phase 1: sentence-transformers, local. No API calls, no keys, no cost.
If you later switch to a hosted embedding API, create a new class that
implements EmbeddingProvider and replace the singleton at the bottom.
"""

from abc import ABC, abstractmethod

from sentence_transformers import SentenceTransformer

from app.config import settings
from app.schemas import PipelineError

BLOCK = "Embedding"


class EmbeddingProvider(ABC):
    @abstractmethod
    def embed(self, text: str) -> list[float]:
        ...


class SentenceTransformerProvider(EmbeddingProvider):
    def __init__(self, model_name: str | None = None):
        self._model = SentenceTransformer(model_name or settings.EMBEDDING_MODEL)

    def embed(self, text: str) -> list[float]:
        try:
            vector = self._model.encode(text, normalize_embeddings=True)
        except Exception as exc:
            raise PipelineError(
                block=BLOCK,
                reason="Embedding generation failed",
                detail=str(exc),
                action_required="Retry the upload",
            ) from exc
        return vector.tolist()


# Module-level singleton. Swap this line to change embedding providers.
embedder: EmbeddingProvider = SentenceTransformerProvider()
