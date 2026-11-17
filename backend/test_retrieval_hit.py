# -*- coding: utf-8 -*-
"""真实国际文献库下的混合检索命中正确性测试。"""
import json
import urllib.request

BASE = "http://127.0.0.1:8765"
QUESTIONS = [
    ("什么是残差连接？它解决了深度神经网络的什么问题？", "Deep Residual Learning"),
    ("GPT-3有多少参数？few-shot学习的核心思想是什么？", "Few-Shot Learners"),
    ("DPO直接偏好优化相比RLHF有什么优势？", "Direct Preference Optimization"),
    ("BERT的掩码语言模型MLM是如何预训练的？", "BERT"),
    ("CLIP如何用对比学习对齐图像和文本？", "Learning Transferable Visual"),
]


def ask(question):
    payload = json.dumps({"question": question}).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/api/chat/stream",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    evidences = None
    gate = None
    refusal = None
    with urllib.request.urlopen(req, timeout=120) as resp:
        event = None
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event == "answer":
                data = json.loads(line[5:].strip())
                evidences = data.get("evidence", [])
                gate = data.get("gate", {})
                refusal = data.get("refusal", 0)
                break
    return evidences, gate, refusal


def main():
    for q, expect in QUESTIONS:
        evs, gate, refusal = ask(q)
        src = {}
        for e in evs:
            src[e.get("paper_title", "?")] = src.get(e.get("paper_title", "?"), 0) + 1
        # 期望论文是否在证据来源中
        hit = any(expect.lower() in t.lower() for t in src)
        print(f"问: {q}")
        print(f"  期望命中: {expect}  实际命中: {'是' if hit else '否'}")
        for rank, e in enumerate(evs, 1):
            print(f"    #{rank} [{e.get('score', 0):.4f}] {e.get('paper_title', '?')[:40]}")
        print(f"  拒答: {refusal}  闸门passed: {gate.get('passed') if gate else None}")
        print()


if __name__ == "__main__":
    main()
