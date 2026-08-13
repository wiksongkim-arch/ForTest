"""Generation API with immutable task snapshots and bounded ownership."""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Literal, Mapping

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.ai.base import AIProvider, ProviderUnavailableError
from backend.ai.images import ImageWorkspace
from backend.ai.registry import ProviderDecision, ProviderRegistry
from backend.ai.types import ProviderUsage
from backend.api.settings_routes import (
    RuntimeDependencies,
    get_runtime_dependencies,
    get_settings_service,
)
from backend.core.generator import GenerationCancelledError, TestCaseGenerator
from backend.security.redaction import redact_log_text, redact_text
from backend.settings.models import ProviderName, SettingsSnapshot
from backend.settings.service import SettingsService
from services.dingtalk_mcp import DingTalkMCPService, extract_node_id
from services.dingtalk_output import DingTalkOutputWriter
from services.dingtalk_output import LocalTemplateOutputWriter
from services.dingtalk_spreadsheet import DingTalkSpreadSheetMCPService
from services.requirement_documents import (
    DingTalkRequirementDocumentReader,
    LocalRequirementDocumentReader,
    RequirementDocumentGateway,
    RequirementDocumentSource,
)
from utils.default_templates import CONTENT_TEMPLATE, OUTPUT_TEMPLATE
from utils.template_loader import TemplateLoader


TaskStatus = Literal[
    "pending",
    "running",
    "completed",
    "partial_failure",
    "failed",
    "stopped",
]


router = APIRouter(prefix="/api", tags=["测试用例生成"])


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # doc_url 保留给旧 API；新客户端使用来源类型和统一地址字段。
    doc_url: str = ""
    source_type: Literal["link", "file"] = "link"
    document_source: str = ""

    @model_validator(mode="after")
    def validate_document_source(self) -> "GenerateRequest":
        legacy = str(self.doc_url or "").strip()
        current = str(self.document_source or "").strip()
        if legacy and current and legacy != current:
            raise ValueError("需求文档地址字段不一致")
        source = RequirementDocumentSource.create(
            self.source_type,
            current or legacy,
        )
        self.source_type = source.source_type
        self.document_source = source.location
        self.doc_url = source.location if source.source_type == "link" else ""
        return self

    def source_reference(self) -> RequirementDocumentSource:
        return RequirementDocumentSource(
            self.source_type,
            self.document_source,
        )


class RecoverOutputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1, max_length=256)
    expected_case_count: int = Field(ge=1, le=3_000)


class GenerateResponse(BaseModel):
    success: bool
    partial_failure: bool = False
    dingtalk_doc_url: str | None = None
    node_id: str | None = None
    output_file_path: str | None = None
    test_cases_count: int = 0
    provider_usage: dict[str, Any] | None = None
    logs: list[str] = Field(default_factory=list)
    error: str | None = None


class HealthResponse(BaseModel):
    status: Literal["healthy"]
    provider: ProviderName
    runtime: Literal["auto", "sdk", "cli", "http"]


class GenerationConfigurationError(ValueError):
    """生成任务缺少必需依赖时使用的配置异常。"""


@dataclass(frozen=True)
class GenerationResult:
    success: bool
    partial_failure: bool
    dingtalk_doc_url: str | None
    node_id: str | None
    output_file_path: str | None
    test_cases_count: int
    provider_usage: ProviderUsage
    logs: tuple[str, ...]
    error: str | None = None

    def public_view(self) -> dict[str, Any]:
        usage = asdict(self.provider_usage)
        usage["provider"] = self.provider_usage.provider.value
        return {
            "success": self.success,
            "partial_failure": self.partial_failure,
            "dingtalk_doc_url": self.dingtalk_doc_url,
            "node_id": self.node_id,
            "output_file_path": self.output_file_path,
            "test_cases_count": self.test_cases_count,
            "provider_usage": usage,
            "logs": [redact_log_text(item) for item in self.logs],
            "error": redact_log_text(self.error) if self.error else None,
        }


