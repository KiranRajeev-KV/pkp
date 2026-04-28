"""Provider-aware client for on-demand proposal explanations."""

from __future__ import annotations

import json
import logging
import os
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

LINK_TYPES = {"related", "extends", "contradicts", "prerequisite"}
PASSAGE_LIMIT = 400
PROMPT_TEMPLATE = """You are analyzing connections between research documents.

Document A: {document_a_title}
Passage from A: "{passage_a}"

Document B: {document_b_title}
Passage from B: "{passage_b}"

Respond with a JSON object only. No explanation, no markdown, no wrapper text.

{{
  "rationale": "one sentence describing what connects these documents from a researcher's perspective",
  "link_type": "one of: related, extends, contradicts, prerequisite"
}}

Choose link_type as:
- "related": both cover the same topic area
- "extends": one document builds on or applies concepts from the other
- "contradicts": the documents present opposing claims or findings
- "prerequisite": one document is foundational knowledge required to understand the other

/no_think
"""
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "rationale": {"type": "string"},
        "link_type": {"type": "string", "enum": sorted(LINK_TYPES)},
    },
    "required": ["rationale", "link_type"],
    "additionalProperties": False,
}


def _truncate_passage(passage: str | None) -> str:
    """Cap passage length to keep explanation prompts responsive."""
    if passage is None:
        return ""
    return passage[:PASSAGE_LIMIT]


def _build_prompt(
    doc_a_title: str,
    doc_b_title: str,
    passage_a: str | None,
    passage_b: str | None,
) -> str:
    """Build the shared rationale prompt."""
    return PROMPT_TEMPLATE.format(
        document_a_title=doc_a_title,
        document_b_title=doc_b_title,
        passage_a=_truncate_passage(passage_a),
        passage_b=_truncate_passage(passage_b),
    )


def _provider_from_endpoint(endpoint: str) -> str:
    """Infer the configured provider from the endpoint shape."""
    if endpoint in {"openai", "anthropic", "ollama"}:
        return endpoint

    host = urlparse(endpoint).netloc.casefold()
    if host.endswith("openai.com"):
        return "openai"
    if host.endswith("anthropic.com"):
        return "anthropic"
    return "ollama"


def _extract_openai_output_text(payload: dict[str, object]) -> str | None:
    """Extract the generated text from an OpenAI Responses payload."""
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    output = payload.get("output")
    if not isinstance(output, list):
        return None

    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                return text

    return None


def _extract_anthropic_output_text(payload: dict[str, object]) -> str | None:
    """Extract the generated text from an Anthropic Messages payload."""
    content = payload.get("content")
    if not isinstance(content, list):
        return None

    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            return text

    return None


def _parse_rationale_response(raw_response: str) -> tuple[str, str] | None:
    """Validate the model response payload."""
    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError:
        return None

    rationale = payload.get("rationale")
    link_type = payload.get("link_type")
    if not isinstance(rationale, str) or not rationale.strip():
        return None
    if not isinstance(link_type, str) or link_type not in LINK_TYPES:
        return None
    return rationale.strip(), link_type


async def _generate_with_ollama(
    client: httpx.AsyncClient,
    endpoint: str,
    model: str,
    prompt: str,
) -> str | None:
    """Generate rationale text through Ollama's native API."""
    response = await client.post(
        f"{endpoint.rstrip('/')}/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "format": "json",
            "options": {"temperature": 0},
        },
    )
    response.raise_for_status()
    payload = response.json()
    raw_response = payload.get("response")
    if isinstance(raw_response, str):
        return raw_response
    return None


async def _generate_with_openai(
    client: httpx.AsyncClient,
    endpoint: str,
    model: str,
    prompt: str,
) -> str | None:
    """Generate rationale text through the OpenAI Responses API."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.warning(
            "rationale generation unavailable provider=openai missing OPENAI_API_KEY"
        )
        return None

    response = await client.post(
        f"{endpoint.rstrip('/')}/v1/responses",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "input": prompt,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "proposal_explanation",
                    "schema": OUTPUT_SCHEMA,
                    "strict": True,
                }
            },
        },
    )
    response.raise_for_status()
    return _extract_openai_output_text(response.json())


async def _generate_with_anthropic(
    client: httpx.AsyncClient,
    endpoint: str,
    model: str,
    prompt: str,
) -> str | None:
    """Generate rationale text through the Anthropic Messages API."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning(
            "rationale generation unavailable provider=anthropic missing ANTHROPIC_API_KEY"
        )
        return None

    response = await client.post(
        f"{endpoint.rstrip('/')}/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": model,
            "max_tokens": 200,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    response.raise_for_status()
    return _extract_anthropic_output_text(response.json())


async def generate_rationale(
    doc_a_title: str,
    doc_b_title: str,
    passage_a: str | None,
    passage_b: str | None,
    endpoint: str,
    model: str,
    timeout: float = 30.0,
) -> tuple[str, str] | None:
    """
    Determine if two documents are related.

    Returns (rationale_sentence, link_type) or None on failure.
    link_type is one of: related, extends, contradicts, prerequisite
    """
    prompt = _build_prompt(doc_a_title, doc_b_title, passage_a, passage_b)
    provider = _provider_from_endpoint(endpoint)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if provider == "openai":
                raw_response = await _generate_with_openai(
                    client, endpoint, model, prompt
                )
            elif provider == "anthropic":
                raw_response = await _generate_with_anthropic(
                    client, endpoint, model, prompt
                )
            else:
                raw_response = await _generate_with_ollama(
                    client, endpoint, model, prompt
                )
    except Exception as exc:
        logger.warning(
            "rationale generation request failed provider=%s endpoint=%s model=%s error=%s",
            provider,
            endpoint,
            model,
            exc,
        )
        return None

    if not isinstance(raw_response, str):
        logger.warning(
            "rationale generation missing text provider=%s endpoint=%s model=%s",
            provider,
            endpoint,
            model,
        )
        return None

    parsed = _parse_rationale_response(raw_response)
    if parsed is None:
        logger.warning(
            "rationale generation returned unusable payload provider=%s endpoint=%s model=%s response=%r",
            provider,
            endpoint,
            model,
            raw_response[:500],
        )
        return None

    return parsed
