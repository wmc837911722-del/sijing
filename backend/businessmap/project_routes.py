"""Authenticated parent router mounts these business-project routes under /api."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from .app import GitConnection
from .business_projects import BusinessProjects
from .project_analysis import ProjectAnalysis
from .providers import ToolError


class ProjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    repository_ids: list[str] = Field(min_length=1, max_length=100)
    repository_roles: dict[str, str] = Field(default_factory=dict)


class UpdateProjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=1, max_length=160)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4000)
    repository_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)
    repository_roles: dict[str, str] | None = None


class RepositoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=4096)
    name: str | None = Field(default=None, max_length=160)
    branch: str = Field(default="", max_length=256)
    connection: GitConnection | None = None


class ImportProjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    frontend: list[RepositoryInput] = Field(min_length=1, max_length=100)
    backend: list[RepositoryInput] = Field(min_length=1, max_length=100)


class ProjectModelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str = Field(min_length=1, max_length=160)


class FullAnalysisPauseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: bool


class ContinueWithModelBody(ProjectModelBody):
    source_run_id: str = Field(min_length=1, max_length=160)


class ProjectRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request):
            try:
                return await original(request)
            except ToolError as error:
                return JSONResponse(status_code=error.status_code, content={"detail": error.message})
        return handler


def create_project_router(service):
    router = APIRouter(prefix="/projects", route_class=ProjectRoute)
    projects = BusinessProjects(service.store)
    analysis = ProjectAnalysis(service)

    @router.get("")
    def list_projects():
        return {"projects": projects.list()}

    @router.post("")
    def create_project(body: ProjectBody):
        return projects.create(**body.model_dump())

    @router.post("/import")
    def import_project(body: ImportProjectBody):
        return projects.import_repositories(**body.model_dump())

    @router.put("/{project_id}")
    def update_project(project_id: str, body: UpdateProjectBody):
        values = body.model_dump(exclude_none=True)
        expected_revision = values.pop("revision")
        return projects.update(project_id, expected_revision, **values)

    @router.get("/{project_id}/model-policy")
    def model_policy(project_id: str):
        return analysis.model_policy(project_id)

    @router.post("/{project_id}/model-policy")
    def bind_model(project_id: str, body: ProjectModelBody):
        return analysis.bind_model(project_id, body.provider_id)

    @router.get("/{project_id}/analysis-status")
    def analysis_status(project_id: str):
        return analysis.status(project_id)

    @router.post("/{project_id}/analyze")
    def analyze(project_id: str):
        return analysis.queue(project_id)

    @router.get("/{project_id}/full-analysis")
    def full_analysis_status(project_id: str):
        from .full_analysis import FullProjectAnalysis
        return FullProjectAnalysis(service).status(project_id)

    @router.post("/{project_id}/full-analysis/pause")
    def pause_full_analysis(project_id: str, body: FullAnalysisPauseBody):
        from .full_analysis import FullProjectAnalysis
        full = FullProjectAnalysis(service)
        result = full.set_paused(project_id, body.paused)
        if not body.paused:
            queued = analysis.queue(project_id)
            return {**result, **queued}
        return result

    @router.post("/{project_id}/full-analysis/continue-with-model")
    def continue_with_model(project_id: str, body: ContinueWithModelBody):
        from .full_analysis import FullProjectAnalysis
        return FullProjectAnalysis(service).continue_with_provider(project_id, body.provider_id, body.source_run_id)

    return router
