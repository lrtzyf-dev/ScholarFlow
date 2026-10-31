"""分层 AGENTS.md 工作规范（对标 Codex）。

应用级结构（所有会话通用）：
1. 全局层：DATA_DIR/AGENTS.md —— 对所有项目/会话生效的通用规范；
2. 补充层：config.work_rules —— 兼容旧设置中的“项目规范”，实际对所有会话生效。

真实目录规范由 workspace.directory_rules 发现，在以上两层之后按目录范围注入。
单个应用规范文件写入上限 32 KiB，超限写入会被拒绝。
"""
from __future__ import annotations

from pathlib import Path

from .config import DATA_DIR

# 单个 AGENTS.md 最大字节数（32 KiB）
MAX_AGENTS_BYTES = 32 * 1024

# 全局工作规范文件路径
GLOBAL_AGENTS_PATH: Path = DATA_DIR / "AGENTS.md"


class AgentsMdError(ValueError):
    """AGENTS.md 校验失败（如超过 32 KiB）。"""


def _byte_size(text: str) -> int:
    """按 UTF-8 编码计算字节数。"""
    return len(text.encode("utf-8"))


def validate_size(text: str, label: str = "AGENTS.md") -> None:
    """校验文本不超过 32 KiB。"""
    size = _byte_size(text)
    if size > MAX_AGENTS_BYTES:
        raise AgentsMdError(
            f"{label} 大小为 {size} 字节，超过上限 {MAX_AGENTS_BYTES} 字节（32 KiB）"
        )


def load_global_rules() -> str:
    """读取全局 AGENTS.md；不存在或读取失败时返回空串。"""
    if not GLOBAL_AGENTS_PATH.exists():
        return ""
    try:
        return GLOBAL_AGENTS_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def write_global_rules(text: str) -> str:
    """写入全局 AGENTS.md，先做 32 KiB 校验。返回写入后的内容。"""
    text = text or ""
    validate_size(text, label="全局 AGENTS.md")
    GLOBAL_AGENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    GLOBAL_AGENTS_PATH.write_text(text, encoding="utf-8")
    return text


def merge_rules(global_rules: str, project_rules: str) -> str:
    """合并全局层与项目层，带清晰分节标题；项目层靠后，冲突时优先。

    空层自动跳过；两层皆空时返回空串。
    """
    sections: list[str] = []
    global_rules = (global_rules or "").strip()
    project_rules = (project_rules or "").strip()
    if global_rules:
        sections.append("## 全局规范（所有项目通用）\n" + global_rules)
    if project_rules:
        sections.append("## 应用补充规范（旧设置项：项目规范；对所有会话生效）\n" + project_rules)
    if not sections:
        return ""
    return "# 工作规范（必须遵循）\n\n" + "\n\n".join(sections)


def load_merged_rules(project_rules: str = "") -> str:
    """读取全局文件并与项目层合并，得到最终注入系统提示的文本。"""
    return merge_rules(load_global_rules(), project_rules)


# /init 一键生成的研究向 AGENTS.md 脚手架模板
SCAFFOLD_TEMPLATE = """# AGENTS.md

本文件用于约束 ScholarFlow 的科研协作行为，可按课题组实际情况增删。

## 研究方向
- 主要领域：例如「自然语言处理 / 计算机视觉」
- 关注主题：例如「大模型高效推理、检索增强生成」

## 语言与写作
- 回答默认使用：简体中文（专业术语保留英文原词）
- 写作风格：客观、严谨、结论先行，避免口语化与无依据的夸张表述
- 论文结构遵循：IMRaD（引言、方法、实验、结果、讨论）

## 引用与证据
- 所有事实性论断必须标注可追溯来源，引用格式遵循目标期刊/会议要求
- 引用须真实存在，禁止编造文献、作者、年份或 DOI
- 证据不足时明确说明「证据不足」，不得臆测或用常识冒充文献结论

## 工作流程
- 复杂任务先拆解为可核对的子任务，再逐步执行并自检
- 涉及外部写入、联网、删除等动作时，先确认再执行
- 交付前核对：数据口径一致、引用可追溯、无遗漏的待办

## 禁忌
- 不输出与问题无关的冗余内容
- 不在未核实的情况下给出确定性结论
- 不泄露任何密钥、个人隐私等敏感信息
"""


def init_agents_md(overwrite: bool = False) -> tuple[str, bool]:
    """一键生成全局 AGENTS.md 脚手架。

    返回 (最终内容, 是否本次新建)。文件已存在且 overwrite=False 时不覆盖。
    """
    if GLOBAL_AGENTS_PATH.exists() and not overwrite:
        return load_global_rules(), False
    write_global_rules(SCAFFOLD_TEMPLATE)
    return SCAFFOLD_TEMPLATE, True
