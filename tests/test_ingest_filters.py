from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from pkp.pipeline.extractor import DoclingExtractor, extract_arxiv_id
from pkp.pipeline.ingest import fetch_arxiv_metadata, is_citation_chunk

CITATION_HEAVY_SAMPLE = """\
.harvard.edu/abs/2019NatPh..15.1242F).[doi](https://en.wikipedia.org/wiki/Doi_(identifier)):[10.1038/s41567-019-0663-9](https://doi.org/10.1038%2Fs41567-019-0663-9).[S2CID](https://en.wikipedia.org/wiki/S2CID_(identifier))[203638258](https://api.semanticscholar.org/CorpusID:203638258).Bojowald, Martin (2015). "Quantum cosmology: a review".[^](https://en.wikipedia.org#cite_ref-5)*Reports on Progress in Physics*.**78**(2) 023901.[arXiv](https://en.wikipedia.org/wiki/ArXiv_(identifier)):[1501.04899](https://arxiv.org/abs/1501.04899).[Bibcode](https://en.wikipedia.org/wiki/Bibcode_(identifier)):[2015RPPh...78b3901B](https://ui.adsabs.harvard.edu/abs/2015RPPh...78b3901B).[doi](https://en.wikipedia.org/wiki/Doi_(identifier)):[10.1088/0034-4885/78/2/023901](https://doi.org/10.1088%2F0034-4885%2F78%2F2%2F023901).[PMID](https://en.wikipedia.org/wiki/PMID_(identifier))[25582917](https://pubmed.ncbi.nlm.nih.gov/25582917).[S2CID](https://en.wikipedia.org/wiki/S2CID_(identifier))[18463042](https://api.semanticscholar.org/CorpusID:18463042).
"""

SEMANTIC_CONTENT_SAMPLE = """\
Quantum systems have bound states that are quantized to discrete values of energy, momentum, angular momentum, and other quantities, in contrast to classical systems where these quantities can be measured continuously. Measurements of quantum systems show characteristics of both particles and waves (wave-particle duality), and there are limits to how accurately the value of a physical quantity can be predicted prior to its measurement, given a complete set of initial conditions (the uncertainty principle). Quantum mechanics arose gradually from theories to explain observations that could not be reconciled with classical physics.
"""


def test_is_citation_chunk_returns_true_for_verification_citation_sample() -> None:
    assert is_citation_chunk(CITATION_HEAVY_SAMPLE) is True


def test_is_citation_chunk_returns_false_for_verification_semantic_sample() -> None:
    assert is_citation_chunk(SEMANTIC_CONTENT_SAMPLE) is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2312.10997", "2312.10997"),
        ("1706.03762v7", "1706.03762"),
        ("rag-survey-2312.10997", "2312.10997"),
        ("2005.11401", "2005.11401"),
        ("attention is all you need", None),
        ("some-normal-title", None),
    ],
)
def test_extract_arxiv_id_detects_supported_shapes(
    text: str,
    expected: str | None,
) -> None:
    assert extract_arxiv_id(text) == expected


@pytest.mark.asyncio
async def test_fetch_arxiv_metadata_parses_valid_atom_entry() -> None:
    response = Mock()
    response.raise_for_status.return_value = None
    response.text = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>  Retrieval-Augmented   Generation for Large Language Models  </title>
    <summary>  Survey of retrieval-augmented generation methods.  </summary>
    <author><name>Jane Doe</name></author>
    <author><name>John Roe</name></author>
  </entry>
</feed>
"""

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=response)):
        metadata = await fetch_arxiv_metadata("2312.10997")

    assert metadata == {
        "title": "Retrieval-Augmented Generation for Large Language Models",
        "authors": ["Jane Doe", "John Roe"],
        "abstract": "Survey of retrieval-augmented generation methods.",
        "arxiv_id": "2312.10997",
    }


@pytest.mark.asyncio
async def test_fetch_arxiv_metadata_returns_none_without_entry() -> None:
    response = Mock()
    response.raise_for_status.return_value = None
    response.text = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"></feed>
"""

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=response)):
        metadata = await fetch_arxiv_metadata("2312.10997")

    assert metadata is None


@pytest.mark.asyncio
async def test_fetch_arxiv_metadata_returns_none_on_request_error() -> None:
    request = httpx.Request(
        "GET", "http://export.arxiv.org/api/query?id_list=2312.10997"
    )

    with patch(
        "httpx.AsyncClient.get",
        new=AsyncMock(side_effect=httpx.RequestError("boom", request=request)),
    ):
        metadata = await fetch_arxiv_metadata("2312.10997")

    assert metadata is None


def test_docling_extract_title_from_markdown_scans_h2_headings() -> None:
    extractor = DoclingExtractor.__new__(DoclingExtractor)

    title = extractor._extract_title_from_markdown(
        "\n".join(
            [
                "Introductory line",
                "Another line",
                "## Searching for Best Practices in Retrieval-Augmented Generation",
                "Body content follows",
            ]
        )
    )

    assert title == "Searching for Best Practices in Retrieval-Augmented Generation"
