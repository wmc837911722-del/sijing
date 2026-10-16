from pathlib import Path
import subprocess
import time

from businessmap.core import Store


def test_analysis_sample_does_not_let_first_module_hide_later_modules(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    for index in range(45):
        file = source / "a-module" / f"Rule{index:02d}.py"
        file.parent.mkdir(exist_ok=True)
        file.write_text("RULE = 'first module'\n" * 180)
    other = source / "z-business" / "SettlementRule.py"
    other.parent.mkdir()
    other.write_text("LOSER_STAR_DELTA = -1\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
    store = Store(tmp_path / "data")
    try:
        result = store.add_repository(str(source), "Business")
        for _ in range(200):
            repo = store.get_repository(result["id"])
            if repo["status"] == "ready":
                break
            time.sleep(.01)
        assert repo["status"] == "ready"
        context = store.repository_context(repo["id"])
        assert "z-business/SettlementRule.py" in [item["path"] for item in context["files"]]
        assert context["total_files"] == 46
        assert context["total_modules"] == 2
        assert context["sampled_modules"] == ["a-module", "z-business"]
        assert len(context["files"]) <= 30
        assert sum(len(item["content"]) for item in context["files"]) <= 60_000
        assert context["truncated"] is True
    finally:
        store.close()
