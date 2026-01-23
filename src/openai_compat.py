from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional, Tuple, Type

from pydantic import BaseModel


def _has_responses(client: Any) -> bool:
    return bool(getattr(client, "responses", None) and hasattr(client.responses, "parse"))


def _has_responses_create(client: Any) -> bool:
    return bool(getattr(client, "responses", None) and hasattr(client.responses, "create"))


def _openai_env_debug() -> str:
    try:
        import openai
        version = getattr(openai, "__version__", "unknown")
        location = getattr(openai, "__file__", "unknown")
    except Exception:
        version = "unknown"
        location = "unknown"
    return f"python={sys.executable} openai_version={version} openai_path={location}"


def _ensure_responses_api(client: Any) -> None:
    if _has_responses(client):
        return
    if os.getenv("XAIO_OPENAI_ALLOW_FALLBACK", "").strip():
        return
    debug = _openai_env_debug()
    raise RuntimeError(
        "OpenAI responses API not available. "
        "This usually means the service is running from a different Python environment "
        "than the one with requirements installed. "
        f"Set XAIO_OPENAI_ALLOW_FALLBACK=1 to allow a chat.completions fallback. ({debug})"
    )


def structured_parse(
    client: Any,
    *,
    model: str,
    system_prompt: str,
    user_content: str,
    schema: Type[BaseModel],
    reasoning_effort: Optional[str] = None,
) -> Tuple[Optional[BaseModel], Dict[str, Any]]:
    if _has_responses(client):
        req: Dict[str, Any] = dict(
            model=model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            text_format=schema,
        )
        if reasoning_effort:
            req["reasoning"] = {"effort": reasoning_effort}
        resp = client.responses.parse(**req)  # type: ignore[attr-defined]
        raw = resp.model_dump() if hasattr(resp, "model_dump") else json.loads(resp.json())
        parsed = getattr(resp, "output_parsed", None)
        return parsed, raw

    if _has_responses_create(client):
        json_schema = {
            "name": schema.__name__,
            "schema": schema.model_json_schema(),
            "strict": True,
        }
        req = dict(
            model=model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            text={"format": {"type": "json_schema", "json_schema": json_schema}},
        )
        if reasoning_effort:
            req["reasoning"] = {"effort": reasoning_effort}
        resp = client.responses.create(**req)  # type: ignore[attr-defined]
        raw = resp.model_dump() if hasattr(resp, "model_dump") else json.loads(resp.json())
        output_text = getattr(resp, "output_text", "") or ""
        try:
            parsed = schema.model_validate_json(output_text)
        except Exception:
            parsed = None
        return parsed, raw

    _ensure_responses_api(client)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    json_schema = {
        "name": schema.__name__,
        "schema": schema.model_json_schema(),
        "strict": True,
    }

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_schema", "json_schema": json_schema},
        )
    except TypeError:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
        )

    raw = resp.model_dump() if hasattr(resp, "model_dump") else json.loads(resp.json())
    content = ""
    if getattr(resp, "choices", None):
        content = resp.choices[0].message.content or ""
    try:
        parsed = schema.model_validate_json(content)
    except Exception:
        parsed = None
    return parsed, raw


def json_schema_response(
    client: Any,
    *,
    model: str,
    system_prompt: str,
    user_content: str,
    json_schema: Dict[str, Any],
    reasoning_effort: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if getattr(client, "responses", None):
        req: Dict[str, Any] = dict(
            model=model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            text={"format": {"type": "json_schema", "json_schema": json_schema}},
        )
        if reasoning_effort:
            req["reasoning"] = {"effort": reasoning_effort}
        resp = client.responses.create(**req)  # type: ignore[attr-defined]
        raw = resp.model_dump() if hasattr(resp, "model_dump") else json.loads(resp.json())
        output_text = getattr(resp, "output_text", "") or ""
    else:
        _ensure_responses_api(client)
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                response_format={"type": "json_schema", "json_schema": json_schema},
            )
        except TypeError:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                response_format={"type": "json_object"},
            )
        raw = resp.model_dump() if hasattr(resp, "model_dump") else json.loads(resp.json())
        output_text = ""
        if getattr(resp, "choices", None):
            output_text = resp.choices[0].message.content or ""

    try:
        data = json.loads(output_text) if output_text else {}
    except json.JSONDecodeError:
        data = {}
    return data, raw
