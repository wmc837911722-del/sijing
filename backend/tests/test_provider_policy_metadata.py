"""Authorization checks compare destinations without opening credential storage."""
from test_full_analysis import full_project


def test_repository_authorization_does_not_read_model_credentials(full_project, monkeypatch):
    full, service, project = full_project
    def forbidden(_key):
        raise AssertionError("authorization checks must not read credentials")
    monkeypatch.setattr(service.providers.secrets, "get", forbidden)
    assert service.model_policy("front")["status"] == "ready"
    assert service.repository_provider("front")["model"] == "fixture"
    snapshot = full.project_analysis.snapshot(project["id"])
    full.project_analysis.guard(snapshot)


def test_project_guard_uses_a_bounded_consistent_database_snapshot(full_project, monkeypatch):
    full, service, project = full_project
    snapshot = full.project_analysis.snapshot(project["id"])
    original = service.store._db
    opens = []
    def database():
        opens.append(True)
        return original()
    monkeypatch.setattr(service.store, "_db", database)
    full.project_analysis.guard(snapshot)
    # One cross-process vault refresh plus one transaction for all consent and
    # repository metadata, independent of the number of project members.
    assert len(opens) <= 2
