"""学术翻译：真实模型翻译并强制术语一致；离线显式降级，不编造译文。"""
from __future__ import annotations

from ..llm import MockLLM, get_llm
from .glossary import DEFAULT_GLOSSARY, find_terms
from .prompting import system_prompt


def _build_prompt(text: str, source: str, target: str) -> str:
    hits = find_terms(text, DEFAULT_GLOSSARY)
    return system_prompt(source, target, hits)


def translate_text(text: str, source: str = "Chinese", target: str = "English",
                   llm=None) -> dict:
    """返回 {translation, mode, glossary_hits[, message]}。"""
    llm = llm or get_llm()
    hits = find_terms(text, DEFAULT_GLOSSARY)

    if isinstance(llm, MockLLM):
        return {
            "translation": None,
            "mode": "offline",
            "glossary_hits": hits,
            "message": (
                "离线模式未配置大模型，无法生成真实英文译文；以下为术语表命中结果，"
                "配置模型后可获得完整且术语一致的翻译。"
            ),
        }

    response = llm.chat(
        [{"role": "system", "content": _build_prompt(text, source, target)},
         {"role": "user", "content": text}],
        temperature=0.2,
    )
    return {
        "translation": response.content.strip(),
        "mode": "llm",
        "glossary_hits": hits,
    }
