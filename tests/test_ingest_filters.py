from __future__ import annotations

from pkp.pipeline.ingest import is_citation_chunk

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
