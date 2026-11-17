"""Frozen, independently diagnosed model features; unknown is not unsupported.

Shared search/OCR services are not capabilities of the selected chat model.
Only the exact admitted profile's observations can constrain its requests.
"""
from copy import deepcopy

CAPABILITIES = ("text", "streaming", "tools", "vision", "reasoning", "structured", "usage", "native_tools", "files")
LABELS = {"text": "文本回答", "streaming": "流式输出", "tools": "本地工具调用", "vision": "图片理解",
          "reasoning": "推理字段", "structured": "JSON 对象输出", "usage": "聊天用量",
          "native_tools": "原生联网工具", "files": "原生文件理解"}
TEXT_ONLY_NOTICE = "当前模型已明确不支持本地工具调用。本轮仍可直接对话；需要读取资料、搜索或执行操作的部分无法完成，请切换支持工具的模型后继续。"
STREAM_NOTICE = "当前模型已明确不支持流式输出，本轮将等待完整回答后显示。"


class CapabilityUnavailable(ValueError):
    def __init__(self, capability):
        self.capability = capability
        super().__init__(f"当前绑定模型已明确不支持{LABELS[capability]}，未丢弃输入或改用其他模型；请切换模型后继续。")


def statuses(value=None):
    return {name: (value or {}).get(name, "unknown") if (value or {}).get(name) in
            ("supported", "unsupported", "unknown") else "unknown" for name in CAPABILITIES}


def capability(binding, name):
    return statuses((binding or {}).get("capabilities"))[name]


def notices(binding):
    result = []
    if capability(binding, "tools") == "unsupported":
        result.append(TEXT_ONLY_NOTICE)
    if capability(binding, "streaming") == "unsupported":
        result.append(STREAM_NOTICE)
    return result


def text_only_messages(messages):
    result = deepcopy(messages)
    if any(message.get("role") == "system" and isinstance(message.get("content"), str)
           and message["content"].startswith(TEXT_ONLY_NOTICE) for message in result):
        return result
    position = 1 if result and result[0].get("role") == "system" else 0
    result.insert(position, {"role": "system", "content": TEXT_ONLY_NOTICE +
        "不得声称调用过工具、已核验附件或已完成工具操作；原有资料仅可按实际已提供内容回答。"})
    return result


def require_request(binding, messages, options):
    requested = {"text"}
    if options.get("tools"):
        requested.add("tools")
    if options.get("native_tools"):
        requested.add("native_tools")
    if options.get("response_format"):
        requested.add("structured")
    if options.get("reasoning_effort") not in (None, "", "none"):
        requested.add("reasoning")
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in ("image_url", "image"):
                    requested.add("vision")
                elif isinstance(part, dict) and part.get("type") in ("file", "input_file"):
                    requested.add("files")
    for name in sorted(requested):
        if capability(binding, name) == "unsupported":
            raise CapabilityUnavailable(name)
