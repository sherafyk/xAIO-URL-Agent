from __future__ import annotations

from typing import Any, List


def chat_completion_text(
    client: Any,
    *,
    model: str,
    system_prompt: str,
    user_content: str,
) -> str:
    messages: List[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    resp = client.chat.completions.create(model=model, messages=messages)
    if getattr(resp, "choices", None):
        return resp.choices[0].message.content or ""
    return ""