@dataclass
class GenerationTask:
    task_id: str
    snapshot: SettingsSnapshot | None = field(repr=False)
    provider_decision: ProviderDecision = field(repr=False)
    status: TaskStatus = "pending"
    task_name: str | None = None
    result: GenerationResult | None = None
    logs: list[str] = field(default_factory=list)
    error: str | None = None
    last_log_index: int = 0
    current_block: int = 0
    completed_block: int = 0
    total_blocks: int = 0
    cancellation_event: threading.Event = field(
        default_factory=threading.Event,
        repr=False,
    )
    cancel_callback: Callable[[], None] | None = field(
        default=None,
        repr=False,
    )


_generation_tasks: dict[str, GenerationTask] = {}
_tasks_lock = threading.Lock()


def build_generator(
    snapshot: SettingsSnapshot,
    provider: AIProvider,
    *,
    source: RequirementDocumentSource | None = None,
    default_template_paths: Mapping[str, Any] | None = None,
    document_factory=None,
    spreadsheet_factory=None,
) -> TestCaseGenerator:
    source = source or RequirementDocumentSource("link", "")
    template_paths = dict(default_template_paths or {})
    document_factory = document_factory or DingTalkMCPService
    spreadsheet_factory = spreadsheet_factory or DingTalkSpreadSheetMCPService
    document_mcp_url = snapshot.secrets.reveal("document_mcp_url")
    spreadsheet_mcp_url = snapshot.secrets.reveal("spreadsheet_mcp_url")
    document_settings = snapshot.settings.document
    content_template_url = str(document_settings.content_template_url or "").strip()
    document_template_url = str(document_settings.document_template_url or "").strip()
    output_folder_url = str(document_settings.output_folder_url or "").strip()

    needs_document_mcp = source.source_type == "link" or bool(
        document_template_url
    )
    needs_spreadsheet_mcp = bool(content_template_url or document_template_url)
    if needs_document_mcp and (
        not document_mcp_url or not document_mcp_url.strip()
    ):
        raise GenerationConfigurationError("钉钉文档 MCP 尚未配置")
    if needs_spreadsheet_mcp and (
        not spreadsheet_mcp_url or not spreadsheet_mcp_url.strip()
    ):
        raise GenerationConfigurationError("钉钉表格 MCP 尚未配置")
    if document_template_url and not output_folder_url:
        raise GenerationConfigurationError("文档配置尚未完成：输出文件夹")

    content_template_path = template_paths.get(CONTENT_TEMPLATE)
    output_template_path = template_paths.get(OUTPUT_TEMPLATE)
    if not content_template_url and not content_template_path:
        raise GenerationConfigurationError("本地用例模板不可用")
    if not document_template_url and not output_template_path:
        raise GenerationConfigurationError("本地输出模板不可用")

    document_service = (
        document_factory(str(document_mcp_url)) if needs_document_mcp else None
    )
    spreadsheet_service = (
        spreadsheet_factory(str(spreadsheet_mcp_url))
        if needs_spreadsheet_mcp
        else None
    )
    if source.source_type == "file":
        requirement_reader = LocalRequirementDocumentReader()
    else:
        if document_service is None:  # pragma: no cover - 前置校验已覆盖
            raise GenerationConfigurationError("钉钉文档 MCP 尚未配置")
        requirement_reader = DingTalkRequirementDocumentReader(document_service)
    requirement_gateway = RequirementDocumentGateway(
        {source.source_type: requirement_reader}
    )
    template_loader = TemplateLoader(
        content_template_url,
        spreadsheet_service,
        local_template_path=content_template_path,
    )
    if document_template_url:
        if document_service is None or spreadsheet_service is None:
            raise GenerationConfigurationError("钉钉输出 MCP 尚未配置")
        output_writer = DingTalkOutputWriter(
            document_service=document_service,
            spreadsheet_service=spreadsheet_service,
            document_template_url=document_template_url,
            output_folder_url=output_folder_url,
        )
    else:
        output_writer = LocalTemplateOutputWriter(output_template_path)
    image_workspace = ImageWorkspace()
    try:
        return TestCaseGenerator(
            snapshot=snapshot,
            provider=provider,
            template_loader=template_loader,
            document_service=document_service,
            image_workspace=image_workspace,
            output_writer=output_writer,
            requirement_reader=requirement_gateway,
        )
    except Exception:
        _safe_close(image_workspace)
        raise


@router.get("/health", response_model=HealthResponse)
async def health_check(
    service: SettingsService = Depends(get_settings_service),
) -> HealthResponse:
    ai = service.load().ai
    runtime = (
        ai.codex.runtime.value
        if ai.active_provider == ProviderName.codex
        else "http"
    )
    return HealthResponse(
        status="healthy",
        provider=ai.active_provider,
        runtime=runtime,
    )


