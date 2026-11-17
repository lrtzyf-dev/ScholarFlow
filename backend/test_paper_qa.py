# -*- coding: utf-8 -*-
"""每篇真实论文的单篇内专属问答质量测试（限定 paper_id，英文问英文论文）。"""
import json
import urllib.request

BASE = "http://127.0.0.1:8765"

# (论文标题关键词, 专属问题)
CASES = [
    ("Deep Residual Learning",
     "What is the residual learning formulation and how does identity mapping help train very deep networks?"),
    ("BERT: Pre-training",
     "What are the two pre-training tasks in BERT and how does the masked language model work?"),
    ("Few-Shot Learners",
     "How many parameters does the largest GPT-3 model have and what is in-context learning?"),
    ("LLaMA: Open",
     "What architecture does LLaMA use and what are the main training improvements?"),
    ("Learning Transferable Visual",
     "How does the contrastive training in CLIP align images and text, and how does zero-shot classification work?"),
    ("Direct Preference Optimization",
     "What is the DPO objective and why does it not need reinforcement learning or a reward model?"),
    ("Providedproperattribution",
     "What is scaled dot-product attention and how many attention heads are used?"),
]


def get_papers():
    with urllib.request.urlopen(BASE + "/api/papers", timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def ask(question, paper_id):
    payload = json.dumps({"question": question, "paper_id": paper_id}).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/api/chat/stream",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        event = None
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event == "answer":
                return json.loads(line[5:].strip())
    return None


def main():
    papers = get_papers()
    for title_key, question in CASES:
        target = next((p for p in papers if title_key in (p.get("title") or "")
                       and p.get("status") == "parsed"), None)
        if not target:
            print(f"未找到论文: {title_key}\n")
            continue
        data = ask(question, target["id"])
        evs = data.get("evidence", [])
        gate = data.get("gate", {})
        # 限定单篇后，所有证据应来自该论文
        all_from_target = all(e.get("paper_id") == target["id"] for e in evs)
        print(f"论文: {target['title'][:50]}")
        print(f"  问题: {question[:70]}")
        print(f"  证据数: {len(evs)}  全部来自本论文: {all_from_target}")
        print(f"  分数范围: {min((e['score'] for e in evs), default=0):.4f} ~ {max((e['score'] for e in evs), default=0):.4f}")
        print(f"  闸门: passed={gate.get('passed')} grounded={gate.get('grounded_ratio')}")
        print()


if __name__ == "__main__":
    main()
