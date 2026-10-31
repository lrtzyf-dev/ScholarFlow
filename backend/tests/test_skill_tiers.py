"""三级技能库回归：分层叠加、禁用、显式调用、用户自建。"""
from pathlib import Path

from app import skill_store
from app.config import DATA_DIR


def test_builtin_skills_have_tier():
    skills = skill_store.list_skills()
    assert skills, "应至少列出内置技能"
    assert all("tier" in s and "disabled" in s for s in skills)
    assert any(s["tier"] == "builtin" for s in skills)


def test_disable_excludes_from_default_list():
    name = skill_store.list_skills()[0]["name"]
    skill_store.set_disabled(name, True)
    assert skill_store.load_skill(name).get("disabled") is True
    # 默认列表仍包含（带禁用标记），但 include_disabled=False 时排除
    assert all(s["name"] != name for s in skill_store.list_skills(False))
    # 显式调用可绕过禁用
    assert "content" in skill_store.load_skill(name, explicit=True)
    skill_store.set_disabled(name, False)


def test_user_skill_create_and_load():
    res = skill_store.create_user_skill({
        "name": "my-skill",
        "title": "我的技能",
        "description": "自定义流程",
        "content": "第一步……",
    })
    assert res["tier"] == "user"
    loaded = skill_store.load_skill("my-skill")
    assert loaded["tier"] == "user"
    assert loaded["content"] == "第一步……"
    skill_store.delete_user_skill("my-skill")
    assert "error" in skill_store.load_skill("my-skill")


def test_project_tier_overrides_builtin():
    # 取一个内置技能名，在项目层级放同名目录实现覆盖
    builtin_name = skill_store.list_skills()[0]["name"]
    proj_dir = Path(DATA_DIR) / "project_skills" / builtin_name
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {builtin_name}\n"
        "title: 项目覆盖版\n"
        "description: 项目级\n"
        "---\n\n项目专属正文",
        encoding="utf-8",
    )
    loaded = skill_store.load_skill(builtin_name)
    assert loaded["tier"] == "project"
    assert loaded["content"] == "项目专属正文"


def test_create_rejects_bad_name():
    try:
        skill_store.create_user_skill({"name": "bad name!", "content": "x"})
    except ValueError:
        return
    raise AssertionError("非法技能名应抛出 ValueError")