@router.post("/generate", response_model=GenerateResponse)
def generate_test_cases(
    request: GenerateRequest,
    dependencies: RuntimeDependencies = Depends(get_runtime_dependencies),
) -> GenerateResponse:
    source = request.source_reference()
    snapshot, provider, _decision, generator = _construct_task(
        dependencies,
        source,
    )
    del snapshot
    try:
        payload = generator.generate(source)
        result = _result_from_payload(payload, provider.name)
        return GenerateResponse.model_validate(result.public_view())
    finally:
        _close_generator_and_provider(generator, provider)


@router.get("/logs")
async def get_logs() -> dict[str, list[str]]:
    return {"logs": []}


def _sse_data(value: object) -> str:
    """Encode one bounded, injection-safe Server-Sent Event data record."""

    return f"data: {redact_log_text(value)}\n\n"


@router.get("/stream-logs/{task_id}")
async def stream_logs(task_id: str) -> StreamingResponse:
    def event_generator():
        local_index = 0
        while True:
            with _tasks_lock:
                task = _generation_tasks.get(task_id)
                if task is None:
                    snapshot = None
                else:
                    snapshot = (
                        task.status,
                        tuple(task.logs),
                        task.result,
                        task.error,
                    )
            if snapshot is None:
                yield _sse_data("任务不存在")
                break
            status, logs, result, error = snapshot
            while local_index < len(logs):
                yield _sse_data(logs[local_index])
                local_index += 1
            if status in {"completed", "partial_failure"}:
                count = result.test_cases_count if result is not None else 0
                yield _sse_data(f"[DONE] {count} 条测试用例已生成")
                yield _sse_data("[END]")
                break
            if status == "failed":
                yield _sse_data(f"[ERROR] {error or '生成失败'}")
                yield _sse_data("[END]")
                break
            if status == "stopped":
                yield _sse_data("[STOPPED] 任务已停止")
                yield _sse_data("[END]")
                break
            time.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.post("/start-generate")
