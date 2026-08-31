from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Annotated, Any

import ai
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import agent as agent_module
from .domain import ConflictResolution, HumanDecisionResponse, RunRecord
from .model_config import ModelConnectionInput, ModelConnectionRepository
from .model_runtime import ModelRuntimeFactory, safe_provider_error
from .persistence import ConcurrentRunUpdate, RunRepository
from .service import RunService


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VAR_ROOT = BACKEND_ROOT / "var"
repository = RunRepository(VAR_ROOT / "merge-agent.sqlite3")
run_service = RunService(repository, VAR_ROOT / "runs")
agent_module.configure_agent(run_service)
model_connections = ModelConnectionRepository()
model_factory = ModelRuntimeFactory(model_connections)


app = FastAPI(title="Excel Merge Agent", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ConcurrentRunUpdate)
async def concurrent_update_handler(_, exc: ConcurrentRunUpdate) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


def _not_found(run_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Run {run_id!r} was not found")


def _get_run(run_id: str) -> RunRecord:
    try:
        return run_service.get(run_id)
    except KeyError as exc:
        raise _not_found(run_id) from exc


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "models": model_connections.summaries(),
    }


@app.post("/api/model/probe")
async def probe_model() -> dict[str, Any]:
    try:
        return await model_factory.probe()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={"message": safe_provider_error(exc), "error": type(exc).__name__},
        ) from exc


@app.get("/api/model-connections")
async def list_model_connections() -> dict[str, Any]:
    return model_connections.summaries()


@app.put("/api/model-connections/{profile_id}")
async def save_model_connection(
    profile_id: str, request: ModelConnectionInput
) -> dict[str, Any]:
    try:
        model_connections.save(profile_id, request)
        return model_connections.summaries()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/model-connections/{profile_id}/activate")
async def activate_model_connection(profile_id: str) -> dict[str, Any]:
    try:
        model_connections.set_default(profile_id)
        return model_connections.summaries()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/model-connections/{profile_id}/probe")
async def probe_model_connection(profile_id: str) -> dict[str, Any]:
    try:
        return await model_factory.probe(profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={"message": safe_provider_error(exc), "error": type(exc).__name__},
        ) from exc


@app.get("/api/runs", response_model=list[RunRecord])
async def list_runs() -> list[RunRecord]:
    return run_service.list()


class CreateRunRequest(BaseModel):
    model_profile_id: str | None = None


@app.post("/api/runs", response_model=RunRecord)
async def create_run(request: CreateRunRequest | None = None) -> RunRecord:
    profile_id = request.model_profile_id if request else None
    try:
        registry = model_connections.registry()
        selected = profile_id or registry.default_profile
        registry.profile(selected)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return run_service.create(selected)


@app.get("/api/runs/{run_id}", response_model=RunRecord)
async def get_run(run_id: str) -> RunRecord:
    return _get_run(run_id)


class BatchSettingsRequest(BaseModel):
    batch_size: int = Field(ge=1, le=500)


@app.put("/api/runs/{run_id}/batch-settings", response_model=RunRecord)
async def update_batch_settings(
    run_id: str, request: BatchSettingsRequest
) -> RunRecord:
    _get_run(run_id)
    try:
        return run_service.configure_batch(run_id, request.batch_size)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/files", response_model=RunRecord)
async def upload_files(
    run_id: str,
    template: Annotated[UploadFile, File(...)],
    sources: Annotated[list[UploadFile], File(...)],
) -> RunRecord:
    _get_run(run_id)
    try:
        return await run_service.save_files(run_id, template, sources)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/inspect", response_model=RunRecord)
async def inspect_run(run_id: str) -> RunRecord:
    _get_run(run_id)
    try:
        run = run_service.prepare_inspection(run_id)
        if run.state.value == "failed":
            raise ValueError(run.error or "Workbook inspection failed")
        evidence = run_service.planning_evidence(run_id)
        if run.template is None:
            raise ValueError("Template is unavailable after inspection")
        runtime = model_factory.create(run.model_profile_id)
        try:
            spec, provenance = await runtime.plan_merge(
                run.template.stored_path,
                evidence,
            )
        finally:
            await runtime.close()
        return run_service.accept_plan(run_id, spec, provenance)
    except ValueError as exc:
        run_service.fail_planning(run_id, exc)
        raise HTTPException(status_code=502, detail=f"Model planning failed: {exc}") from exc
    except Exception as exc:
        run_service.fail_planning(run_id, exc)
        raise HTTPException(
            status_code=502,
            detail=f"Configured model could not create a valid merge plan ({type(exc).__name__})",
        ) from exc


class ApprovalRequest(BaseModel):
    spec_hash: str


@app.post("/api/runs/{run_id}/approve", response_model=RunRecord)
async def approve_run(run_id: str, request: ApprovalRequest) -> RunRecord:
    _get_run(run_id)
    try:
        return run_service.approve(run_id, request.spec_hash)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post(
    "/api/runs/{run_id}/conflicts/{conflict_id}/resolve",
    response_model=RunRecord,
)
async def resolve_conflict(
    run_id: str,
    conflict_id: str,
    resolution: ConflictResolution,
) -> RunRecord:
    _get_run(run_id)
    try:
        return run_service.resolve_conflict(run_id, conflict_id, resolution)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Conflict not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/execute", response_model=RunRecord)
