"""Move verified topic files inside a fixture vault without changing model receipts."""

import pytest

from businessmap import public_topic_titles
from businessmap.core import CoreError
from businessmap.full_analysis import FullProjectAnalysis
from test_full_analysis import full_project
from test_semantic_integration import semantic_pipeline


@pytest.fixture
def moved_topic(full_project, monkeypatch, request):
    full, service, project, calls = semantic_pipeline(full_project, monkeypatch)
    old_title = "旧门店名称"
    title = [old_title]
    paginated = getattr(request, "param", False)
    if paginated:
        full.PAGE_BYTES = 4000

    def show(projection, project_name):
        item, = projection["public_documents"]
        old, new = item["title"], title[0]
        body = item["body"].replace(old, new)
        if paginated:
            repeats = 300 if new == old_title else 135
            body += "\n\n" + "营业范围须由门店确认。\n\n" * repeats
        return ([{**item, "title": new, "body": body}],
                projection["overview_body"].replace(old, new),
                projection["summary_body"].replace(old, new))

    monkeypatch.setattr(public_topic_titles, "public_topic_presentation", show)
    assert full.analyze(project["id"])["publication_complete"]

    def notes():
        return service.store.config_get("project_analysis:" + project["id"])["notes"]

    key, = [key for key in notes() if key.startswith("topic:") and ":part:" not in key]
    old = service.store.vault / notes()[key]["path"]
    title[0] = "门店营业范围"
    return full, service, project["id"], calls, notes, key, old, old.with_name(title[0] + ".md")


def test_relocate_owned_topic_and_indexes_without_resending(moved_topic):
    full, service, project, calls, notes, key, old, new = moved_topic
    initial = list(calls)
    assert full.analyze(project)["publication_complete"]
    assert not old.exists() and new.stem in new.read_text()
    assert notes()[key]["path"] == new.relative_to(service.store.vault).as_posix()
    for index in ("overview", "summary"):
        rendered = (service.store.vault / notes()[index]["path"]).read_text()
        assert new.stem in rendered and old.stem not in rendered
    assert full.analyze(project)["publication_complete"] and calls == initial


@pytest.mark.parametrize("obstruction", ("edited", "occupied", "markdown", "wiki", "other-project"))
def test_relocation_conflict_preserves_both_addresses(moved_topic, obstruction):
    full, service, project, calls, notes, key, old, new = moved_topic
    if obstruction == "edited":
        old.write_text(old.read_text() + "\n人工说明\n")
    if obstruction == "occupied":
        new.write_bytes(old.read_bytes())
    if obstruction == "markdown":
        manual = old.parent / "用户说明.md"
        manual.write_text("参见 [原文](./" + old.name + "#详细信息)")
    if obstruction == "wiki":
        (service.store.vault / "人工引用.md").write_text(f"参见 [[{old.parent.name}/{old.stem}]]")
    if obstruction == "other-project":
        owned = {**notes()[key], "path": notes()[key]["path"].replace("01-Wiki", "01-wiki")}
        service.store.config_set("project_analysis:another", {"vault_path": str(service.store.vault), "notes": {key: owned}})
    before = old.read_bytes()
    occupied = new.read_bytes() if new.exists() else None
    assert not full.analyze(project)["publication_complete"]
    assert old.read_bytes() == before and (new.read_bytes() if new.exists() else None) == occupied
    assert notes()[key]["path"] == old.relative_to(service.store.vault).as_posix()


@pytest.mark.parametrize("race", ("same-bytes", "late-backlink"))
def test_changes_after_plan_survive_publication_rollback(moved_topic, monkeypatch, race):
    full, service, project, calls, notes, key, old, new = moved_topic
    install = full._install_moved_note
    expected = []

    def write(vault, directory, manifest, entry, content):
        if race == "same-bytes":
            expected.append(content)
            new.write_text(content)
        else:
            (vault / "临时索引.md").write_text(f"[[{old.stem}]]")
        return install(vault, directory, manifest, entry, content)

    monkeypatch.setattr(full, "_install_moved_note", write)
    with pytest.raises(CoreError) as failure:
        full.analyze(project)
    assert failure.value.status_code == 409 and old.exists()
    if expected:
        assert new.read_text() == expected[0]
    else:
        assert not new.exists()
    assert not list(service.store.vault.rglob(".sijing-*"))


