"""Authenticated localhost HTTP API for the desktop application."""

from contextlib import asynccontextmanager
import hmac
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field

from .core import CoreError, Store


class SaveNote(BaseModel):
    path: str
    content: str
    revision: str | None = None


class NewNote(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    folder: str = ""


class OpenVault(BaseModel):
    path: str


class GitConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auth_type: Literal["none", "https", "ssh-agent", "ssh-key"] = "none"
    username: str = Field(default="", max_length=256)
    secret: str | None = Field(default=None, max_length=16384)
    proxy_url: str = Field(default="", max_length=4096)
    ca_bundle: str = Field(default="", max_length=4096)
    ssh_key_path: str = Field(default="", max_length=4096)
    known_hosts_path: str = Field(default="", max_length=4096)


class ImportRepository(BaseModel):
    url: str = Field(min_length=1, max_length=4096)
    name: str | None = Field(default=None, max_length=100)
    branch: str = Field(default="", max_length=256)
    connection: GitConnection | None = None


class TestRepository(BaseModel):
    url: str = Field(min_length=1, max_length=4096)
    repository_id: str | None = None
    connection: GitConnection | None = None


class UpdateConnection(BaseModel):
    connection: GitConnection
    branch: str | None = Field(default=None, max_length=256)


def create_app(data_dir: str | Path, token: str) -> FastAPI:
    if not token or len(token) < 16:
        raise ValueError("A session token of at least 16 characters is required")
    store = Store(data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.start()
        try:
            yield
        finally:
            # close can wait for a bounded Git subprocess; don't block the event loop.
            import asyncio
            await asyncio.to_thread(store.close)

    app = FastAPI(title="Sijing Local Service", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store

    @app.exception_handler(CoreError)
    async def core_error(_request: Request, exc: CoreError):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc), **({"code": exc.code} if hasattr(exc, "code") else {})})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        # Pydantic's default `input` can echo a supplied password into the response.
        return JSONResponse(status_code=422, content={"detail": "请求字段无效，请检查输入格式。",
                            "fields": [{"loc": error["loc"], "type": error["type"]} for error in exc.errors()]})

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if request.url.path.startswith("/api"):
            expected = "Bearer " + token
            if not hmac.compare_digest(request.headers.get("authorization", "").encode(), expected.encode()):
                return JSONResponse(status_code=401, content={"detail": "需要有效的本地会话令牌"})
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/api/workspace")
    def workspace():
        return store.workspace()

    @app.get("/api/notes")
    def notes(q: str = ""):
        return {"notes": store.notes(q)}

    @app.get("/api/note")
    def note(path: str):
        return store.read_note(path)

    @app.put("/api/note")
    def save_note(body: SaveNote):
        saved = store.save_note(body.path, body.content, body.revision)
        store.activity("已保存笔记", body.path, "note")
        return saved

    @app.post("/api/notes")
    def new_note(body: NewNote):
        return store.create_note(body.title, body.folder)

    @app.get("/api/graph")
    def graph():
        return store.graph()

    @app.post("/api/vault")
    def vault(body: OpenVault):
        return store.open_vault(body.path)

    @app.post("/api/repositories")
    def repository(body: ImportRepository):
        return store.add_repository(body.url, body.name, body.branch,
                                    body.connection.model_dump() if body.connection else None)

    @app.post("/api/repositories/test")
    def test_repository(body: TestRepository):
        return store.git_transport.test(body.url, body.connection.model_dump() if body.connection else None,
                                        body.repository_id)

    @app.put("/api/repositories/{repo_id}/connection")
    def update_connection(repo_id: str, body: UpdateConnection):
        return store.update_repository_connection(repo_id, body.connection.model_dump(), body.branch)

    @app.post("/api/repositories/{repo_id}/sync")
    def sync_repository(repo_id: str):
        return store.sync_repository(repo_id)

    @app.get("/api/tasks")
    def tasks():
        return {"tasks": store.tasks()}

    @app.get("/api/activity")
    def activity():
        return {"activities": store.activities()}

    from .tools_api import create_router
    app.include_router(create_router(store))
    return app
