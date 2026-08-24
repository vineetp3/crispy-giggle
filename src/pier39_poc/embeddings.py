"""OpenAI embeddings, behind one function so the provider stays swappable.

Two operational details that matter:

* Truncating via the `dimensions` parameter can break unit length, so we normalise
  ourselves. Whether OpenAI returns unit-normalised vectors is not stated in the API
  reference, so the first batch is measured and logged rather than assumed -- the
  measurement decides whether the pgvector operator class could be inner product.
* Batch limits are 2048 inputs and 300,000 tokens per request. Both pilot stores fit
  in a handful of calls.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from openai import OpenAI

MAX_INPUTS_PER_REQUEST = 2048
# Conservative: the documented cap is 300k tokens summed across a request.
MAX_CHARS_PER_REQUEST = 400_000


@dataclass
class EmbeddingStats:
    requests: int = 0
    inputs: int = 0
    first_batch_norms: list[float] = field(default_factory=list)

    @property
    def looked_normalised(self) -> bool | None:
        if not self.first_batch_norms:
            return None
        return all(abs(n - 1.0) < 0.01 for n in self.first_batch_norms)

    def summary(self) -> str:
        if not self.first_batch_norms:
            return f"{self.requests} request(s), {self.inputs} input(s)"
        sample = ", ".join(f"{n:.4f}" for n in self.first_batch_norms[:3])
        verdict = "unit-normalised" if self.looked_normalised else "NOT unit-normalised"
        return (
            f"{self.requests} request(s), {self.inputs} input(s); "
            f"first-batch L2 norms [{sample}] -> {verdict}"
        )


def l2_normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return vector
    return [v / norm for v in vector]


def _batches(texts: list[str]) -> list[list[str]]:
    out: list[list[str]] = []
    current: list[str] = []
    chars = 0
    for text in texts:
        size = len(text)
        if current and (len(current) >= MAX_INPUTS_PER_REQUEST or chars + size > MAX_CHARS_PER_REQUEST):
            out.append(current)
            current, chars = [], 0
        current.append(text)
        chars += size
    if current:
        out.append(current)
    return out


class Embedder:
    def __init__(self, model: str, dimensions: int, client: OpenAI | None = None):
        self.model = model
        self.dimensions = dimensions
        self._client = client or OpenAI()
        self.stats = EmbeddingStats()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for batch in _batches(texts):
            response = self._client.embeddings.create(
                model=self.model, input=batch, dimensions=self.dimensions
            )
            self.stats.requests += 1
            self.stats.inputs += len(batch)
            for item in sorted(response.data, key=lambda d: d.index):
                raw = list(item.embedding)
                if not self.stats.first_batch_norms:
                    # Measure before normalising, so the log reflects what OpenAI sent.
                    self.stats.first_batch_norms = [
                        math.sqrt(sum(v * v for v in raw))
                    ]
                out.append(l2_normalise(raw))
        return out

    def embed_one(self, text: str) -> list[float]:
        vectors = self.embed([text])
        return vectors[0] if vectors else []
