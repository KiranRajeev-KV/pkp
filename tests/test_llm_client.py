from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from pkp.llm.client import generate_rationale


@pytest.mark.asyncio
async def test_generate_rationale_returns_tuple_for_valid_ollama_json() -> None:
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "response": (
            '{"rationale": "Both documents discuss retrieval techniques.", '
            '"link_type": "related"}'
        )
    }

    with patch(
        "httpx.AsyncClient.post", new=AsyncMock(return_value=response)
    ) as mock_post:
        result = await generate_rationale(
            doc_a_title="Doc A",
            doc_b_title="Doc B",
            passage_a="A" * 450,
            passage_b="Passage B",
            endpoint="http://localhost:11434",
            model="qwen3:4b",
        )

    assert result == ("Both documents discuss retrieval techniques.", "related")
    _, kwargs = mock_post.call_args
    assert kwargs["json"]["stream"] is False
    assert kwargs["json"]["think"] is False
    assert kwargs["json"]["format"] == "json"
    assert kwargs["json"]["options"] == {"temperature": 0}
    assert kwargs["json"]["prompt"].endswith("/no_think\n")
    assert 'Passage from A: "' + ("A" * 400) + '"' in kwargs["json"]["prompt"]


@pytest.mark.asyncio
async def test_generate_rationale_returns_none_for_malformed_json() -> None:
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"response": "not-json"}

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
        result = await generate_rationale(
            doc_a_title="Doc A",
            doc_b_title="Doc B",
            passage_a="Passage A",
            passage_b="Passage B",
            endpoint="http://localhost:11434",
            model="qwen3:4b",
        )

    assert result is None


@pytest.mark.asyncio
async def test_generate_rationale_returns_none_for_network_error() -> None:
    request = httpx.Request("POST", "http://localhost:11434/api/generate")

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(side_effect=httpx.RequestError("boom", request=request)),
    ):
        result = await generate_rationale(
            doc_a_title="Doc A",
            doc_b_title="Doc B",
            passage_a="Passage A",
            passage_b="Passage B",
            endpoint="http://localhost:11434",
            model="qwen3:4b",
        )

    assert result is None
