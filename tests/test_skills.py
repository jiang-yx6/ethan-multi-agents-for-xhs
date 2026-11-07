import json
from dataclasses import FrozenInstanceError

import pytest

from xhs_agent.cli import main
from xhs_agent.service import TaskService
from xhs_agent.skills import SkillLibrary, SkillSource


@pytest.fixture
def library(tmp_path):
    return SkillLibrary.initialize(tmp_path / "library")


def make_skill(tmp_path, name="demo", description="测试技能"):
    directory = tmp_path / name
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: >-\n  {description}\n  按需加载。\n---\n\n# 正文\n仅测试。\n",
        encoding="utf-8",
    )
    (directory / "references").mkdir()
    (directory / "references" / "guide.md").write_text("本技能参考资料", encoding="utf-8")
    return directory


def import_demo(library, directory, agents=None):
    return library.import_skill(directory, source=SkillSource(repository="local-test"), agents=agents)


def test_import_is_ungranted_by_default_and_does_not_execute_code(library, tmp_path):
    directory = make_skill(tmp_path)
    (directory / "run.py").write_text("raise RuntimeError('不得执行')", encoding="utf-8")
    imported = import_demo(library, directory)
    assert imported.id == "demo"
    assert imported.path == "builtin/demo"
    assert not (library.root / "external").exists()
    assert library.for_agent("ideation").catalog() == []
    with pytest.raises(PermissionError):
        library.for_agent("ideation").load(imported.id)
    library.grant("ideation", imported.id)
    access = library.for_agent("ideation")
    assert "按需加载" in access.catalog()[0].description
    assert "# 正文" in access.load(imported.id).instructions
    assert "run.py" in access.load(imported.id).resources
    assert "不得执行" in access.read_resource(imported.id, "run.py")


def test_custom_import_id_avoids_name_collisions_in_project_layout(library, tmp_path):
    directory = make_skill(tmp_path)
    original = library.import_skill(directory)
    copied = library.import_skill(directory, skill_id="demo-copy", agents=["creation"])
    assert original.id == "demo"
    assert copied.id == "demo-copy"
    assert copied.path == "builtin/demo-copy"
    assert copied.source.repository == "local"
    assert library.for_agent("creation").load(copied.id).instructions == (directory / "SKILL.md").read_text(encoding="utf-8")
    assert (library.root / original.path / "SKILL.md").is_file()
    assert not (library.root / "external").exists()


def test_overridden_id_is_used_for_staging_untrusted_metadata_name(library, tmp_path):
    directory = make_skill(tmp_path)
    (directory / "SKILL.md").write_text("---\nname: ../../escaped\ndescription: test\n---\n正文", encoding="utf-8")
    imported = library.import_skill(directory, skill_id="safe", agents=["creation"])
    assert imported.path == "builtin/safe"
    assert library.for_agent("creation").load("safe").instructions.endswith("正文")
    assert not (library.root / "escaped").exists()


def test_scope_revoke_and_disable_are_checked_on_every_access(library, tmp_path):
    imported = import_demo(library, make_skill(tmp_path), ["ideation"])
    ideation = library.for_agent("ideation")
    research = library.for_agent("research")
    assert [s.id for s in ideation.catalog()] == [imported.id]
    assert research.catalog() == []
    with pytest.raises(PermissionError):
        research.read_resource(imported.id, "references/guide.md")
    with pytest.raises(FrozenInstanceError):
        ideation.role = "review"
    library.revoke("ideation", imported.id)
    with pytest.raises(PermissionError):
        ideation.load(imported.id)
    library.grant("ideation", imported.id)
    data = json.loads(library.registry_path.read_text(encoding="utf-8"))
    data["skills"][0]["enabled"] = False
    library.registry_path.write_text(json.dumps(data), encoding="utf-8")
    assert ideation.catalog() == []
    with pytest.raises(PermissionError):
        ideation.load(imported.id)


@pytest.mark.parametrize("relative", ["../other/SKILL.md", "../../registry.json", "C:/Windows/win.ini", "/etc/passwd"])
def test_resource_paths_cannot_escape_assigned_skill(library, tmp_path, relative):
    imported = import_demo(library, make_skill(tmp_path), ["creation"])
    with pytest.raises((PermissionError, ValueError)):
        library.for_agent("creation").read_resource(imported.id, relative)


