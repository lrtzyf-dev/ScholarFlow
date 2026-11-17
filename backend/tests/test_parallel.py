"""双语对照翻译测试。"""
from app.translate.parallel import segment_units, translate_parallel

SAMPLE = """本文提出一种基于深度学习的方法。

模型的损失函数如下：
$$L = -\\sum_{i=1}^{n} y_i \\log \\hat{y}_i$$

```python
loss = compute_loss(y, y_hat)
```

实验在公开数据集上验证了准确率。"""


def test_segment_counts_and_kinds():
    units = segment_units(SAMPLE)
    preserve = [u for u in units if u["preserve"]]
    normal = [u for u in units if not u["preserve"]]
    # 公式 + 代码块两个保留单元
    assert len(preserve) == 2
    assert any("$$" in u["source"] for u in preserve)
    assert any("```python" in u["source"] for u in preserve)
    # 三个普通段落（含公式前引导句）
    assert len(normal) == 3
    # index 连续
    assert [u["index"] for u in units] == list(range(len(units)))


def test_offline_parallel_keeps_preserve():
    out = translate_parallel(SAMPLE)
    assert out["mode"] == "offline"
    for unit in out["units"]:
        if unit["preserve"]:
            # 代码/公式保留原文
            assert unit["target"] == unit["source"]
        else:
            # 普通段落不编造
            assert unit["target"] is None


class _FakeResponse:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def chat(self, messages, temperature=0.2):
        return _FakeResponse("Translated paragraph.")


def test_llm_parallel_translates_normal_units():
    out = translate_parallel(SAMPLE, llm=_FakeLLM())
    assert out["mode"] == "llm"
    for unit in out["units"]:
        if unit["preserve"]:
            assert unit["target"] == unit["source"]
        else:
            assert unit["target"] == "Translated paragraph."