def start_generate(
    request: GenerateRequest,
    dependencies: RuntimeDependencies = Depends(get_runtime_dependencies),
) -> dict[str, str]:
    source = request.source_reference()
    snapshot, provider, decision, generator = _construct_task(
        dependencies,
        source,
    )
    task_id = str(uuid.uuid4())
    task = GenerationTask(
        task_id=task_id,
        snapshot=snapshot,
        provider_decision=decision,
    )
    with _tasks_lock:
        _generation_tasks[task_id] = task

    def request_cancel() -> None:
        """只中止当前任务拥有的 provider，避免误伤其它桌面或网页任务。"""

        task.cancellation_event.set()
        cancel = getattr(provider, "cancel", None)
        if callable(cancel):
            cancel()

    task.cancel_callback = request_cancel

    def progress_callback(message: str) -> None:
        safe_message = redact_log_text(message)
        with _tasks_lock:
            current = _generation_tasks.get(task_id)
            if current is None or current.cancellation_event.is_set():
                return
            current.logs.append(safe_message)
            match = re.search(r"\[(\d+)/(\d+)\]", safe_message)
            if match:
                current.current_block = int(match.group(1))
                current.total_blocks = int(match.group(2))

    def document_name_callback(name: str) -> None:
        # 文档名称仅作为展示元数据写入任务快照，不保存凭据或原始链接。
        safe_name = redact_text(name).strip()
        if not safe_name:
            return
        with _tasks_lock:
            current = _generation_tasks.get(task_id)
            if current is not None and not current.cancellation_event.is_set():
                current.task_name = safe_name

    def completed_block_callback(completed: int, total: int) -> None:
        # current_block 保留“当前开始处理区块”的旧语义，完成数单独维护。
        normalized_total = max(0, int(total))
        normalized_completed = min(
            normalized_total,
            max(0, int(completed)),
        )
        with _tasks_lock:
            current = _generation_tasks.get(task_id)
            if current is not None and not current.cancellation_event.is_set():
                current.completed_block = max(
                    current.completed_block,
                    normalized_completed,
                )
                current.total_blocks = max(
                    current.total_blocks,
                    normalized_total,
                )

    def run_generation() -> None:
        with _tasks_lock:
            current = _generation_tasks.get(task_id)
            if current is not None and not current.cancellation_event.is_set():
                current.status = "running"
        result: GenerationResult | None = None
        status: TaskStatus = "failed"
        failure_detail: str | None = None
        try:
            if task.cancellation_event.is_set():
                raise GenerationCancelledError("任务已停止")
            payload = generator.generate(
                source,
                progress_callback=progress_callback,
                document_name_callback=document_name_callback,
                completed_block_callback=completed_block_callback,
                cancellation_check=task.cancellation_event.is_set,
            )
            result = _result_from_payload(payload, provider.name)
            if result.partial_failure:
                status = "partial_failure"
            elif result.success:
                status = "completed"
            else:
                status = "failed"
        except GenerationCancelledError:
            status = "stopped"
            failure_detail = None
        except Exception as exc:
            if task.cancellation_event.is_set():
                status = "stopped"
                failure_detail = None
            else:
                failure_detail = f"生成失败（{type(exc).__name__}）"
        finally:
            _close_generator_and_provider(generator, provider)
            with _tasks_lock:
                current = _generation_tasks.get(task_id)
                if current is not None:
                    current.snapshot = None
                    current.cancel_callback = None
                    if current.cancellation_event.is_set():
                        status = "stopped"
                    current.result = result
                    if status == "stopped":
                        current.error = None
                        if not current.logs or current.logs[-1] != "任务已停止":
                            current.logs.append("任务已停止")
                    elif result is not None:
                        current.logs = [
                            redact_log_text(item) for item in result.logs
                        ]
                        current.error = (
                            redact_log_text(result.error)
                            if result.error
                            else None
                        )
                    else:
                        current.error = failure_detail or "生成失败"
                        current.logs.append(current.error)
                    current.status = status

    worker = threading.Thread(
        target=run_generation,
        name=f"prd-to-case-{task_id}",
        daemon=True,
    )
    try:
        worker.start()
    except Exception as exc:
        _close_generator_and_provider(generator, provider)
        with _tasks_lock:
            _generation_tasks.pop(task_id, None)
        raise HTTPException(
            status_code=500,
            detail=f"任务启动失败（{type(exc).__name__}）",
        ) from None
    return {"task_id": task_id}


@router.post("/stop-task/{task_id}")
def stop_generation_task(task_id: str) -> dict[str, Any]:
    """请求停止单个生成任务；回调在锁外执行，避免阻塞其它状态查询。"""

    callback: Callable[[], None] | None = None
    with _tasks_lock:
        task = _generation_tasks.get(task_id)
        if task is None:
            return {"stopped": False, "status": "not_found"}
        if task.status not in {"pending", "running"}:
            return {"stopped": False, "status": task.status}
        task.cancellation_event.set()
        task.status = "stopped"
        task.error = None
        if not task.logs or task.logs[-1] != "任务已停止":
            task.logs.append("任务已停止")
        callback = task.cancel_callback
    if callback is not None:
        try:
            callback()
        except Exception:
            # 中止请求已经落到任务状态；底层关闭失败由生成线程最终收敛。
            pass
    return {"stopped": True, "status": "stopped"}


@router.get("/task-status/{task_id}")
def get_task_status(task_id: str) -> dict[str, Any]:
    with _tasks_lock:
        task = _generation_tasks.get(task_id)
        if task is None:
            return {"status": "not_found", "result": None}
        result = task.result.public_view() if task.result else None
        return {
            "status": task.status,
            "task_name": task.task_name,
            "result": result,
            "logs": [redact_log_text(item) for item in task.logs],
            "error": redact_log_text(task.error) if task.error else None,
            "current_block": task.current_block,
            "completed_block": task.completed_block,
            "total_blocks": task.total_blocks,
        }


def discard_terminal_task(task_id: str) -> bool:
    """释放已结束任务的内存快照；进行中或不存在的任务保持不变。"""

    with _tasks_lock:
        task = _generation_tasks.get(task_id)
        if task is None or task.status not in {
            "completed",
            "partial_failure",
            "failed",
            "stopped",
        }:
            return False
        _generation_tasks.pop(task_id, None)
        return True


