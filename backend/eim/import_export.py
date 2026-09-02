"""EIM 任务的无凭证、可校验 ZIP 导入导出。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from backend.eim.models import EIMDSL, EIMTask, EventType, utc_now
from backend.eim.redaction import sanitize_payload
from backend.eim.repository import EIMRepository


FORMAT_VERSION = "fortest-eim/v1"
_FILES = frozenset({"manifest.json", "dsl.json", "samples.json", "hashes.json"})
_MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
_MAX_MEMBER_BYTES = 5 * 1024 * 1024
_SAFE_SAMPLE_STRINGS = {
    "platform",
    "event_type",
    "message_kind",
    "mime_type",
    "occurred_at",
    "received_at",
}


def _encode(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def export_task(
    repository: EIMRepository,
    task_id: str,
    output_path: Path,
    *,
    draft_dsl: EIMDSL | None = None,
) -> Path:
    """导出允许字段；连接 ID、目标链接、正文、日志和本机路径均不进入包。"""

    task = repository.get_task(task_id)
    if task is None:
        raise KeyError(f"EIM 任务不存在：{task_id}")
    version = repository.get_version(task.active_version_id) if task.active_version_id else None
    dsl = draft_dsl or (EIMDSL.model_validate(version.dsl) if version else EIMDSL())
    manifest = {
        "format": FORMAT_VERSION,
        "exported_at": utc_now(),
        "task": {
            "name": task.name,
            "platform": task.platform,
            "event_types": [str(item) for item in task.event_types],
        },
    }
    samples = [
        {
            "source": "exported",
            "input": _sample_without_content(sample["input"]),
            "expected": _sample_without_content(sample["expected"]),
        }
        for sample in repository.list_samples(task_id)
    ]
    payloads = {
        "manifest.json": _encode(manifest),
        "dsl.json": _encode(sanitize_payload(dsl.model_dump(mode="json"))),
        "samples.json": _encode(samples),
    }
    payloads["hashes.json"] = _encode(
        {name: f"sha256:{_digest(value)}" for name, value in payloads.items()}
    )

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, value in payloads.items():
                archive.writestr(name, value)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _sample_without_content(value: Any, *, key: str = "") -> Any:
    """保留样例结构和枚举，移除正文、个人名称、ID、链接及任意字符串输出。"""

    sanitized = sanitize_payload(value)
    if isinstance(sanitized, dict):
        return {
            str(item_key): _sample_without_content(item_value, key=str(item_key))
            for item_key, item_value in sanitized.items()
        }
    if isinstance(sanitized, list):
        return [_sample_without_content(item, key=key) for item in sanitized]
    if isinstance(sanitized, str):
        return sanitized if key in _SAFE_SAMPLE_STRINGS else "<redacted>"
    return sanitized


def import_task(
    repository: EIMRepository,
    archive_path: Path,
    *,
    connection_id: str,
    source_id: str,
    source_name: str,
    destination_id: str,
) -> tuple[EIMTask, EIMDSL]:
    """校验整个包后创建全新停止草稿，并强制使用调用方重新绑定的对象。"""

    path = Path(archive_path)
    if not path.is_file() or path.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise ValueError("EIM 导入包不存在或超过 10 MiB")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)) or set(names) != _FILES:
                raise ValueError("EIM 导入包文件清单不合法")
            for info in infos:
                # 不解压到磁盘，同时拒绝目录、绝对路径、符号链接和超大成员。
                mode = info.external_attr >> 16
                if (
                    info.is_dir()
                    or Path(info.filename).is_absolute()
                    or ".." in Path(info.filename).parts
                    or (mode & 0o170000) == 0o120000
                    or info.file_size > _MAX_MEMBER_BYTES
                ):
                    raise ValueError("EIM 导入包包含不安全成员")
            raw = {name: archive.read(name) for name in _FILES}
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError("EIM 导入包无法读取") from exc

    hashes = _load_object(raw["hashes.json"], "哈希清单")
    for name in _FILES - {"hashes.json"}:
        if hashes.get(name) != f"sha256:{_digest(raw[name])}":
            raise ValueError(f"EIM 导入包校验失败：{name}")
    manifest = _load_object(raw["manifest.json"], "清单")
    if manifest.get("format") != FORMAT_VERSION or not isinstance(manifest.get("task"), dict):
        raise ValueError("不支持的 EIM 导入包版本")
    template = manifest["task"]
    dsl = EIMDSL.model_validate(_load_object(raw["dsl.json"], "DSL"))
    samples = _load_array(raw["samples.json"], "样例")
    validated_samples: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("EIM 样例格式不合法")
        input_value = sample.get("input")
        expected = sample.get("expected")
        if not isinstance(input_value, dict) or not isinstance(expected, dict):
            raise ValueError("EIM 样例必须是对象")
        validated_samples.append(
            (input_value, expected, str(sample.get("source") or "imported")[:40])
        )

    task = repository.create_task(
        EIMTask(
            name=str(template.get("name") or "导入的 EIM 任务"),
            connection_id=connection_id,
            source_id=source_id,
            source_name=source_name,
            destination_id=destination_id,
            event_types=[EventType(item) for item in template.get("event_types", [])],
        )
    )
    try:
        for input_value, expected, source in validated_samples:
            repository.add_sample(task.task_id, input_value, expected, source=source)
    except Exception:
        repository.soft_delete_task(task.task_id)
        repository.purge_task(task.task_id)
        raise
    return task, dsl


def _load_object(value: bytes, label: str) -> dict[str, Any]:
    parsed = _load_json(value, label)
    if not isinstance(parsed, dict):
        raise ValueError(f"EIM {label}必须是对象")
    return parsed


def _load_array(value: bytes, label: str) -> list[Any]:
    parsed = _load_json(value, label)
    if not isinstance(parsed, list) or len(parsed) > 1_000:
        raise ValueError(f"EIM {label}必须是至多 1000 项的数组")
    return parsed


def _load_json(value: bytes, label: str) -> Any:
    try:
        return json.loads(value.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"EIM {label}不是有效 JSON") from exc
