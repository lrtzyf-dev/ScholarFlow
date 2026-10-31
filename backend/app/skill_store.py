"""三级技能库（对标 Codex 技能分层）。

技能 = 目录 + SKILL.md（YAML frontmatter + 正文）。三个层级按优先级叠加：
- 内置 builtin：随软件本体分发（app/skills），优先级最低；
- 用户 user：DATA_DIR/skills，用户自建；
- 项目 project：DATA_DIR/project_skills，针对具体课题，优先级最高。

同名技能由更高层级覆盖；支持单技能禁用（禁用后不自动加载，显式 `$name` 仍可调用）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import DATA_DIR

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_NAME_OK = re.compile(r"^[a-zA-Z0-9_-]+$")

SETTINGS_PATH = Path(DATA_DIR) / "skill_settings.json"


def _builtin_dir() -> Path:
    """内置技能目录；兼容 PyInstaller 的 _MEIPASS 资源路径。"""
    here = Path(__file__).resolve().parent / "skills"
    if here.exists():
        return here
    import sys

    return Path(getattr(sys, "_MEIPASS", "")) / "app" / "skills"


def _tier_dirs() -> list[tuple[str, Path]]:
    """返回 (层级, 目录)，优先级从低到高。"""
    return [
        ("builtin", _builtin_dir()),
        ("user", Path(DATA_DIR) / "skills"),
        ("project", Path(DATA_DIR) / "project_skills"),
    ]


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, m.group(2)


def _load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return {"disabled": []}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"disabled": []}
    data.setdefault("disabled", [])
    return data


def _save_settings(data: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _disabled_names() -> set[str]:
    return set(_load_settings().get("disabled", []))


def set_disabled(name: str, disabled: bool) -> dict:
    """启用/禁用单个技能（按技能名）。"""
    data = _load_settings()
    names = set(data.get("disabled", []))
    if disabled:
        names.add(name)
    else:
        names.discard(name)
    data["disabled"] = sorted(names)
    _save_settings(data)
    return data


def list_skills(include_disabled: bool = True) -> list[dict]:
    """列出全部技能；同名以更高层级覆盖，附带 tier 与 disabled 标记。"""
    disabled = _disabled_names()
    merged: dict[str, dict] = {}
    for tier, root in _tier_dirs():
        if not root.exists():
            continue
        for d in sorted(root.iterdir()):
            f = d / "SKILL.md"
            if not f.exists():
                continue
            try:
                meta, _ = _parse_frontmatter(f.read_text(encoding="utf-8"))
            except OSError:
                continue
            name = meta.get("name", d.name)
            merged[name] = {
                "name": name,
                "title": meta.get("title", name),
                "description": meta.get("description", ""),
                "tier": tier,
                "disabled": name in disabled,
            }
    out = list(merged.values())
    if not include_disabled:
        out = [s for s in out if not s["disabled"]]
    return sorted(out, key=lambda s: s["name"])


def load_skill(name: str, explicit: bool = False) -> dict:
    """加载技能正文；按层级从高到低查找，高优先级覆盖。

    explicit=True（显式 `$name` 调用）时忽略禁用状态。
    """
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    if not explicit and safe in _disabled_names():
        return {"error": f"技能 {safe} 已被禁用，可在设置中启用或用 $ 显式调用",
                "disabled": True}
    # 从高优先级到低优先级查找
    for tier, root in reversed(_tier_dirs()):
        f = root / safe / "SKILL.md"
        if not f.exists():
            continue
        meta, body = _parse_frontmatter(f.read_text(encoding="utf-8"))
        return {
            "name": meta.get("name", safe),
            "title": meta.get("title", meta.get("name", safe)),
            "description": meta.get("description", ""),
            "tier": tier,
            "content": body.strip(),
        }
    return {"error": f"未找到技能: {name}",
            "available": [s["name"] for s in list_skills()]}


def create_user_skill(form: dict) -> dict:
    """在用户层级新建技能（写 SKILL.md）。"""
    name = (form.get("name") or "").strip()
    if not _NAME_OK.match(name):
        raise ValueError("技能名仅允许字母、数字、下划线与连字符")
    user_root = Path(DATA_DIR) / "skills" / name
    if user_root.exists():
        raise ValueError("用户层级已存在同名技能，请换名或直接编辑")
    title = (form.get("title") or name).strip()
    description = (form.get("description") or "").strip()
    content = (form.get("content") or "").strip()
    user_root.mkdir(parents=True, exist_ok=True)
    frontmatter = (
        "---\n"
        f"name: {name}\n"
        f"title: {title}\n"
        f"description: {description}\n"
        "---\n\n"
    )
    (user_root / "SKILL.md").write_text(
        frontmatter + content + "\n", encoding="utf-8"
    )
    return {"name": name, "title": title, "tier": "user"}


def delete_user_skill(name: str) -> None:
    """删除用户层级自建技能（内置/项目技能不在此删除）。"""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    target = Path(DATA_DIR) / "skills" / safe
    if not target.exists():
        raise ValueError("该用户技能不存在")
    import shutil

    shutil.rmtree(target)
