"""Independent source segmentation goldens: original code/math are not translated."""
import pytest
from app.translate.parallel import segment_units, translate_parallel
from app.llm import LLMResponse


@pytest.mark.parametrize("block", [
    '````python\nprint("start")\n```\nprint("still code")\n````',
    '~~~python\n```\nprint("still code")\n~~~',
    '```python\nprint("start")\n```not a closer\nprint("still code")\n````',
    '  ````python\n  ```\n  print("preserve indentation")\n  `````',
    '````python\r\n```\r\nprint("CRLF code")\r\n````',
    '$$\n\\begin{aligned}\na &= b + c \\\\\nx &= y\n\\end{aligned}\n$$',
    '$$ E =\nmc^2 $$',
    '\\[\n\\frac{1}{n}\\sum_i x_i\n\\]',
    '\\begin{equation*}\n\\alpha + \\beta = \\gamma\n\\end{equation*}',
])
def test_multiline_blocks_preserve_exact_source_and_only_prose_is_sent(block):
    source = "First paragraph.\n\n" + block + "\n\nFinal paragraph."
    units = segment_units(source)
    assert [u["preserve"] for u in units] == [False, True, False]
    assert units[1]["source"] == block
    sent = []
    class Model:
        def chat(self, messages, **kwargs):
            sent.append(messages[-1]["content"])
            return LLMResponse("Translated " + messages[-1]["content"])
    result = translate_parallel(source, llm=Model())
    assert sent == ["First paragraph.", "Final paragraph."]
    assert result["units"][1]["target"] == block


@pytest.mark.parametrize("source", [
    '````python\n```\nprint("unclosed but protected")',
    '$$\n\\text{do not translate inside math}',
    '\\[\nx+y',
    '\\begin{align}\na &= b',
])
def test_unclosed_preserved_block_does_not_translate_its_interior(source):
    units = segment_units(source)
    assert len(units) == 1 and units[0]["preserve"] and units[0]["source"] == source


def test_short_inline_formula_and_complete_math_then_prose_are_separate():
    units = segment_units('$$x = y$$\n\nText.\n\n\\[x = y\\]\n\nMore.')
    assert [u["preserve"] for u in units] == [True, False, True, False]