async def execute_run(run_id: str) -> RunRecord:
    _get_run(run_id)
    try:
        return await asyncio.to_thread(run_service.execute, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Deterministic workbook execution failed ({type(exc).__name__})",
        ) from exc


@app.post(
    "/api/runs/{run_id}/decisions/{decision_id}/resolve",
    response_model=RunRecord,
)
async def resolve_runtime_decision(
    run_id: str,
    decision_id: str,
    response: HumanDecisionResponse,
) -> RunRecord:
    _get_run(run_id)
    try:
        return run_service.resolve_decision(run_id, decision_id, response)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Runtime question not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/runs/{run_id}/output")
async def download_output(run_id: str) -> FileResponse:
    run = _get_run(run_id)
    if not run.output_path or not Path(run.output_path).exists():
        raise HTTPException(status_code=404, detail="Output is not available")
    return FileResponse(
        run.output_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="merged.xlsx",
    )


@app.get("/api/runs/{run_id}/audit")
async def download_audit(run_id: str) -> FileResponse:
    run = _get_run(run_id)
    if not run.audit_path or not Path(run.audit_path).exists():
        raise HTTPException(status_code=404, detail="Audit report is not available")
    return FileResponse(run.audit_path, media_type="application/json", filename="audit.json")


@app.post("/api/runs/{run_id}/explain")
async def explain_plan(run_id: str) -> dict[str, str]:
    run = _get_run(run_id)
    if run.spec is None:
        raise HTTPException(status_code=409, detail="Create a plan first")
    unresolved = [conflict.message for conflict in run.conflicts if not conflict.resolved]
    summary = json.dumps(
        {
            "run_state": run.state.value,
            "operations": [item.model_dump() for item in run.spec.operations],
            "unresolved_conflicts": unresolved,
        },
        ensure_ascii=False,
    )
    try:
        runtime = model_factory.create(run.model_profile_id)
        try:
            return {"explanation": await runtime.explain_plan(summary)}
        finally:
            await runtime.close()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={"message": "Configured model could not explain the plan", "error": type(exc).__name__},
        ) from exc


class ChatRequest(BaseModel):
    messages: list[ai.ui.ai_sdk.UIMessage]


class ConversationSnapshot(BaseModel):
    messages: list[ai.ui.ai_sdk.UIMessage]


def _conversation_payload(
    messages: list[ai.ui.ai_sdk.UIMessage],
) -> list[dict[str, object]]:
    return [
        message.model_dump(by_alias=True, exclude_none=True)
        for message in messages
    ]


@app.put("/api/runs/{run_id}/conversation", response_model=RunRecord)
async def save_conversation(
    run_id: str,
    request: ConversationSnapshot,
) -> RunRecord:
    _get_run(run_id)
    return run_service.save_conversation(
        run_id,
        _conversation_payload(request.messages),
    )


@app.post("/api/runs/{run_id}/chat")
async def chat(run_id: str, request: ChatRequest) -> StreamingResponse:
    _get_run(run_id)
    run_service.save_conversation(
        run_id,
        _conversation_payload(request.messages),
    )
    messages, approvals = ai.ui.ai_sdk.to_messages(request.messages)
    messages.insert(
        0,
        ai.system_message(
            "You are the Excel Merge Agent in a task-oriented chat interface. Be concise. "
            "Use read-only tools without asking permission and inspect get_run_summary before answering about a run. "
            "Attempt safe, bounded, deterministic recovery before involving the user. Ask one focused question only "
            "when the answer requires business knowledge unavailable in the files, configuration, prior answers, or runtime evidence. "
            "Do not ask for confirmation in prose. When the run has no pending user-information question and the user asks to run it, "
            "call execute_approved_merge directly; its approval hook is the single authorization immediately before any workbook or audit write. "
            "There is no separate plan-approval button. awaiting_write_approval means you must call the write tool, not tell the user to approve elsewhere. "
            "Ignore earlier conversation messages that describe the removed two-step approval workflow. "
            "Never invent business meaning, exclude a source, or change the reviewed plan silently. "
            f"The active run id is {run_id}."
        ),
    )

    async def stream_response() -> AsyncGenerator[str]:
        runtime = model_factory.create(_get_run(run_id).model_profile_id)
        try:
            async with agent_module.chat_agent.run(
                runtime.model, messages, params=runtime.request_params()
            ) as result:
                ai.ui.ai_sdk.apply_approvals(approvals)

                async def process() -> AsyncGenerator[ai.events.AgentEvent]:
                    async for event in result:
                        if isinstance(event, ai.events.HookEvent) and event.hook.status == "pending":
                            ai.defer_hook(event.hook)
                        yield event

                async for chunk in ai.ui.ai_sdk.to_sse(process()):
                    yield chunk
        finally:
            await runtime.close()

    return StreamingResponse(
        stream_response(),
        headers=ai.ui.ai_sdk.UI_MESSAGE_STREAM_HEADERS,
    )
