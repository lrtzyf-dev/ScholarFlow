"""版本 diff 测试。"""
from app.rag.diff import diff_texts


def test_diff_detects_change_add_remove():
    old = "模型使用 6 个注意力头。翻译任务达到 27.3 BLEU。旧版本仅在英德翻译上评测。"
    new = "模型使用 8 个注意力头。翻译任务达到 28.4 BLEU。新版本增加了英法翻译的评测结果。"
    result = diff_texts(old, new)
    assert result["stats"]["changed"] >= 1
    assert result["stats"]["added"] >= 1
    assert result["stats"]["removed"] >= 1
    pair = result["changed"][0]
    assert pair["similarity"] >= 0.82