@router.post("/recover-output")
def recover_output(
    request: RecoverOutputRequest,
    dependencies: RuntimeDependencies = Depends(get_runtime_dependencies),
) -> dict[str, Any]:
    snapshot = dependencies.service.snapshot()
    document_mcp_url = snapshot.secrets.reveal("document_mcp_url")
    spreadsheet_mcp_url = snapshot.secrets.reveal("spreadsheet_mcp_url")
    if not document_mcp_url or not spreadsheet_mcp_url:
        raise HTTPException(status_code=422, detail="钉钉 MCP 配置不完整")
    if not (
        snapshot.settings.document.document_template_url
        and snapshot.settings.document.output_folder_url
    ):
        raise HTTPException(status_code=422, detail="钉钉输出模板配置不完整")
    writer = DingTalkOutputWriter(
        document_service=DingTalkMCPService(document_mcp_url),
        spreadsheet_service=DingTalkSpreadSheetMCPService(spreadsheet_mcp_url),
        document_template_url=snapshot.settings.document.document_template_url,
        output_folder_url=snapshot.settings.document.output_folder_url,
    )
    try:
        node_id = extract_node_id(request.node_id)
        output = writer.recover_existing(
            node_id,
            request.expected_case_count,
            snapshot.settings.document.local_output_dir,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"钉钉已有输出重新验收失败（{type(exc).__name__}）",
        ) from None
    return {
        "status": "partial_failure" if output.partial_failure else "completed",
        "result": {
            "success": not output.partial_failure,
            "partial_failure": output.partial_failure,
            "dingtalk_doc_url": output.dingtalk_doc_url,
            "node_id": output.node_id,
            "output_file_path": output.output_file_path,
            "test_cases_count": request.expected_case_count,
            "error": output.local_error,
        },
        "logs": ["钉钉已有输出重新验收完成"],
        "current_block": 0,
        "completed_block": 0,
        "total_blocks": 0,
    }


@router.post("/retry-output-verification/{task_id}")
def retry_output_verification(
    task_id: str,
    dependencies: RuntimeDependencies = Depends(get_runtime_dependencies),
) -> dict[str, Any]:
    with _tasks_lock:
        task = _generation_tasks.get(task_id)
        if task is None or task.result is None:
            raise HTTPException(status_code=404, detail="生成任务不存在")
        if task.status != "failed":
            raise HTTPException(status_code=409, detail="仅失败任务可重新验收")
        original = task.result
    if not original.node_id or not original.dingtalk_doc_url:
        raise HTTPException(status_code=409, detail="失败任务没有可重新验收的钉钉输出")

    snapshot = dependencies.service.snapshot()
    document_mcp_url = snapshot.secrets.reveal("document_mcp_url")
    spreadsheet_mcp_url = snapshot.secrets.reveal("spreadsheet_mcp_url")
    if not document_mcp_url or not spreadsheet_mcp_url:
        raise HTTPException(status_code=422, detail="钉钉 MCP 配置不完整")
    if not (
        snapshot.settings.document.document_template_url
        and snapshot.settings.document.output_folder_url
    ):
        raise HTTPException(status_code=422, detail="钉钉输出模板配置不完整")
    writer = DingTalkOutputWriter(
        document_service=DingTalkMCPService(document_mcp_url),
        spreadsheet_service=DingTalkSpreadSheetMCPService(spreadsheet_mcp_url),
        document_template_url=snapshot.settings.document.document_template_url,
        output_folder_url=snapshot.settings.document.output_folder_url,
    )
    try:
        output = writer.recover_existing(
            original.node_id,
            original.test_cases_count,
            snapshot.settings.document.local_output_dir,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"钉钉已有输出重新验收失败（{type(exc).__name__}）",
        ) from None

    fallback_count = original.provider_usage.fallback_count
    partial = bool(output.partial_failure or fallback_count)
    if output.partial_failure:
        error = output.local_error
    elif fallback_count:
        error = f"{fallback_count} 个区块使用了兜底逻辑"
    else:
        error = None
    recovered = GenerationResult(
        success=not partial,
        partial_failure=partial,
        dingtalk_doc_url=output.dingtalk_doc_url,
        node_id=output.node_id,
        output_file_path=output.output_file_path,
        test_cases_count=original.test_cases_count,
        provider_usage=original.provider_usage,
        logs=(*original.logs, "钉钉已有输出重新验收完成"),
        error=error,
    )
    with _tasks_lock:
        current = _generation_tasks.get(task_id)
        if current is None or current.result is not original:
            raise HTTPException(status_code=409, detail="任务状态已变化，请刷新页面")
        current.result = recovered
        current.logs = list(recovered.logs)
        current.error = recovered.error
        current.status = "partial_failure" if partial else "completed"
        return {
            "status": current.status,
            "result": recovered.public_view(),
            "logs": [redact_log_text(item) for item in current.logs],
            "current_block": current.current_block,
            "completed_block": current.completed_block,
            "total_blocks": current.total_blocks,
        }


