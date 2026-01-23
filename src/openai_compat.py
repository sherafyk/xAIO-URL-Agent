from __future__ import annotations

import json
import os
import sys
import warnings
from typing import Any, Dict, Optional, Tuple, Type

from pydantic import BaseModel


def _enforce_strict_json_schema(node: Any) -> Any:
    """Make a JSON schema compatible with OpenAI strict Structured Outputs.

    OpenAI strict mode requires (among other constraints):
      - `additionalProperties: false` for every object schema
      - `required` must include *every* key in `properties`
        (optional fields should be represented by allowing `null`)

    We apply these rules recursively across properties/items/anyOf/allOf/oneOf
    and within $defs/definitions.
    """

    if isinstance(node, dict):
        # Recurse into common schema containers first
        for k in ("properties", "$defs", "definitions"):
            if k in node and isinstance(node[k], dict):
                for _, v in node[k].items():
                    _enforce_strict_json_schema(v)

        for k in ("items", "additionalProperties", "propertyNames"):
            if k in node:
                _enforce_strict_json_schema(node[k])

        for k in ("anyOf", "allOf", "oneOf"):
            if k in node and isinstance(node[k], list):
                for v in node[k]:
                    _enforce_strict_json_schema(v)

        # If this node is (or can be) an object schema, enforce closed schema rules.
        t = node.get("type")
        is_object = t == "object" or (isinstance(t, list) and "object" in t) or "properties" in node
        if is_object:
            # OpenAI requires this key to be present and false.
            node["additionalProperties"] = False

            props = node.get("properties")
            if isinstance(props, dict):
                # OpenAI strict requires every property name to appear in required.
                node["required"] = list(props.keys())

        return node

    if isinstance(node, list):
        for v in node:
            _enforce_strict_json_schema(v)
        return node

    return node


def _strict_pydantic_json_schema(model: Type[BaseModel]) -> Dict[str, Any]:
    schema = model.model_json_schema()
    _enforce_strict_json_schema(schema)
    return schema


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


def _warn_missing_responses_api() -> None:
    if os.getenv("XAIO_OPENAI_ALLOW_FALLBACK", "").strip():
        return
    debug = _openai_env_debug()
    warnings.warn(
        "OpenAI responses API not available. Falling back to chat.completions. "
        "Set XAIO_OPENAI_ALLOW_FALLBACK=1 to suppress this warning. "
        f"({debug})"
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
            "schema": _strict_pydantic_json_schema(schema),
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

    _warn_missing_responses_api()

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    json_schema = {
        "name": schema.__name__,
        "schema": _strict_pydantic_json_schema(schema),
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
        _warn_missing_responses_api()
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
