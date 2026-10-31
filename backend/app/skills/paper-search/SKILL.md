---
name: paper-search
title: 论文检索
description: 当用户需要查找、检索、调研某主题的论文，或需要找某篇论文的原文/作者/发表信息时使用。指导如何组合 arXiv、Semantic Scholar、Crossref 与本地文献库进行系统检索。
---

# 论文检索技能

## 何时使用
用户要"找论文 / 查某方向有哪些研究 / 找原文 / 查作者或发表信息 / 做选题调研"。

## 检索顺序（由广到准，按需调用）
1. **arXiv**（`mcp__builtin__arxiv_search`）：查预印本、最新方法、计算机/AI/物理/数学。英文术语检索效果最好。
2. **Semantic Scholar**（`mcp__builtin__semantic_scholar_search`）：查带摘要、引用数、开放 PDF 的论文，适合评估影响力。
3. **Crossref**（`mcp__builtin__crossref_search`）：查正式出版信息（期刊、卷期、DOI、出版社），用于核对引用与正式版本。
4. **本地文献库**（`list_papers` / `retrieve_evidence`）：用户已上传的论文，可直接检索其内容。

## 检索技巧
- 用 2–4 个核心术语，先宽后窄；同义词分别检索（如 "LLM" / "large language model"）。
- 找到关键论文后，用其标题/作者/方法名做二次检索，顺藤摸瓜。
- 注意区分：预印本（arXiv，无同行评审）与正式出版（Crossref 有 DOI/期刊）。
- 若接口返回限流（429）或超时，换一个数据源，或告知用户稍后重试，不要反复请求同一接口。

## 输出要求
- 给出论文清单时包含：标题、作者、年份、来源、一句话说明其相关性，附链接/PDF。
- 若用于选题调研，按主题或方法分组，并指出哪些是奠基性工作、哪些是最新进展。
