"""学术术语表（中→英）与命中检测。

内置一份通用学术/AI 术语对照，保证同一术语在全文中译法一致。
长术语优先匹配，避免短术语先命中而破坏长术语。
"""
from __future__ import annotations

DEFAULT_GLOSSARY: dict[str, str] = {
    # 通用学术
    "摘要": "abstract",
    "实验": "experiment",
    "实验结果": "experimental results",
    "数据集": "dataset",
    "基准": "benchmark",
    "基线": "baseline",
    "指标": "metric",
    "准确率": "accuracy",
    "精确率": "precision",
    "召回率": "recall",
    "假设": "hypothesis",
    "结论": "conclusion",
    "相关工作": "related work",
    "参考文献": "references",
    "消融实验": "ablation study",
    # AI / 机器学习
    "人工智能": "artificial intelligence",
    "机器学习": "machine learning",
    "深度学习": "deep learning",
    "神经网络": "neural network",
    "卷积神经网络": "convolutional neural network",
    "循环神经网络": "recurrent neural network",
    "生成对抗网络": "generative adversarial network",
    "注意力机制": "attention mechanism",
    "自注意力": "self-attention",
    "大语言模型": "large language model",
    "强化学习": "reinforcement learning",
    "监督学习": "supervised learning",
    "无监督学习": "unsupervised learning",
    "迁移学习": "transfer learning",
    "微调": "fine-tuning",
    "预训练": "pre-training",
    "词向量": "word embedding",
    "特征提取": "feature extraction",
    "自然语言处理": "natural language processing",
    "计算机视觉": "computer vision",
    "过拟合": "overfitting",
    "正则化": "regularization",
    "梯度下降": "gradient descent",
    "反向传播": "backpropagation",
}


def find_terms(text: str, glossary: dict[str, str] | None = None) -> list[dict]:
    """返回文本中命中的术语，长术语优先、按命中顺序去重。"""
    glossary = glossary or DEFAULT_GLOSSARY
    hits: list[dict] = []
    for term in sorted(glossary, key=len, reverse=True):
        if term in text:
            hits.append({"source": term, "target": glossary[term]})
    return hits
