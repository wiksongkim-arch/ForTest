"""ForTest 构建前后隐私与业务配置泄露门禁。"""

from __future__ import annotations

import argparse
import getpass
import json
import marshal
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import CodeType
from typing import Any, Iterable

# 允许 build.ps1 从任意工作目录直接执行本文件，且不依赖调用方 PYTHONPATH。
if __package__ in {None, ""}:
    bootstrap_root = Path(__file__).resolve().parents[1]
    if os.fspath(bootstrap_root) not in sys.path:
        sys.path.insert(0, os.fspath(bootstrap_root))

from backend.ai.provider_specs import PROVIDER_SPECS
from backend.settings.defaults import DEFAULT_PROMPTS, default_settings
from backend.settings.paths import user_data_root
from backend.settings.secrets import KeyringSecretStore
from backend.settings.service import SECRET_ENV, ai_configuration_secret_name


_FIRST_PARTY_PREFIXES = (
    "backend",
    "services",
    "utils",
    "windows_native",
)
_FORBIDDEN_MODULE_PREFIXES = (
    "frontend",
    "plugins",
    "tests",
    "windows_desktop",
)
_FORBIDDEN_ARTIFACT_NAMES = {
    ".env",
    "desktop_preferences.json",
    "generation_tasks.json",
    "jenkins.json",
    "jenkins_deployment_tasks.json",
    "jenkins_projects.json",
    "jenkins_task_settings.json",
    "settings.json",
}
_SECRET_VALUE_PATTERN = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,})"
)
_DINGTALK_NODE_PATTERN = re.compile(
    r"https://alidocs\.dingtalk\.com/(?:i/)?nodes/[A-Za-z0-9_-]{16,}",
    re.IGNORECASE,
)
_PERSONAL_PATH_PATTERN = re.compile(
    r"(?:[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/][^\\/\s]+|"
    r"/(?:home|Users)/[^/\s]+)",
    re.IGNORECASE,
)
_SENSITIVE_ENV_NAME_PATTERN = re.compile(
    r"(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    code: str
    location: str
    detail: str


@dataclass(frozen=True)
class SensitiveCandidate:
    """值永不进入 repr、报告或异常文本。"""

    label: str
    value: str = field(repr=False)
    kind: str = "business"


def _public_values() -> set[str]:
    values = set(DEFAULT_PROMPTS.values())
    for spec in PROVIDER_SPECS:
        values.update(
            {
                spec.label,
                spec.base_url,
                spec.default_model,
                spec.documentation_url,
            }
        )
    values.discard("")
    return values


_PUBLIC_VALUES = _public_values()


def _candidate_value(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if len(normalized) < 4 or normalized in _PUBLIC_VALUES:
        return None
    return normalized


def _walk_json_strings(
    value: object,
    *,
    key_path: tuple[str, ...] = (),
) -> Iterable[tuple[tuple[str, ...], str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_json_strings(
                child,
                key_path=(*key_path, str(key)),
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_json_strings(
                child,
                key_path=(*key_path, str(index)),
            )
    elif isinstance(value, str):
        yield key_path, value


def _looks_like_business_value(key_path: tuple[str, ...], value: str) -> bool:
    if value in _PUBLIC_VALUES or len(value.strip()) < 8:
        return False
    key = key_path[-1].lower() if key_path else ""
    if key in {
        "base_url",
        "content",
        "content_template_url",
        "doc_url",
        "document_template_url",
        "iteration_name",
        "local_output_dir",
        "manifest_url",
        "name",
        "output_folder_url",
        "task_name",
        "username",
    }:
        return True
    return bool(
        value.startswith(("http://", "https://"))
        or _PERSONAL_PATH_PATTERN.search(value)
        or (len(value) >= 24 and any(character.isspace() for character in value))
    )


def _read_user_json_candidates(root: Path) -> list[SensitiveCandidate]:
    candidates: list[SensitiveCandidate] = []
    data = root / "data"
    if not data.is_dir():
        return candidates
    for path in sorted(data.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        for key_path, raw in _walk_json_strings(payload):
            value = _candidate_value(raw)
            if value and _looks_like_business_value(key_path, value):
                candidates.append(
                    SensitiveCandidate(
                        label=f"user-json:{path.name}",
                        value=value,
                        kind="business",
                    )
                )
    return candidates


def collect_sensitive_candidates(
    project_root: Path,
    environment: dict[str, str] | None = None,
) -> list[SensitiveCandidate]:
    """只在内存中解析当前用户值，报告永不包含明文。"""

    source = environment if environment is not None else dict(os.environ)
    candidates: list[SensitiveCandidate] = []
    for name, raw in source.items():
        if not _SENSITIVE_ENV_NAME_PATTERN.search(name):
            continue
        value = _candidate_value(raw)
        if value:
            candidates.append(
                SensitiveCandidate(f"environment:{name}", value, "secret")
            )

    for label, raw in (
        ("current-user", getpass.getuser()),
        ("home-directory", os.fspath(Path.home())),
        ("project-directory", os.fspath(project_root.resolve())),
    ):
        value = _candidate_value(raw)
        if value:
            candidates.append(SensitiveCandidate(label, value, "personal"))

    data_root = user_data_root(source)
    candidates.extend(_read_user_json_candidates(data_root))

    secret_names = {"jenkins_api_token"}
    secret_names.update(SECRET_ENV)
    settings_path = data_root / "data" / "settings.json"
    try:
        settings_payload = json.loads(settings_path.read_text(encoding="utf-8"))
        configurations = (settings_payload.get("ai") or {}).get(
            "configurations"
        ) or []
        for item in configurations:
            if isinstance(item, dict) and str(item.get("id") or "").strip():
                secret_names.add(
                    ai_configuration_secret_name(str(item["id"]))
                )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    keyring_store = KeyringSecretStore()
    for index, name in enumerate(sorted(secret_names)):
        try:
            value = _candidate_value(keyring_store.get(name))
        except Exception:
            value = None
        if value:
            candidates.append(
                SensitiveCandidate(f"keyring:{index}", value, "secret")
            )

    # 同值去重可以显著降低大文件扫描成本，同时不暴露值本身。
    unique: dict[tuple[str, str], SensitiveCandidate] = {}
    for item in candidates:
        unique.setdefault((item.kind, item.value), item)
    return list(unique.values())


def audit_source(project_root: Path) -> list[Finding]:
    """确认源码默认值、工作区和打包定义不会携带用户业务数据。"""

    root = project_root.resolve()
    findings: list[Finding] = []
    retired_plugins = root / "plugins"
    if retired_plugins.exists():
        findings.append(
            Finding(
                "retired-plugin-present",
                "plugins",
                "已退役插件目录仍位于活跃源码区",
            )
        )
    forbidden_workspace = [root / "data" / "settings.json"]
    forbidden_workspace.extend(
        path
        for path in root.glob(".env*")
        if path.name != ".env.example"
    )
    for path in forbidden_workspace:
        if path.is_file():
            findings.append(
                Finding(
                    "workspace-user-data",
                    path.relative_to(root).as_posix(),
                    "源码工作区存在运行时用户配置文件",
                )
            )

    document = default_settings().document
    if any(
        (
            document.content_template_url,
            document.document_template_url,
            document.output_folder_url,
        )
    ):
        findings.append(
            Finding(
                "business-default",
                "backend/settings/defaults.py",
                "产品默认值包含实际业务模板地址",
            )
        )

    production_roots = (
        root / "backend",
        root / "services",
        root / "utils",
        root / "windows_native",
    )
    excluded_parts = {
        ".build",
        ".build-venv",
        "__pycache__",
        "dist",
        "final-validation",
        "tests",
    }
    for production_root in production_roots:
        if not production_root.is_dir():
            continue
        for path in production_root.rglob("*.py"):
            if any(part in excluded_parts for part in path.parts):
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            relative = path.relative_to(root).as_posix()
            if _SECRET_VALUE_PATTERN.search(source):
                findings.append(
                    Finding(
                        "hardcoded-secret-shape",
                        relative,
                        "生产源码包含疑似明文密钥格式",
                    )
                )
            if _DINGTALK_NODE_PATTERN.search(source):
                findings.append(
                    Finding(
                        "hardcoded-business-node",
                        relative,
                        "生产源码包含固定钉钉业务节点地址",
                    )
                )

    spec_path = root / "windows_native" / "ForTest.spec"
    installer_path = root / "windows_native" / "installer.iss"
    try:
        spec = spec_path.read_text(encoding="utf-8")
    except OSError:
        spec = ""
    if (
        "Tree(" in spec
        or '"frontend"' not in spec
        or '"pytest"' not in spec
        or "include_py_files=False" not in spec
    ):
        findings.append(
            Finding(
                "unsafe-spec",
                "windows_native/ForTest.spec",
                "PyInstaller 定义缺少窄化收集或必要排除项",
            )
        )
    try:
        installer = installer_path.read_text(encoding="utf-8")
    except OSError:
        installer = ""
    expected_source = 'Source: "dist\\ForTest\\*"'
    if expected_source not in installer or "data\\*" in installer.lower():
        findings.append(
            Finding(
                "unsafe-installer-source",
                "windows_native/installer.iss",
                "安装器来源不是单一 PyInstaller 产物目录",
            )
        )
    return findings


def _walk_code(code: CodeType) -> Iterable[tuple[str, str]]:
    yield "filename", code.co_filename
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            yield from _walk_code(constant)
        elif isinstance(constant, str):
            yield "constant", constant


def _first_party_module(module_name: str) -> bool:
    return module_name == "main" or module_name.startswith(
        tuple(f"{prefix}." for prefix in _FIRST_PARTY_PREFIXES)
    ) or module_name in _FIRST_PARTY_PREFIXES


def _scan_code_object(
    module_name: str,
    code: CodeType,
    candidates: list[SensitiveCandidate],
) -> list[Finding]:
    findings: list[Finding] = []
    first_party = _first_party_module(module_name)
    for kind, text in _walk_code(code):
        if first_party and kind == "filename" and (
            Path(text).is_absolute() or _PERSONAL_PATH_PATTERN.search(text)
        ):
            findings.append(
                Finding(
                    "absolute-build-path",
                    module_name,
                    "归档代码保留了绝对编译路径",
                )
            )
        if first_party and kind == "constant":
            if _SECRET_VALUE_PATTERN.search(text):
                findings.append(
                    Finding(
                        "hardcoded-secret-shape",
                        module_name,
                        "归档代码包含疑似明文密钥格式",
                    )
                )
            if _DINGTALK_NODE_PATTERN.search(text):
                findings.append(
                    Finding(
                        "hardcoded-business-node",
                        module_name,
                        "归档代码包含固定钉钉业务节点地址",
                    )
                )
            if _PERSONAL_PATH_PATTERN.search(text):
                findings.append(
                    Finding(
                        "personal-path",
                        module_name,
                        "归档代码包含个人用户目录",
                    )
                )
        folded = text.casefold()
        for candidate in candidates:
            # 业务 JSON 若被复制会先被文件清单拦截；代码常量只精确比对密钥和
            # 本机标识，避免把通用项目名或模型名误判为用户数据。
            if candidate.kind == "business":
                continue
            matched = (
                candidate.value in text
                if candidate.kind == "secret"
                else candidate.value.casefold() in folded
            )
            if matched:
                findings.append(
                    Finding(
                        f"embedded-{candidate.kind}",
                        module_name,
                        f"归档代码命中受保护值来源 {candidate.label}",
                    )
                )
    return findings


def _archive_findings(
    executable: Path,
    candidates: list[SensitiveCandidate],
) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    module_count = 0
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except Exception:
        return [
            Finding(
                "archive-auditor-unavailable",
                executable.name,
                "当前构建环境无法读取 PyInstaller 归档",
            )
        ], module_count
    try:
        archive = CArchiveReader(os.fspath(executable))
        for name, metadata in archive.toc.items():
            typecode = metadata[-1]
            if typecode not in {"m", "s", "M"}:
                continue
            raw = archive.extract(name)
            try:
                code = marshal.loads(raw)
            except Exception:
                continue
            if isinstance(code, CodeType):
                module_count += 1
                findings.extend(_scan_code_object(name, code, candidates))
        pyz = archive.open_embedded_archive("PYZ.pyz")
        for module_name in pyz.toc:
            module_count += 1
            if any(
                module_name == prefix or module_name.startswith(f"{prefix}.")
                for prefix in _FORBIDDEN_MODULE_PREFIXES
            ):
                findings.append(
                    Finding(
                        "forbidden-module",
                        module_name,
                        "原生安装包收集了非运行时模块",
                    )
                )
            try:
                code = pyz.extract(module_name)
            except Exception:
                continue
            if isinstance(code, CodeType):
                findings.extend(
                    _scan_code_object(module_name, code, candidates)
                )
    except Exception:
        findings.append(
            Finding(
                "archive-read-failed",
                executable.name,
                "PyInstaller 归档完整性检查失败",
            )
        )
    return findings, module_count


def _raw_sensitive_findings(
    artifact_root: Path,
    candidates: list[SensitiveCandidate],
) -> list[Finding]:
    """大文件只扫描密钥和本机标识；业务内容由代码归档审计负责。"""

    protected = [
        item
        for item in candidates
        if item.kind in {"secret", "personal"} and len(item.value) >= 4
    ]
    encoded: list[tuple[SensitiveCandidate, bytes]] = []
    for item in protected:
        for codec in ("utf-8", "utf-16-le"):
            try:
                raw = item.value.encode(codec)
            except UnicodeError:
                continue
            if raw:
                encoded.append((item, raw))
    if not encoded:
        return []
    overlap = max(len(raw) for _item, raw in encoded) - 1
    findings: list[Finding] = []
    for path in artifact_root.rglob("*"):
        if not path.is_file():
            continue
        previous = b""
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    block = previous + chunk
                    matched = next(
                        (
                            item
                            for item, raw in encoded
                            if raw in block
                        ),
                        None,
                    )
                    if matched is not None:
                        findings.append(
                            Finding(
                                f"raw-{matched.kind}",
                                path.relative_to(artifact_root).as_posix(),
                                f"文件命中受保护值来源 {matched.label}",
                            )
                        )
                        break
                    previous = block[-overlap:] if overlap > 0 else b""
        except OSError:
            findings.append(
                Finding(
                    "artifact-read-failed",
                    path.relative_to(artifact_root).as_posix(),
                    "产物文件无法完成隐私扫描",
                )
            )
    return findings


def audit_artifact(
    project_root: Path,
    artifact_root: Path,
    executable: Path,
) -> tuple[list[Finding], dict[str, int]]:
    root = artifact_root.resolve()
    findings: list[Finding] = []
    files = [path for path in root.rglob("*") if path.is_file()]
    for path in files:
        if path.name.casefold() in _FORBIDDEN_ARTIFACT_NAMES:
            findings.append(
                Finding(
                    "user-data-file",
                    path.relative_to(root).as_posix(),
                    "安装目录包含运行时用户数据文件",
                )
            )
    candidates = collect_sensitive_candidates(project_root)
    archive_results, module_count = _archive_findings(
        executable,
        candidates,
    )
    findings.extend(archive_results)
    findings.extend(_raw_sensitive_findings(root, candidates))
    return findings, {
        "artifact_files": len(files),
        "archive_modules": module_count,
        "protected_value_sources": len(candidates),
    }


def _deduplicate(findings: Iterable[Finding]) -> list[Finding]:
    unique = {
        (item.code, item.location, item.detail): item
        for item in findings
    }
    return [unique[key] for key in sorted(unique)]


def _write_report(
    path: Path | None,
    *,
    phase: str,
    findings: list[Finding],
    metrics: dict[str, int],
) -> dict[str, Any]:
    report = {
        "schema_version": 1,
        "phase": phase,
        "ok": not findings,
        "metrics": metrics,
        "findings": [asdict(item) for item in findings],
    }
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ForTest 安装包隐私门禁")
    parser.add_argument("phase", choices=("source", "artifact"))
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)

    metrics: dict[str, int] = {}
    if args.phase == "source":
        findings = audit_source(args.project_root)
    else:
        if args.artifact_root is None or args.executable is None:
            parser.error("artifact 阶段必须提供 --artifact-root 和 --executable")
        findings, metrics = audit_artifact(
            args.project_root,
            args.artifact_root,
            args.executable,
        )
    findings = _deduplicate(findings)
    report = _write_report(
        args.report,
        phase=args.phase,
        findings=findings,
        metrics=metrics,
    )
    if report["ok"]:
        print(f"隐私门禁通过：{args.phase}")
        return 0
    print(f"隐私门禁失败：{args.phase}，发现 {len(findings)} 项")
    for item in findings:
        print(f"- [{item.code}] {item.location}: {item.detail}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
