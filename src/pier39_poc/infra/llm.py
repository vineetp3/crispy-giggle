"""One chat completion, across model families that disagree about their own parameters.

Used by ingest.labels and evaluation.chat; no ingest stage imports it for extraction.
Gotchas: docs/reference/infra.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pier39_poc.core.tuning import DEFAULTS


@dataclass
class Completion:

    text: str
    params: dict[str, Any] = field(default_factory=dict)


def client_or_default(client: Any = None) -> Any:
    return client


def _resolved_params(model: str, budget: int, effort: str) -> dict[str, Any]:
    try:
        import litellm

        supported = set(litellm.get_supported_openai_params(model=model) or ())
    except Exception:
        supported = set()

    if "reasoning_effort" in supported and "max_completion_tokens" in supported:
        return {"max_completion_tokens": budget, "reasoning_effort": effort}
    if "max_completion_tokens" in supported:
        return {"max_completion_tokens": budget}
    return {"temperature": 0, "max_tokens": budget}


def complete(
    client: Any,
    model: str,
    prompt: str,
    budget: int = DEFAULTS.llm.default_budget,
    effort: str = "low",
) -> Completion:
    messages = [{"role": "user", "content": prompt}]
    params = _resolved_params(model, budget, effort)

    if client is not None:
        response: Any = client.chat.completions.create(
            model=model, messages=messages, **params
        )
    else:
        import litellm

        response = litellm.completion(
            model=model, messages=messages, drop_params=True, **params
        )

    text = (response.choices[0].message.content or "").strip()
    return Completion(text=text, params=params)
