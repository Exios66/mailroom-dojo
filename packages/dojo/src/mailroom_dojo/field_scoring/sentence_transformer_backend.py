"""An optional similarity backend built on ``sentence-transformers`` (the ``embeddings`` extra).

Nothing in the Dojo uses it unless a caller builds one and passes it in. Importing
this module does not import ``sentence-transformers``; building a backend does. A
model is loaded from local files only, so scoring never downloads anything: pass a
model directory, or a model name already in the local cache.
"""

from __future__ import annotations

import importlib
from collections import OrderedDict
from typing import Any

import numpy

__all__ = ["SentenceTransformerBackend"]


class SentenceTransformerBackend:
    """Cosine similarity of sentence embeddings, clipped to ``[0, 1]``, with a bounded per-instance cache.

    Not safe to share between threads; give each thread its own backend.
    """

    def __init__(self, model_name_or_path: str | None = None, *, model: Any = None, cache_size: int = 1024) -> None:
        """Load a local model by name or path, or wrap an already built ``model`` (anything with ``encode``).

        Raises ``ImportError`` naming the ``embeddings`` extra when
        ``sentence-transformers`` is not installed and no ``model`` is given.
        """
        if (model is None) == (model_name_or_path is None):
            raise ValueError("pass exactly one of model_name_or_path and model")
        if isinstance(cache_size, bool) or not isinstance(cache_size, int) or cache_size < 1:
            raise ValueError(f"cache_size must be a positive integer, got {cache_size!r}")
        if model is None:
            try:
                module = importlib.import_module("sentence_transformers")
            except ImportError as error:
                raise ImportError(
                    "SentenceTransformerBackend needs sentence-transformers: "
                    'pip install "mailroom-dojo[embeddings]"'
                ) from error
            model = module.SentenceTransformer(model_name_or_path, local_files_only=True)
        self._model = model
        self._cache_size = cache_size
        self._vectors: OrderedDict[str, Any] = OrderedDict()

    def similarity(self, first: str, second: str) -> float:
        """Return the clipped cosine similarity of the two texts' normalized embeddings."""
        first_vector, second_vector = self._vector(first), self._vector(second)
        cosine = float(numpy.dot(first_vector, second_vector))
        return min(1.0, max(0.0, cosine))

    def _vector(self, text: str) -> Any:
        """Return the cached or freshly encoded unit vector for ``text``."""
        vector = self._vectors.get(text)
        if vector is not None:
            self._vectors.move_to_end(text)
            return vector
        vector = self._model.encode([text], normalize_embeddings=True)[0]
        self._vectors[text] = vector
        if len(self._vectors) > self._cache_size:
            self._vectors.popitem(last=False)
        return vector
