"""One chat completion, across model families that disagree about their own parameters.

Reasoning models reject `max_tokens` and require `max_completion_tokens`, and they spend
that budget thinking before emitting anything, so a budget sized for a one-word answer comes
back empty rather than raising. Older models reject `max_completion_tokens` and
`reasoning_effort`. The request is therefore attempted in the modern shape first and falls
back, and an empty completion counts as a failed attempt rather than as an answer.

This lives apart from `labels` and `chat` so the two cannot drift on request handling. No
ingest stage imports it.
"""

from __future__ import annotations

from typing import Any

DEFAULT_BUDGET = 512


def client_or_default(client: Any = None) -> Any:
    if client is not None:
        return client
    from openai import OpenAI

    return OpenAI()


def complete(
    client: Any,
    model: str,
    prompt: str,
    budget: int = DEFAULT_BUDGET,
    effort: str = "low",
) -> str:
    messages = [{"role": "user", "content": prompt}]
    attempts: tuple[dict[str, Any], ...] = (
        {"max_completion_tokens": budget, "reasoning_effort": effort},
        {"max_completion_tokens": budget},
        {"temperature": 0, "max_tokens": budget},
    )
    last: Exception | None = None
    for kwargs in attempts:
        try:
            response = client.chat.completions.create(
                model=model, messages=messages, **kwargs
            )
        except Exception as exc:
            last = exc
            continue
        text = (response.choices[0].message.content or "").strip()
        if text:
            return text
    if last is not None:
        raise last
    return ""
