from __future__ import annotations

from pkp.llm.client import (
    _batch_note_texts_for_combine,
    _clean_notes_response,
    _split_note_sections,
)


def test_clean_notes_response_strips_thinking_and_preamble() -> None:
    raw = (
        "planning text\n"
        "</think>\n\n"
        "extra preface\n"
        "## Key Concepts\n"
        "Generated notes contain enough specific words to pass validation for a "
        "real-looking note body.\n\n"
        "## Details Worth Remembering\n"
        "The cleanup starts at the first notes heading and keeps later sections."
    )

    cleaned = _clean_notes_response(raw)

    assert cleaned is not None
    assert cleaned.startswith("## Key Concepts")
    assert "planning text" not in cleaned
    assert "extra preface" not in cleaned
    assert "## Details Worth Remembering" in cleaned


def test_clean_notes_response_trims_trailing_qwen_planning_artifacts() -> None:
    raw = (
        "## Key Concepts\n"
        + "Specific note content " * 30
        + "\n\n## Questions and Follow-ups\n"
        + "Specific follow-up content " * 20
        + "\n\nNow, let me count the words.\n"
        "## Key Concepts\n"
        "This repeated draft should be discarded."
    )

    cleaned = _clean_notes_response(raw)

    assert cleaned is not None
    assert "Now, let me count the words." not in cleaned
    assert "This repeated draft should be discarded." not in cleaned
    assert cleaned.count("## Key Concepts") == 1


def test_split_note_sections_uses_overlap() -> None:
    text = "Sentence one. Sentence two. Sentence three. " * 500

    sections = _split_note_sections(text, section_chars=1_000, overlap_chars=100)

    assert len(sections) > 1
    assert sections[0][-80:] in sections[1]


def test_batch_note_texts_for_combine_bounds_prompt_input() -> None:
    note_texts = [
        "section one " * 20,
        "section two " * 20,
        "section three " * 20,
        "section four " * 20,
    ]

    batches = _batch_note_texts_for_combine(note_texts, body_char_limit=300)

    assert len(batches) > 1
    for batch in batches:
        assert len("\n\n".join(batch)) <= 300
    assert [item for batch in batches for item in batch] == [
        note.strip() for note in note_texts
    ]
