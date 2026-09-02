"""EIM 受控 AI 工具循环、确定性门禁和不可变版本发布。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from backend.eim.builder.action_schema import ACTIONS
from backend.eim.builder.compiler import compile_dsl, dsl_from_text, dsl_to_text, source_fields
from backend.eim.builder.model_adapter import EIMBuilderModelAdapter
from backend.eim.builder.simulator import run_samples, simulate
from backend.eim.connections.dingtalk import DingTalkConnector
from backend.eim.destinations.dingtalk import destination_adapter
from backend.eim.models import (
    BuildState,
    ConnectionState,
    EIMDSL,
    EIMTaskVersion,
    ensure_editable,
    new_ulid,
    utc_now,
)
from backend.eim.redaction import redact_structure, sanitize_payload
from backend.eim.repository import EIMRepository
from backend.security.redaction import redact_text


class EIMBuilder:
    def __init__(
        self,
        repository: EIMRepository,
        connector: DingTalkConnector,
        runtime: Any,
        data_root: Path,
        *,
        settings_service: Any = None,
        codex_path_resolver: Any = None,
        client_factory: Any = None,
        model_runner: Callable[[str, str], tuple[dict[str, Any], Any]] | None = None,
        start_callback: Callable[[str], None] | None = None,
    ):
        self.repository = repository
        self.connector = connector
        self.runtime = runtime
        self.data_root = Path(data_root).resolve()
        self.settings_service = settings_service
        self.codex_path_resolver = codex_path_resolver
        self.client_factory = client_factory
        self.model_runner = model_runner
        self.start_callback = start_callback
        self._locks: dict[str, threading.Lock] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._models: dict[str, EIMBuilderModelAdapter] = {}
        self._guard = threading.RLock()

    def load_draft(self, task_id: str) -> EIMDSL:
        path = self._draft_path(task_id)
        if path.is_file():
            return dsl_from_text(path.read_text(encoding="utf-8"))
        task = self.repository.get_task(task_id)
        if task and task.active_version_id:
            version = self.repository.get_version(task.active_version_id)
            if version:
                return EIMDSL.model_validate(version.dsl)
        return EIMDSL()

    def save_draft(self, task_id: str, dsl: EIMDSL) -> Path:
        task = self.repository.get_task(task_id)
        if task is None:
            raise KeyError(f"EIM 任务不存在：{task_id}")
        path = self._draft_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix=".dsl.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(dsl_to_text(dsl) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, path)
        finally:
            Path(temporary_name).unlink(missing_ok=True)
        return path

    def detect_configuration(self, configuration_id: str) -> dict[str, Any]:
        adapter = self._model_adapter()
        try:
            result, evidence = adapter.run_action(
                configuration_id,
                '能力检测：请只返回 finish 动作，arguments 为字符串 "{}"，message 简述已就绪。',
            )
            compatible = result.get("action") in ACTIONS and isinstance(result.get("arguments"), dict)
            detail = (
                f"EIM 结构化动作兼容 · {getattr(evidence, 'model', '')}"
                if compatible
                else "模型未返回合法 EIM 动作"
            )
        except Exception as exc:
            compatible = False
            detail = f"EIM 能力检测失败：{redact_text(exc) or type(exc).__name__}"
        finally:
            adapter.close()
        self.repository.save_ai_configuration_health(
            configuration_id,
            compatible=compatible,
            detail=detail,
        )
        return {"configuration_id": configuration_id, "compatible": compatible, "detail": detail}

    def build(
        self,
        task_id: str,
        *,
        configuration_id: str | None = None,
        instruction: str = "",
        start_after: bool = False,
    ) -> dict[str, Any]:
        lock = self._task_lock(task_id)
        if not lock.acquire(blocking=False):
            raise ValueError("该 EIM 任务正在构建")
        cancel = threading.Event()
        with self._guard:
            self._cancel[task_id] = cancel
        session_id: str | None = None
        steps = 0
        usage = {"input_tokens": 0, "output_tokens": 0, "duration_ms": 0}
        staged_samples: list[dict[str, Any]] = []
        model: EIMBuilderModelAdapter | None = None
        try:
            task = self.repository.get_task(task_id)
            if task is None:
                raise KeyError(f"EIM 任务不存在：{task_id}")
            ensure_editable(task)
            revision = task.draft_revision
            self.repository.transition_build(task_id, BuildState.BUILDING)
            dsl = self.load_draft(task_id)
            if configuration_id:
                session_id = self.repository.start_ai_session(task_id, configuration_id)
                if self.model_runner is None:
                    model = self._model_adapter()
                    with self._guard:
                        self._models[task_id] = model
                    runner = model.run_action
                else:
                    runner = self.model_runner
                dsl, steps, usage, staged_samples = self._tool_loop(
                    task_id,
                    configuration_id,
                    dsl,
                    instruction,
                    runner,
                    cancel,
                )
            # AI 修改先落到草稿；只有后续全部门禁通过才会切换活动版本。
            self.save_draft(task_id, dsl)
            result = self._validate_publish(
                task_id,
                revision,
                dsl,
                configuration_id,
                usage,
                staged_samples,
            )
            if session_id:
                self.repository.update_ai_session(
                    session_id,
                    state="completed",
                    steps=steps,
                    usage=usage,
                    summary="EIM 构建并发布成功",
                )
            if start_after:
                if self.start_callback is None:
                    raise ValueError("EIM 启动服务不可用")
                self.start_callback(task_id)
            return result
        except Exception as exc:
            current = self.repository.get_task(task_id)
            if current and current.build_state in {BuildState.BUILDING, BuildState.VALIDATING}:
                self.repository.transition_build(task_id, BuildState.FAILED)
            if session_id:
                self.repository.update_ai_session(
                    session_id,
                    state="cancelled" if cancel.is_set() else "failed",
                    steps=steps,
                    usage=usage,
                    summary=redact_text(exc),
                )
            raise
        finally:
            if model:
                model.close()
            with self._guard:
                self._cancel.pop(task_id, None)
                self._models.pop(task_id, None)
            lock.release()

    def cancel(self, task_id: str) -> bool:
        with self._guard:
            event = self._cancel.get(task_id)
            model = self._models.get(task_id)
        if event is None:
            return False
        event.set()
        if model:
            model.cancel()
        return True

    def _tool_loop(
        self,
        task_id: str,
        configuration_id: str,
        dsl: EIMDSL,
        instruction: str,
        runner: Callable[[str, str], tuple[dict[str, Any], Any]],
        cancel: threading.Event,
    ) -> tuple[EIMDSL, int, dict[str, int], list[dict[str, Any]]]:
        started = monotonic()
        history: list[dict[str, Any]] = []
        proposed: dict[str, Any] | None = None
        failures: dict[str, int] = {}
        usage = {"input_tokens": 0, "output_tokens": 0, "duration_ms": 0}
        staged_samples: list[dict[str, Any]] = []
        safe_instruction = sanitize_payload(str(instruction))
        for step in range(1, 13):
            if cancel.is_set():
                raise RuntimeError("EIM 构建已取消")
            if monotonic() - started > 600:
                raise TimeoutError("EIM AI 构建超过 10 分钟预算")
            prompt = self._prompt(task_id, dsl, safe_instruction, history)
            action, evidence = runner(configuration_id, prompt)
            sanitized = sanitize_payload(action)
            if sanitized != action:
                raise ValueError("模型动作包含疑似凭证，已拒绝写入")
            name = str(action.get("action") or "")
            arguments = action.get("arguments")
            if name not in ACTIONS or not isinstance(arguments, dict):
                raise ValueError("模型返回了未登记的 EIM 动作")
            usage["input_tokens"] += int(getattr(evidence, "input_tokens", 0) or 0)
            usage["output_tokens"] += int(getattr(evidence, "output_tokens", 0) or 0)
            usage["duration_ms"] += int(getattr(evidence, "duration_ms", 0) or 0)
            try:
                result, dsl, proposed = self._execute_tool(
                    name,
                    arguments,
                    task_id,
                    dsl,
                    proposed,
                    staged_samples,
                )
                history.append({"action": name, "ok": True, "result": redact_structure(result)})
                if name == "finish":
                    return dsl, step, usage, staged_samples
            except Exception as exc:
                failures[name] = failures.get(name, 0) + 1
                history.append(
                    {"action": name, "ok": False, "error": redact_text(exc)}
                )
                if failures[name] > 2:
                    raise ValueError(f"动作 {name} 连续修正超过 2 次") from exc
            history = history[-8:]
        raise ValueError("EIM AI 构建达到 12 回合预算但未完成")

    def _execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        task_id: str,
        dsl: EIMDSL,
        proposed: dict[str, Any] | None,
        staged_samples: list[dict[str, Any]],
    ) -> tuple[Any, EIMDSL, dict[str, Any] | None]:
        if name == "inspect_task":
            self._only(arguments)
            return self._task_facts(task_id), dsl, proposed
        if name == "inspect_source_fields":
            self._only(arguments)
            return {"fields": source_fields()}, dsl, proposed
        if name == "inspect_destination_schema":
            self._only(arguments)
            return self._destination(task_id).schema_snapshot, dsl, proposed
        if name == "read_dsl":
            self._only(arguments)
            return dsl.model_dump(mode="json"), dsl, proposed
        if name == "propose_patch":
            self._only(arguments, "patch")
            patch = arguments.get("patch")
            if not isinstance(patch, dict):
                raise ValueError("propose_patch.patch 必须是对象")
            return {"accepted": True}, dsl, patch
        if name == "apply_patch":
            self._only(arguments, "patch")
            patch = arguments.get("patch", proposed)
            if not isinstance(patch, dict):
                raise ValueError("apply_patch 之前必须提供 patch")
            merged = _merge_patch(dsl.model_dump(mode="json"), patch)
            updated = EIMDSL.model_validate(merged)
            return {"dsl": updated.model_dump(mode="json")}, updated, None
        if name == "add_sample":
            self._only(arguments, "input", "expected")
            input_value, expected = arguments.get("input"), arguments.get("expected")
            if not isinstance(input_value, dict) or not isinstance(expected, dict):
                raise ValueError("add_sample 必须提供对象 input/expected")
            if len(staged_samples) >= 100:
                raise ValueError("单次构建最多新增 100 个样例")
            # 先验证、暂存在会话内；发布事务成功前不污染任务样例。
            simulate(
                dsl,
                input_value,
                expected=expected,
                target_fields=self._target_fields(task_id),
            )
            staged_samples.append(
                {"source": "ai", "input": input_value, "expected": expected}
            )
            return {"staged_sample": len(staged_samples)}, dsl, proposed
        if name == "run_static_validation":
            self._only(arguments)
            compile_dsl(dsl, target_fields=self._target_fields(task_id))
            return {"valid": True}, dsl, proposed
        if name == "run_simulation":
            self._only(arguments, "input", "expected")
            if not isinstance(arguments.get("input"), dict):
                raise ValueError("run_simulation.input 必须是事件对象")
            result = simulate(
                dsl,
                arguments["input"],
                expected=arguments.get("expected"),
                target_fields=self._target_fields(task_id),
            )
            return result, dsl, proposed
        if name == "run_regression":
            self._only(arguments)
            result = run_samples(
                dsl,
                [*self.repository.list_samples(task_id), *staged_samples],
                target_fields=self._target_fields(task_id),
            )
            return result, dsl, proposed
        if name == "run_connection_preflight":
            self._only(arguments)
            task = self.repository.get_task(task_id)
            assert task
            connection = self.connector.refresh(task.connection_id)
            return {"state": str(connection.connection_state)}, dsl, proposed
        if name == "run_destination_preflight":
            self._only(arguments)
            return self._refresh_destination(task_id).schema_snapshot, dsl, proposed
        if name in {"publish_version", "explain_failure", "finish"}:
            self._only(arguments, "reason")
            return {"deferred_to_trusted_builder": name == "publish_version"}, dsl, proposed
        raise ValueError("未登记的 EIM 工具")

    def _validate_publish(
        self,
        task_id: str,
        revision: int,
        dsl: EIMDSL,
        configuration_id: str | None,
        usage: dict[str, int],
        staged_samples: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.repository.transition_build(task_id, BuildState.VALIDATING)
        task = self.repository.get_task(task_id)
        assert task
        if not dsl.mappings:
            raise ValueError("EIM DSL 至少需要一个目标映射")
        connection = self.connector.refresh(task.connection_id)
        if connection.connection_state is not ConnectionState.CONNECTED:
            raise ValueError("钉钉连接预检未通过")
        destination = self._refresh_destination(task_id)
        adapter = destination_adapter(self.runtime, connection, destination)
        compiled = compile_dsl(dsl, target_fields=adapter.target_fields())
        expected_action = {
            "dingtalk_doc": "append",
            "dingtalk_sheet": "append",
            "dingtalk_aitable": "upsert",
        }[str(destination.destination_type)]
        if compiled.dsl.destination_action != expected_action:
            raise ValueError(f"当前归档目标只允许 {expected_action} 动作")
        if hasattr(adapter, "validate_write_capability"):
            adapter.validate_write_capability()
        samples = [*self.repository.list_samples(task_id), *staged_samples]
        if not samples:
            raise ValueError("部署前至少需要一个脱敏样例")
        evidence = run_samples(dsl, samples, target_fields=adapter.target_fields())
        if not evidence["passed"]:
            raise ValueError("EIM 样例或回归测试未通过")
        for result in evidence["results"]:
            if result["matched"]:
                adapter.validate_values(dict(result["output"] or {}))
        self._validate_ai_steps(dsl)
        manifest = {
            "format": "fortest-eim-bundle/v1",
            "task_id": task_id,
            "draft_revision": revision,
            "builder_configuration_id": configuration_id,
            "created_at": utc_now(),
        }
        content_hash = _content_hash(dsl, samples, {**manifest, "created_at": None})
        existing = self.repository.find_version_by_hash(task_id, content_hash)
        if existing:
            deployed = self.repository.activate_version(
                task_id,
                existing.version_id,
                expected_draft_revision=revision,
            )
            version = existing
        else:
            version = EIMTaskVersion(
                version_id=new_ulid(),
                task_id=task_id,
                manifest=manifest,
                dsl=dsl.model_dump(mode="json"),
                bundle_path="",
                content_hash=content_hash,
                builder_configuration_id=configuration_id,
                test_evidence=redact_structure({**evidence, "usage": usage}),
            )
            version.bundle_path = self._write_bundle(version, samples)
            try:
                deployed = self.repository.publish_version(
                    version,
                    expected_draft_revision=revision,
                    staged_samples=staged_samples,
                )
            except Exception:
                shutil.rmtree(self.data_root / version.bundle_path, ignore_errors=True)
                raise
        return {
            "task_id": task_id,
            "version_id": version.version_id,
            "content_hash": content_hash,
            "build_state": str(deployed.build_state),
            "tests": evidence,
        }

    def _validate_ai_steps(self, dsl: EIMDSL) -> None:
        if not dsl.ai_steps:
            return
        if self.settings_service is None:
            raise ValueError("AI 增强任务缺少设置服务")
        snapshot = self.settings_service.snapshot()
        active = {
            item.id: item
            for item in snapshot.settings.ai.configurations
            if item.deleted_at is None
        }
        compatible = {
            item["configuration_id"]
            for item in self.repository.list_ai_configuration_health()
            if item["compatible"]
        }
        for step in dsl.ai_steps:
            configuration = active.get(step.configuration_id)
            if configuration is None or step.configuration_id not in compatible:
                raise ValueError(f"AI 增强配置未通过 EIM 能力检测：{step.configuration_id}")
            if not self.settings_service.ai_configuration_is_complete(configuration):
                raise ValueError(f"AI 增强配置不完整：{step.configuration_id}")

    def _refresh_destination(self, task_id: str):
        task = self.repository.get_task(task_id)
        assert task
        connection = self.repository.get_connection(task.connection_id)
        destination = self._destination(task_id)
        assert connection
        adapter = destination_adapter(self.runtime, connection, destination)
        schema = adapter.inspect_schema()
        if destination.schema_snapshot.get("sheets") and "sheets" not in schema:
            schema["sheets"] = destination.schema_snapshot["sheets"]
        destination.schema_snapshot = schema
        destination.checked_at = utc_now()
        return self.repository.save_destination(destination)

    def _target_fields(self, task_id: str) -> set[str]:
        task = self.repository.get_task(task_id)
        assert task
        connection = self.repository.get_connection(task.connection_id)
        destination = self._destination(task_id)
        assert connection
        return destination_adapter(self.runtime, connection, destination).target_fields()

    def _destination(self, task_id: str):
        task = self.repository.get_task(task_id)
        if task is None:
            raise KeyError(f"EIM 任务不存在：{task_id}")
        destination = self.repository.get_destination(task.destination_id)
        if destination is None:
            raise ValueError("EIM 归档目标不存在")
        return destination

    def _task_facts(self, task_id: str) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if task is None:
            raise KeyError(f"EIM 任务不存在：{task_id}")
        return {
            "task_id": task.task_id,
            "name": task.name,
            "platform": task.platform,
            "source_name": task.source_name,
            "event_types": [str(item) for item in task.event_types],
            "draft_revision": task.draft_revision,
        }

    def _prompt(
        self,
        task_id: str,
        dsl: EIMDSL,
        instruction: Any,
        history: list[dict[str, Any]],
    ) -> str:
        payload = {
            "instruction": instruction,
            "task": self._task_facts(task_id),
            "dsl": dsl.model_dump(mode="json"),
            "last_tool_results": history,
        }
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(text.encode("utf-8")) > 64 * 1024:
            raise ValueError("EIM 构建上下文超过 64 KiB")
        return text

    def _write_bundle(self, version: EIMTaskVersion, samples: list[dict[str, Any]]) -> str:
        base = self.data_root / "eim" / "bundles" / version.task_id
        base.mkdir(parents=True, exist_ok=True)
        target = base / version.version_id
        staging = Path(tempfile.mkdtemp(prefix=f".{version.version_id}-", dir=base))
        try:
            files = {
                "manifest.json": version.manifest,
                "dsl.json": version.dsl,
                "samples.json": redact_structure(samples),
                "evidence.json": redact_structure(version.test_evidence),
            }
            for name, value in files.items():
                (staging / name).write_text(
                    json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            os.replace(staging, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return target.relative_to(self.data_root).as_posix()

    def _draft_path(self, task_id: str) -> Path:
        normalized = str(task_id).strip()
        if not normalized or any(character not in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for character in normalized):
            raise ValueError("EIM 任务 ID 不合法")
        path = (self.data_root / "eim" / "workspaces" / normalized / "draft" / "dsl.json").resolve()
        if self.data_root not in path.parents:
            raise ValueError("EIM 草稿路径越界")
        return path

    def _task_lock(self, task_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(task_id, threading.Lock())

    def _model_adapter(self) -> EIMBuilderModelAdapter:
        if self.settings_service is None:
            raise ValueError("AI 设置服务不可用")
        return EIMBuilderModelAdapter(
            self.settings_service,
            codex_path_resolver=self.codex_path_resolver,
            client_factory=self.client_factory,
        )

    @staticmethod
    def _only(arguments: dict[str, Any], *allowed: str) -> None:
        unknown = set(arguments) - set(allowed)
        if unknown:
            raise ValueError(f"动作包含未知参数：{', '.join(sorted(unknown))}")


def _merge_patch(target: Any, patch: Any) -> Any:
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = _merge_patch(result.get(key), value)
    return result


def _content_hash(dsl: EIMDSL, samples: list[dict[str, Any]], manifest: dict[str, Any]) -> str:
    payload = {
        "dsl": dsl.model_dump(mode="json"),
        "samples": redact_structure(samples),
        "manifest": manifest,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