def test_shared_references_require_explicit_per_skill_mapping(library, tmp_path):
    first = import_demo(library, make_skill(tmp_path, "first"), ["ideation"])
    second = import_demo(library, make_skill(tmp_path, "second"), ["ideation"])
    shared = library.root / "shared"
    shared.mkdir()
    (shared / "scoring.md").write_text("共享评分依据", encoding="utf-8")
    library.register_resources(first.id, {"../../shared/scoring.md": "shared/scoring.md"})
    access = library.for_agent("ideation")
    assert access.read_resource(first.id, "../../shared/scoring.md") == "共享评分依据"
    with pytest.raises(PermissionError):
        access.read_resource(second.id, "../../shared/scoring.md")
    with pytest.raises(ValueError):
        library.register_resources(first.id, {"x": "../outside.md"})


def test_body_integrity_and_duplicate_import(library, tmp_path):
    directory = make_skill(tmp_path)
    imported = import_demo(library, directory, ["review"])
    with pytest.raises(ValueError, match="已存在"):
        import_demo(library, directory)
    path = library.root / imported.path / "SKILL.md"
    path.write_text("已改变", encoding="utf-8")
    with pytest.raises(ValueError, match="正文已改变"):
        library.for_agent("review").load(imported.id)
    with pytest.raises(ValueError, match="正文已改变"):
        library.for_agent("review").read_resource(imported.id, "SKILL.md")


def test_external_license_is_preserved(library, tmp_path):
    directory = make_skill(tmp_path)
    license_path = tmp_path / "LICENSE.txt"
    license_path.write_text("测试许可证原文", encoding="utf-8")
    imported = library.import_skill(directory, source=SkillSource(repository="local"),
                                    license_path=license_path)
    assert (library.root / imported.source.license_file).read_text(encoding="utf-8") == "测试许可证原文"


def test_registry_write_failure_does_not_leave_orphan_import(library, tmp_path, monkeypatch):
    directory = make_skill(tmp_path)
    original = library._write_registry
    monkeypatch.setattr(library, "_write_registry", lambda registry: (_ for _ in ()).throw(OSError("模拟写入失败")))
    with pytest.raises(OSError):
        import_demo(library, directory)
    assert not (library.root / "builtin/demo").exists()
    monkeypatch.setattr(library, "_write_registry", original)
    assert import_demo(library, directory).id == "demo"


def test_invalid_registry_fails_closed(library):
    library.registry_path.write_text(json.dumps({"version": 1, "agents": {"ideation": ["*"]}}), encoding="utf-8")
    with pytest.raises(ValueError):
        library.for_agent("ideation").catalog()
    with pytest.raises(ValueError):
        library.for_agent("administrator")


def test_service_binds_distinct_scopes_and_builds_metadata_only_prompts(tmp_path):
    svc = TaskService(tmp_path / "data")
    try:
        assert svc.ideation_agent.skills.role == "ideation"
        assert svc.research_agent.skills.role == "research"
        assert svc.creation_agent.skills.role == "creation"
        assert svc.review_agent.skills.role == "review"
        assert svc.publisher.skills.catalog() == []
        assert "skill-topic-evaluator" in svc.ideation_agent.skill_prompt()
        assert "skill-quality-gate" not in svc.ideation_agent.skill_prompt()
        with pytest.raises(PermissionError):
            svc.ideation_agent.skills.load("skill-quality-gate")
        assert "references/general-rules.md" in svc.review_agent.skills.load("skill-quality-gate").resources
    finally:
        svc.close()


def test_project_skills_and_explicit_resources_are_readable():
    library = SkillLibrary()
    assert not (library.root / "external").exists()
    assert all("/" not in s.id for s in library.for_agent("ideation").catalog())
    for role in ("ideation", "research", "creation", "review"):
        access = library.for_agent(role)
        for summary in access.catalog():
            loaded = access.load(summary.id)
            assert loaded.instructions.startswith("---")
            for relative in loaded.resources:
                assert access.read_resource(summary.id, relative)
    source = json.loads(library.registry_path.read_text(encoding="utf-8"))["skills"][0]["source"]
    assert len(source["revision"]) == 40
    assert (library.root / source["license_file"]).is_file()


def test_cli_import_list_read_and_scope_changes(tmp_path, capsys):
    directory = make_skill(tmp_path)
    base = ["--skills-dir", str(tmp_path / "library")]
    assert main([*base, "skills", "init"]) == 0
    assert main([*base, "skills", "import", str(directory), "--source", "local-test"]) == 0
    assert main([*base, "skills", "show", "creation", "demo"]) == 1
    assert main([*base, "skills", "grant", "creation", "demo"]) == 0
    assert main([*base, "skills", "list", "--agent", "creation"]) == 0
    assert "demo" in capsys.readouterr().out
    assert main([*base, "skills", "read", "creation", "demo", "references/guide.md"]) == 0
    assert main([*base, "skills", "show", "review", "demo"]) == 1
    assert main([*base, "skills", "revoke", "creation", "demo"]) == 0
    assert main([*base, "skills", "show", "creation", "demo"]) == 1
