"""学术翻译逻辑测试。"""
from app.translate.translator import translate_text

TEXT = "本文提出一种基于深度学习的方法，在数据集上取得了较高准确率。"


def test_offline_degradation_is_honest():
    out = translate_text(TEXT)
    # 离线不编造译文
    assert out["mode"] == "offline"
    assert out["translation"] is None
    assert "message" in out
    # 但术语表真实命中
    sources = [h["source"] for h in out["glossary_hits"]]
    assert "深度学习" in sources
    assert "数据集" in sources
    assert "准确率" in sources


class _FakeResponse:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self):
        self.prompt = ""

    def chat(self, messages, temperature=0.2):
        self.prompt = messages[0]["content"]
        return _FakeResponse("This paper proposes a deep learning based method.")


def test_llm_translation_includes_glossary():
    fake = _FakeLLM()
    out = translate_text(TEXT, llm=fake)
    assert out["mode"] == "llm"
    assert "deep learning based method" in out["translation"]
    # 提示中注入了术语对照表
    assert "deep learning" in fake.prompt