def _construct_task(
    dependencies: RuntimeDependencies,
    source: RequirementDocumentSource,
) -> tuple[
    SettingsSnapshot,
    AIProvider,
    ProviderDecision,
    TestCaseGenerator,
]:
    provider: AIProvider | None = None
    generator: TestCaseGenerator | None = None
    try:
        snapshot = dependencies.service.snapshot()
        provider, decision = dependencies.registry.create_for_task(snapshot)
        generator = build_generator(
            snapshot,
            provider,
            source=source,
            default_template_paths=dependencies.default_template_paths,
            document_factory=dependencies.document_factory,
            spreadsheet_factory=dependencies.spreadsheet_factory,
        )
        return snapshot, provider, decision, generator
    except GenerationConfigurationError as exc:
        if generator is not None:
            _safe_close(generator)
        if provider is not None:
            _safe_close(provider)
        raise HTTPException(status_code=422, detail=redact_text(exc)) from None
    except ProviderUnavailableError:
        if generator is not None:
            _safe_close(generator)
        if provider is not None:
            _safe_close(provider)
        raise HTTPException(status_code=502, detail="AI Provider 不可用") from None
    except HTTPException:
        if generator is not None:
            _safe_close(generator)
        if provider is not None:
            _safe_close(provider)
        raise
    except Exception as exc:
        if generator is not None:
            _safe_close(generator)
        if provider is not None:
            _safe_close(provider)
        raise HTTPException(
            status_code=500,
            detail=f"生成依赖构造失败（{type(exc).__name__}）",
        ) from None


def _result_from_payload(
    payload: object,
    provider_name: ProviderName,
) -> GenerationResult:
    if not isinstance(payload, dict):
        raise RuntimeError("生成结果格式无效")
    usage_payload = payload.get("provider_usage")
    if not isinstance(usage_payload, dict):
        raise RuntimeError("生成结果缺少 Provider 用量")
    try:
        usage = ProviderUsage(
            provider=ProviderName(usage_payload["provider"]),
            runtime_mode=str(usage_payload["runtime_mode"]),
            model=str(usage_payload["model"]),
            total_sections=max(0, int(usage_payload["total_sections"])),
            ai_case_count=max(0, int(usage_payload["ai_case_count"])),
            fallback_count=max(0, int(usage_payload["fallback_count"])),
            duration_ms=max(0, int(usage_payload["duration_ms"])),
            retry_count=max(0, int(usage_payload["retry_count"])),
        )
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("生成结果 Provider 用量无效") from None
    if usage.provider != provider_name:
        raise RuntimeError("生成结果 Provider 身份不匹配")
    logs_value = payload.get("logs", [])
    if not isinstance(logs_value, list):
        logs_value = []
    logs = tuple(redact_log_text(item) for item in logs_value)
    error = payload.get("error")
    return GenerationResult(
        success=bool(payload.get("success")),
        partial_failure=bool(payload.get("partial_failure")),
        dingtalk_doc_url=_optional_string(payload.get("dingtalk_doc_url")),
        node_id=_optional_string(payload.get("node_id")),
        output_file_path=_optional_string(payload.get("output_file_path")),
        test_cases_count=max(0, int(payload.get("test_cases_count", 0))),
        provider_usage=usage,
        logs=logs,
        error=redact_log_text(error) if error else None,
    )


def _close_generator_and_provider(
    generator: TestCaseGenerator,
    provider: AIProvider,
) -> None:
    # These resources have separate ownership and must close independently.
    _safe_close(generator)
    _safe_close(provider)


def _safe_close(resource: object) -> None:
    try:
        close = getattr(resource, "close")
        close()
    except Exception:
        pass


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
