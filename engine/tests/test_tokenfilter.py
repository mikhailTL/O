import json

import pytest

from o_engine.tokenfilter import TokenFilter, filter_stream


def _chunks(text: str, size: int = 3):
    return [text[i : i + size] for i in range(0, len(text), size)]


def test_plain_array_passes_unchanged():
    text = '[{"a": 1}]'
    assert filter_stream(_chunks(text)) == text


def test_prose_before_and_after_is_dropped():
    text = 'Sure, here you go:\n[{"a": 1}]\nLet me know if that helps.'
    assert filter_stream(_chunks(text)) == '[{"a": 1}]'


def test_markdown_fence_is_dropped():
    text = 'Here:\n```json\n[{"a": 1}, {"b": 2}]\n```\nDone.'
    assert filter_stream(_chunks(text)) == '[{"a": 1}, {"b": 2}]'


def test_brackets_inside_strings_do_not_close():
    inner = '[{"command_string": "echo \\"]}\\" && ls [a-z]*", "x": "{"}]'
    text = "prefix " + inner + " suffix ]"
    out = filter_stream(_chunks(text, 2))
    assert out == inner
    assert json.loads(out)[0]["command_string"] == 'echo "]}" && ls [a-z]*'


def test_escaped_quote_inside_string_keeps_string_state():
    inner = '["a\\"b]", "c"]'
    assert filter_stream(_chunks(inner, 1)) == inner


def test_nested_arrays_and_objects_balanced():
    inner = '[{"a": [1, [2, {"b": [3]}]]}, []]'
    assert filter_stream(_chunks(inner + "]]}} trailing", 1)) == inner


def test_object_at_top_level():
    inner = '{"label": "x", "steps": [{"a": "}"}]}'
    assert filter_stream(_chunks("note: " + inner + " end", 4)) == inner


def test_done_flag_and_feed_after_close_returns_empty():
    tf = TokenFilter()
    assert tf.done is False
    assert tf.feed("junk [") == "["
    assert tf.done is False
    assert tf.feed("1]") == "1]"
    assert tf.done is True
    assert tf.feed(" more [2]") == ""
    assert tf.done is True


def test_only_prose_yields_nothing():
    tf = TokenFilter()
    assert tf.feed("no json here at all") == ""
    assert tf.done is False
    assert filter_stream(["nothing", " to", " see"]) == ""


def test_single_token_whole_document():
    text = 'Answer:\n[{"target_directory": ".", "command_string": "ls", "timeout_seconds": 5}]\n'
    assert filter_stream([text]) == text.strip().split("\n", 1)[1].strip()


def test_token_boundary_inside_escape():
    inner = '["x\\"]"]'
    assert filter_stream(['["x\\', '"]"', "]"]) == inner
    assert filter_stream(['["x', '\\"', ']"]']) == inner


def test_filter_stream_accepts_token_filter_instance():
    tf = TokenFilter()
    assert filter_stream(iter(["a", "[", "1", "]", "b"]), tf) == "[1]"
    assert tf.done


def test_unterminated_input_returns_partial_and_not_done():
    tf = TokenFilter()
    assert tf.feed('[{"a": 1') == '[{"a": 1'
    assert tf.done is False
    assert tf.text == '[{"a": 1'


@pytest.mark.parametrize("opener", ["[", "{"])
def test_text_property_accumulates(opener):
    closer = "]" if opener == "[" else "}"
    tf = TokenFilter()
    tf.feed("x" + opener)
    tf.feed(closer + "y")
    assert tf.text == opener + closer