@pytest.mark.parametrize("moved_topic", (True,), indirect=True)
@pytest.mark.parametrize("edited_part", (False, True))
def test_complete_old_and_new_page_families(moved_topic, edited_part):
    full, service, project, calls, notes, key, old, new = moved_topic
    initial = list(calls)
    old_parts = [service.store.vault / meta["path"] for part, meta in notes().items() if part.startswith(key + ":part:")]
    assert len(old_parts) > 2
    if edited_part:
        old_parts[-1].write_text("用户编辑的分页")
    result = full.analyze(project)
    if edited_part:
        assert not result["publication_complete"] and old.exists() and not new.exists()
        assert old_parts[-1].read_text() == "用户编辑的分页" and all(path.is_file() for path in old_parts)
    else:
        assert result["publication_complete"] and not old.exists() and not any(path.exists() for path in old_parts)
        new_parts = [service.store.vault / meta["path"] for part, meta in notes().items() if part.startswith(key + ":part:")]
        assert 1 < len(new_parts) < len(old_parts)
        assert not old.with_suffix("").exists()
        assert all(path.is_file() and path.stem in new.read_text() for path in new_parts)
    assert calls == initial


@pytest.mark.parametrize("point", ("install", "before-commit", "after-commit"))
def test_restart_recovers_owned_files_using_commit_marker(moved_topic, monkeypatch, point):
    full, service, project, calls, notes, key, old, new = moved_topic
    prior = old.read_bytes(), dict(notes()[key]), list(calls)
    run = full._load(project)

    class PowerLoss(BaseException):
        pass

    with monkeypatch.context() as patch:
        if point == "before-commit":
            save = full.project_analysis._set

            def interrupt(db, name, value):
                if name == "project_analysis:" + project:
                    raise PowerLoss()
                return save(db, name, value)

            patch.setattr(full.project_analysis, "_set", interrupt)
        elif point == "after-commit":
            def interrupt_cleanup(*args):
                raise PowerLoss()

            patch.setattr(full, "_finish_moves", interrupt_cleanup)
        else:
            def interrupt_link(*args, **options):
                raise PowerLoss()

            patch.setattr("businessmap.full_analysis.os.link", interrupt_link)
        with pytest.raises(PowerLoss):
            full.analyze(project)

    restarted = FullProjectAnalysis(service)
    restarted._recover_publications(run)
    if point == "after-commit":
        assert new.is_file() and not old.exists() and notes()[key]["path"].endswith(new.name)
    else:
        assert old.read_bytes() == prior[0] and notes()[key] == prior[1] and not new.exists()
    assert not list(service.store.vault.rglob(".sijing-*"))
    assert restarted.analyze(project)["publication_complete"] and calls == prior[2]

def test_postcommit_old_editor_keeps_its_visible_file(moved_topic, monkeypatch):
    full, service, project, calls, notes, key, old, new = moved_topic
    finish = full._finish_moves
    with old.open("r+") as editor:
        def finish_with_edit(manifest, directory, committed):
            if committed and manifest.get("retired"):
                editor.seek(0)
                editor.write("用户正在修改的原文")
                editor.truncate()
                editor.flush()
            return finish(manifest, directory, committed)

        monkeypatch.setattr(full, "_finish_moves", finish_with_edit)
        with pytest.raises(CoreError) as error:
            full.analyze(project)
    assert error.value.status_code == 409
    assert old.read_text() == "用户正在修改的原文"
    assert new.is_file() and notes()[key]["path"].endswith(new.name)


@pytest.mark.parametrize("moved_topic", (True,), indirect=True)
def test_retirement_keeps_other_files_in_old_page_folder(moved_topic):
    full, _, project, _, _, _, old, new = moved_topic
    human = old.with_suffix("") / "门店工作手册.md"
    human.write_text("人工保留的说明")
    assert full.analyze(project)["publication_complete"] and new.is_file()
    assert human.read_text() == "人工保留的说明"
