"""
LLM 统一抽象。
- 配置了 LLM_API_KEY：httpx 直连 OpenAI 兼容接口（百炼/硅基/DeepSeek/vLLM 都支持）
- 未配置：走 MockLLM，离线也能跑通"检索→取证→带引用作答"完整 Agent 流程
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .config import settings
from .rag.citation import split_sentences


class LLMResponse:
    def __init__(self, content: str, tool_calls: list[dict] | None = None,
                 reasoning_content: str = "", usage: dict | None = None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.reasoning_content = reasoning_content  # 推理模型（Qwen3/QwQ/DeepSeek-R1）的思考链
        # token 用量：{prompt_tokens, completion_tokens, total_tokens}
        self.usage = usage or None

    def to_dict(self) -> dict:
        return {"content": self.content, "tool_calls": self.tool_calls,
                "reasoning_content": self.reasoning_content, "usage": self.usage}


def estimate_tokens(text: str) -> int:
    """离线估算 token 数：中文按字计，英文按约 4 字符 1 token，取两者之和。"""
    if not text:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(re.sub(r"[\u4e00-\u9fff\s]", "", text))
    return cjk + (max(1, round(other / 4)) if other else 0)


def _messages_text(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
    return "\n".join(parts)


def estimate_usage(messages: list[dict], output: LLMResponse) -> dict:
    """离线模式下估算一次调用的用量。"""
    prompt_tokens = estimate_tokens(_messages_text(messages))
    completion = estimate_tokens(
        (output.content or "") + (output.reasoning_content or "")
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion,
        "total_tokens": prompt_tokens + completion,
    }


class BaseLLM:
    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.2, reasoning_effort: str | None = None) -> LLMResponse:
        raise NotImplementedError

    def stream_chat(self, messages: list[dict], tools: list[dict] | None = None,
                    reasoning_effort: str | None = None):
        """生成器：yield ("delta", "thinking"|"answer", text)…，最后 yield ("final", LLMResponse)。"""
        raise NotImplementedError


def _resolve_effort(reasoning_effort: str | None) -> str:
    """None 时回落到全局设置；返回最终要发送的 effort（空串表示不发送）。"""
    if reasoning_effort is None:
        return (settings.reasoning_effort or "").strip().lower()
    return (reasoning_effort or "").strip().lower()


def _chunk(text: str, size: int = 16):
    """把文本切成小块，模拟逐字流式（Mock 用）。"""
    import re

    pieces = re.split(r"(?<=[。！？，,.!?\s])", text)
    for p in pieces:
        for i in range(0, len(p), size):
            seg = p[i:i + size]
            if seg:
                yield seg


class APIClient(BaseLLM):
    """真实大模型，OpenAI 兼容 Chat Completions，httpx 直连。"""

    def __init__(self, timeout: float = 120.0) -> None:
        self._timeout = timeout

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.2, reasoning_effort: str | None = None) -> LLMResponse:
        base = (settings.llm_base_url or "").rstrip("/")
        url = f"{base}/chat/completions"
        body: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": messages,
        }
        effort = _resolve_effort(reasoning_effort)
        if effort:
            # 推理模型通常不接受自定义 temperature，设置 effort 时交由模型默认
            body["reasoning_effort"] = effort
        else:
            body["temperature"] = temperature
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
        headers = {"Content-Type": "application/json"}
        if settings.llm_api_key:
            headers["Authorization"] = f"Bearer {settings.llm_api_key}"
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        msg = data["choices"][0]["message"]
        tool_calls: list[dict] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append({"id": tc.get("id", ""), "name": fn.get("name", ""), "arguments": args})
        return LLMResponse(
            content=msg.get("content") or "",
            tool_calls=tool_calls,
            reasoning_content=msg.get("reasoning_content") or msg.get("reasoning") or "",
            usage=data.get("usage") or None,
        )

    def stream_chat(self, messages: list[dict], tools: list[dict] | None = None,
                    reasoning_effort: str | None = None):
        """OpenAI 兼容流式：逐块 yield 思考/答案增量，最后 yield 汇总 LLMResponse。"""
        base = (settings.llm_base_url or "").rstrip("/")
        url = f"{base}/chat/completions"
        body: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": messages,
            "stream": True,
            # 让服务端在最后一个 chunk 回传 usage（OpenAI/Qwen 兼容）
            "stream_options": {"include_usage": True},
        }
        effort = _resolve_effort(reasoning_effort)
        if effort:
            body["reasoning_effort"] = effort
        else:
            body["temperature"] = 0.2
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
        headers = {"Content-Type": "application/json"}
        if settings.llm_api_key:
            headers["Authorization"] = f"Bearer {settings.llm_api_key}"

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_acc: dict[int, dict] = {}
        stream_usage: dict | None = None

        with httpx.Client(timeout=self._timeout) as client:
            with client.stream("POST", url, headers=headers, json=body) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    # 部分供应商在带 usage 的 chunk 里 choices 为空
                    if obj.get("usage"):
                        stream_usage = obj["usage"]
                    delta = obj.get("choices", [{}])[0].get("delta", {})
                    rc = delta.get("reasoning_content") or delta.get("reasoning")
                    if rc:
                        reasoning_parts.append(rc)
                        yield ("delta", "thinking", rc)
                    if delta.get("content"):
                        content_parts.append(delta["content"])
                        yield ("delta", "answer", delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]

        tool_calls: list[dict] = []
        for idx in sorted(tool_acc):
            slot = tool_acc[idx]
            try:
                args = json.loads(slot["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append({"id": slot["id"] or f"call_{idx}",
                               "name": slot["name"], "arguments": args})
        final = LLMResponse(
            content="".join(content_parts),
            tool_calls=tool_calls,
            reasoning_content="".join(reasoning_parts),
            usage=stream_usage,
        )
        yield ("final", final)


# 向后兼容旧名称
LiteLLMClient = APIClient


def _safe_citation_title(title: str | None) -> str:
    """清洗嵌入《》的论文标题：移除会切断句子的句末标点与换行，
    避免标题里的“。！？.！?”破坏引用切句，导致半句无引用标记。"""
    t = (title or "该论文").strip()
    t = re.sub(r"[。.！？!?\n\r]", "", t).strip()
    return t or "该论文"


class MockLLM(BaseLLM):
    """
    离线脚本模型：
    1. 看到工具且尚未检索 -> 发起 retrieve_evidence 工具调用
    2. 看到工具返回的证据 -> 用证据原文拼接带 [^eN] 引用的答案
    3. 无证据 -> 拒答（真实走拒答路径，保证前端可演示）
    """

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.2, reasoning_effort: str | None = None) -> LLMResponse:
        last_user = next((m for m in reversed(messages) if m["role"] == "user"), None)
        tool_msgs = [m for m in messages if m["role"] == "tool"]

        if any(m["role"] == "system" and "[SCHOLARFLOW_PLAN_MODE]" in str(m.get("content", "")) for m in messages):
            return LLMResponse(content=(
                "（离线模式）待审阅计划\n\n"
                "1. 确认任务目标、输入材料与预期产出。\n"
                "2. 读取相关文献，核对证据与缺失信息。\n"
                "3. 按审阅后的范围执行，遇到写入或外部操作时遵循审批设置。\n"
                "4. 检查产出、引用和未解决问题。\n\n"
                "上述步骤尚未执行。当前为离线示例，具体产出、修改范围和验证方式需接入模型后结合任务细化。"
            ))

        if tools and not tool_msgs and last_user is not None:
            # 第一步：调用检索
            q = last_user["content"]
            return LLMResponse(
                content="",
                reasoning_content=(
                    f"先理解问题：用户想知道「{q}」。这是一个事实性问题，"
                    "我不能凭记忆作答，必须先在文献库里检索原文证据。"
                    "我将调用检索工具，按相关度取回最相关的段落，再据此组织带引用的答案。"
                ),
                tool_calls=[
                    {
                        "id": "call_mock_1",
                        "name": "retrieve_evidence",
                        "arguments": {"query": q, "k": settings.retrieve_topk},
                    }
                ],
            )

        # 第二步：基于证据作答
        evidences: list[dict] = []
        for tm in tool_msgs:
            try:
                payload = json.loads(tm["content"]) if isinstance(tm["content"], str) else tm["content"]
                evidences.extend(payload.get("evidence", []))
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue

        if not evidences:
            return LLMResponse(
                content="（离线模式）知识库中没有检索到支持该问题的原文证据，按抗幻觉规则拒绝回答。请先上传相关论文，或在「模型与设置」中配置大模型接口后重试。"
            )

        lines = ["（离线模式，未配置大模型 API Key；以下答案由检索到的原文证据直接拼接）"]
        for i, ev in enumerate(evidences[:3], 1):
            raw = ev["quote"].strip().replace("\n", " ")
            # 每条证据只引一句，引用标记逐句附加（与真实 LLM 的规范输出一致）
            sentences = split_sentences(raw)
            quote = sentences[0] if sentences else raw
            if len(quote) > 200:
                quote = quote[:200] + "…"
            safe_title = _safe_citation_title(ev.get("paper_title"))
            lines.append(f"根据《{safe_title}》第 {ev['page']} 页原文：{quote}[^e{i}]")
        lines.append("配置真实大模型后，这里会输出由模型基于证据组织的自然语言答案与跨论文对比。")
        titles = sorted({_safe_citation_title(e.get("paper_title")) for e in evidences[:3]})
        return LLMResponse(
            content="\n\n".join(lines),
            reasoning_content=(
                f"检索完成，命中 {len(evidences)} 条相关段落，来自《{('》、《').join(titles)}》。"
                "我逐句核对原文，只保留能直接支撑问题的内容，并为每个论断标注证据编号；"
                "没有原文支撑的内容一律不写。现在整理最终答案。"
            ),
        )

    def stream_chat(self, messages: list[dict], tools: list[dict] | None = None,
                    reasoning_effort: str | None = None):
        """离线流式：复用 chat 的结果，把思考/答案切块逐字吐出，再 yield 汇总。"""
        import os
        import time

        # 测试/演示节流：MOCK_STREAM_DELAY 为每个切块之间的秒数（默认 0，生产无影响）
        delay = float(os.environ.get("MOCK_STREAM_DELAY", "0") or 0)
        resp = self.chat(messages, tools=tools, reasoning_effort=reasoning_effort)
        # 离线模式没有真实用量，做一次估算以便演示与统计
        resp.usage = resp.usage or estimate_usage(messages, resp)

        def _emit(kind, text):
            for piece in _chunk(text):
                yield ("delta", kind, piece)
                if delay:
                    time.sleep(delay)

        if resp.reasoning_content:
            yield from _emit("thinking", resp.reasoning_content)
        # 工具调用轮没有正文；最终轮才逐字输出答案
        if resp.content and not resp.tool_calls:
            yield from _emit("answer", resp.content)
        yield ("final", resp)


_llm = None


def get_llm() -> BaseLLM:
    global _llm
    if _llm is not None:
        return _llm
    if settings.llm_mode in ("api", "litellm"):
        _llm = APIClient()
    else:
        _llm = MockLLM()
    return _llm


def reset_llm() -> None:
    """配置变更后调用，丢弃缓存的客户端，下次 get_llm 重新创建。"""
    global _llm
    _llm = None
