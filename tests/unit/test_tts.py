"""Unit tests for lambdas/shared/tts.py — TTS utilities."""

from __future__ import annotations

from lambdas.shared.tts import split_text, format_ssml, select_hold_message


# ---------------------------------------------------------------------------
# split_text
# ---------------------------------------------------------------------------

class TestSplitText:
    def test_short_text_returned_as_single_segment(self):
        assert split_text("Hello world.", 3000) == ["Hello world."]

    def test_empty_text(self):
        assert split_text("", 3000) == [""]

    def test_exact_boundary(self):
        text = "a" * 3000
        assert split_text(text, 3000) == [text]

    def test_split_at_sentence_boundary(self):
        # Two sentences, second pushes past the limit.
        s1 = "A" * 10 + "."
        s2 = " " + "B" * 10 + "."
        text = s1 + s2
        segments = split_text(text, 15)
        assert "".join(segments) == text
        assert all(len(s) <= 15 for s in segments)

    def test_forced_split_no_sentence_boundary(self):
        text = "a" * 50
        segments = split_text(text, 20)
        assert "".join(segments) == text
        assert all(len(s) <= 20 for s in segments)

    def test_multiple_splits(self):
        sentences = ["Hello. ", "World. ", "Foo. ", "Bar."]
        text = "".join(sentences)
        segments = split_text(text, 10)
        assert "".join(segments) == text
        assert all(len(s) <= 10 for s in segments)

    def test_concat_reproduces_original(self):
        text = "First sentence. Second sentence! Third? Done."
        segments = split_text(text, 20)
        assert "".join(segments) == text


# ---------------------------------------------------------------------------
# format_ssml
# ---------------------------------------------------------------------------

class TestFormatSsml:
    def test_single_segment(self):
        assert format_ssml(["Hello"]) == "<speak>Hello</speak>"

    def test_multiple_segments(self):
        result = format_ssml(["A", "B", "C"])
        expected = '<speak>A<break time="1000ms"/>B<break time="1000ms"/>C</speak>'
        assert result == expected

    def test_xml_escaping(self):
        result = format_ssml(["Tom & Jerry said \"hi\" <now> it's done"])
        assert "&amp;" in result
        assert "&lt;" in result
        assert "&gt;" in result
        assert "&quot;" in result
        assert "&apos;" in result
        assert "<speak>" in result

    def test_custom_pause(self):
        result = format_ssml(["A", "B"], pause_ms=500)
        assert '<break time="500ms"/>' in result


# ---------------------------------------------------------------------------
# select_hold_message
# ---------------------------------------------------------------------------

class TestSelectHoldMessage:
    def test_empty_pool_returns_default(self):
        msg = select_hold_message([])
        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_single_message_pool(self):
        assert select_hold_message(["only one"], last_message="only one") == "only one"

    def test_never_same_consecutive(self):
        pool = ["A", "B", "C"]
        last = None
        for _ in range(50):
            msg = select_hold_message(pool, last_message=last)
            assert msg != last or len(pool) == 1
            last = msg

    def test_selection_from_pool(self):
        pool = ["X", "Y", "Z"]
        msg = select_hold_message(pool, last_message="X")
        assert msg in ("Y", "Z")
