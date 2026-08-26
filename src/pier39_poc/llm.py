"""One chat completion, across model families that disagree about their own parameters.

Reasoning models reject `max_tokens` and require `max_completion_tokens`; older models
reject `max_completion_tokens` and `reasoning_effort`. That disagreement is litellm's
problem now: it knows which parameters each model family supports and drops the rest,
so there is one request rather than a blind three-shape fallback.

What the fallback could never do was say which shape won. `complete` returns a
`Completion` carrying both the text and the parameters actually sent, so a caller can
record it -- `chat.Turn.to_log` does, which is what makes two chat-replay runs
comparable (PENDING.md 3c).

An empty completion is still not an answer, and is still not an error either: it comes
back as empty text, which `labels` degrades to UNCERTAIN and `chat` scores as uncited.

This lives apart from `labels` and `chat` so the two cannot drift on request handling.
No ingest stage imports it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_BUDGET = 512


@dataclass
class Completion:
    """The answer, plus the request shape that produced it."""

    text: str
    params: dict[str, Any] = field(default_factory=dict)


def client_or_default(client: Any = None) -> Any:
    """Retained as the injection seam: tests and callers pass a stub through here.

    litellm's `completion` is a module-level function, so a `None` client means "call
    litellm directly" rather than "build an OpenAI client".
    """
    return client


def _resolved_params(model: str, budget: int, effort: str) -> dict[str, Any]:
    """Pick the request shape this model accepts, instead of discovering it by failing.

    The three shapes are the ones the old fallback tried, in the same order and with the
    same contents -- including the deliberate absence of `temperature` from the reasoning
    shape, which reasoning models reject. What changed is that litellm's capability table
    chooses between them up front, so the shape is known rather than inferred from which
    request stopped erroring.
    """
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
    budget: int = DEFAULT_BUDGET,
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

        # `stream` is never set, so this is a ModelResponse rather than the streaming
        # wrapper the union also allows. Annotated as Any to say so once, here.
        response = litellm.completion(
            model=model, messages=messages, drop_params=True, **params
        )

    text = (response.choices[0].message.content or "").strip()
    return Completion(text=text, params=params)
