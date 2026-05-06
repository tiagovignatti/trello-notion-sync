from src.markdown import body_to_blocks


def test_empty_body_yields_no_blocks():
    assert body_to_blocks("") == []
    assert body_to_blocks("   \n  \n") == []


def test_single_paragraph():
    blocks = body_to_blocks("hello world")
    assert len(blocks) == 1
    assert blocks[0]["type"] == "paragraph"
    rich = blocks[0]["paragraph"]["rich_text"]
    assert rich[0]["text"]["content"] == "hello world"


def test_paragraphs_split_on_blank_lines():
    blocks = body_to_blocks("first\n\nsecond\n\nthird")
    assert [b["paragraph"]["rich_text"][0]["text"]["content"] for b in blocks] == [
        "first", "second", "third",
    ]


def test_long_paragraph_is_chunked_into_2k_pieces():
    body = "x" * 4500
    blocks = body_to_blocks(body)
    assert len(blocks) == 1
    rich = blocks[0]["paragraph"]["rich_text"]
    assert [len(r["text"]["content"]) for r in rich] == [2000, 2000, 500]
