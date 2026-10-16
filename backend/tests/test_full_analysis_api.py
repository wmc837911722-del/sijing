"""The project API distinguishes complete analysis from historic previews."""
from fastapi.testclient import TestClient

from businessmap.app import create_app


def test_full_analysis_status_is_authenticated_and_does_not_start_model(tmp_path):
    app = create_app(tmp_path, "test-only-token-at-least-16")
    store = app.state.store
    try:
        for rid in ("front", "back"):
            store._write_repo({"id": rid, "name": rid, "vault_path": str(store.vault),
                               "path": "unused", "url": "unused", "commit": "a" * 40, "status": "ready"})
        project = store.create_project("业务", "", ["front", "back"], {"front": "frontend", "back": "backend"})
        client = TestClient(app)
        endpoint = f"/api/projects/{project['id']}/full-analysis"
        assert client.get(endpoint).status_code == 401
        response = client.get(endpoint, headers={"Authorization": "Bearer test-only-token-at-least-16"})
        assert response.status_code == 200
        assert response.json()["status"] == "never"
        assert not store.config_get("project_analysis:" + project["id"])
        assert not [task for task in store.tasks() if task.get("kind") == "project-analysis"]
    finally:
        store.close()
