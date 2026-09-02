"""固定、校验且隔离运行的 DWS v1.0.60。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Sequence

from backend.security.redaction import redact_text
from windows_native.process_policy import hidden_popen_kwargs


DWS_VERSION = "1.0.60"
DWS_ARCHIVE_SHA256 = "cce1cb02fece17443957441207849f3dd465bb3261377dedc349d27046cedcb6"
DWS_EXECUTABLE_SHA256 = "6eccc842f09e661fa3a1aefd2231b8ae849e9542903bf87da6499e24ab1ae3d3"
DWS_SOURCE = "https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli/releases/tag/v1.0.60"
_VERSION_MARKER = "dws version v1.0.60"
_RUNTIME_FILES = ("dws.exe", "LICENSE", "NOTICE")


class DWSRuntimeError(RuntimeError):
    """不含凭证、可以安全显示给用户的 DWS 错误。"""


class DWSRuntime:
    """只运行 ForTest 固定版本，不读取 PATH 或用户全局 DWS。"""

    def __init__(
        self,
        data_root: Path,
        *,
        bundled_root: Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ):
        self.data_root = Path(data_root).resolve()
        self.runtime_root = self.data_root / "runtimes" / "dws" / f"v{DWS_VERSION}"
        self.connections_root = self.data_root / "eim" / "connections"
        self._bundled_override = Path(bundled_root).resolve() if bundled_root else None
        self.runner = runner
        self.popen_factory = popen_factory
        self._lock = threading.RLock()

    def _bundled_root(self) -> Path:
        if self._bundled_override:
            return self._bundled_override
        if getattr(sys, "frozen", False):
            base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
            return base / "windows_native" / "runtimes" / "dws" / f"v{DWS_VERSION}"
        return (
            Path(__file__).resolve().parents[3]
            / "windows_native"
            / ".tools"
            / "dws"
            / f"v{DWS_VERSION}"
            / "runtime"
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _verify_executable(cls, path: Path) -> None:
        if not path.is_file() or cls._sha256(path) != DWS_EXECUTABLE_SHA256:
            raise DWSRuntimeError("DWS 运行时 SHA-256 校验失败")
        try:
            with path.open("rb") as stream:
                if stream.read(2) != b"MZ":
                    raise DWSRuntimeError("DWS 运行时不是有效的 Windows 可执行文件")
                stream.seek(0x3C)
                pe_offset = int.from_bytes(stream.read(4), "little")
                stream.seek(pe_offset)
                if stream.read(6) != b"PE\x00\x00d\x86":
                    raise DWSRuntimeError("DWS 运行时不是 Windows AMD64 架构")
        except OSError as exc:
            raise DWSRuntimeError(f"无法校验 DWS 运行时：{redact_text(exc)}") from None

    def ensure_available(self) -> Path:
        """从安装包只复制已知文件，并在使用前校验主程序。"""

        with self._lock:
            target = self.runtime_root / "dws.exe"
            if target.is_file():
                try:
                    self._verify_executable(target)
                    return target
                except DWSRuntimeError:
                    # 已知目标文件损坏时，用安装包内固定副本原子修复。
                    pass
            source_root = self._bundled_root()
            source = source_root / "dws.exe"
            self._verify_executable(source)
            self.runtime_root.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(
                tempfile.mkdtemp(prefix=f".v{DWS_VERSION}-", dir=self.runtime_root.parent)
            )
            try:
                for name in _RUNTIME_FILES:
                    source_file = source_root / name
                    if not source_file.is_file():
                        raise DWSRuntimeError(f"DWS 安装包缺少 {name}")
                    shutil.copy2(source_file, staging / name)
                self._verify_executable(staging / "dws.exe")
                self.runtime_root.mkdir(parents=True, exist_ok=True)
                for name in ("LICENSE", "NOTICE", "dws.exe"):
                    os.replace(staging / name, self.runtime_root / name)
                self._verify_executable(target)
                return target
            except OSError as exc:
                raise DWSRuntimeError(f"安装 DWS 运行时失败：{redact_text(exc)}") from None
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    def config_dir(self, connection_id: str) -> Path:
        normalized = str(connection_id).strip()
        if not normalized or any(character not in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for character in normalized):
            raise ValueError("EIM 连接 ID 不合法")
        path = (self.connections_root / normalized / "dws").resolve()
        if self.connections_root.resolve() not in path.parents:
            raise ValueError("DWS 配置目录越界")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def environment(self, config_dir: Path) -> dict[str, str]:
        path = Path(config_dir).resolve()
        if self.connections_root.resolve() not in path.parents:
            raise ValueError("DWS_CONFIG_DIR 必须位于当前 EIM 连接目录")
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        values = {
            "DWS_CONFIG_DIR": str(path),
            "NO_COLOR": "1",
            "SystemRoot": system_root,
            "WINDIR": os.environ.get("WINDIR", system_root),
            "PATH": os.pathsep.join((str(self.runtime_root), str(Path(system_root) / "System32"))),
        }
        for name in ("COMSPEC", "PATHEXT", "TEMP", "TMP"):
            if os.environ.get(name):
                values[name] = os.environ[name]
        return values

    @staticmethod
    def _arguments(values: Sequence[object]) -> list[str]:
        arguments = [str(value) for value in values]
        for index, value in enumerate(arguments):
            if not value or "\x00" in value:
                raise ValueError("DWS 命令参数不合法")
            if ("\r" in value or "\n" in value) and (
                index == 0 or arguments[index - 1] != "--content"
            ):
                # shell=False 下正文可安全保留换行；其它参数仍拒绝换行混淆。
                raise ValueError("DWS 命令参数不合法")
        return arguments

    def run(
        self,
        arguments: Sequence[object],
        *,
        config_dir: Path,
        timeout: float = 30,
    ) -> subprocess.CompletedProcess[str]:
        executable = self.ensure_available()
        command = [str(executable), *self._arguments(arguments)]
        try:
            completed = self.runner(
                command,
                env=self.environment(config_dir),
                cwd=str(self.data_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(1.0, min(float(timeout), 900.0)),
                check=False,
                shell=False,
                **hidden_popen_kwargs({}),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DWSRuntimeError(f"DWS 命令运行失败：{redact_text(exc)}") from None
        if completed.returncode != 0:
            detail = completed.stderr or completed.stdout or f"退出码 {completed.returncode}"
            raise DWSRuntimeError(f"DWS 操作失败：{redact_text(detail)}")
        return completed

    def run_json(
        self,
        arguments: Sequence[object],
        *,
        config_dir: Path,
        timeout: float = 30,
    ) -> dict[str, Any]:
        completed = self.run([*arguments, "-f", "json"], config_dir=config_dir, timeout=timeout)
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise DWSRuntimeError("DWS 返回了无效 JSON") from exc
        if not isinstance(value, dict):
            raise DWSRuntimeError("DWS 返回结构不是对象")
        return value

    def probe(self) -> dict[str, str]:
        executable = self.ensure_available()
        try:
            completed = self.runner(
                [str(executable), "--version"],
                cwd=str(self.data_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
                shell=False,
                **hidden_popen_kwargs({}),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DWSRuntimeError(f"DWS 版本探测失败：{redact_text(exc)}") from None
        output = str(completed.stdout or completed.stderr or "").strip()
        if completed.returncode != 0 or _VERSION_MARKER not in output:
            raise DWSRuntimeError("DWS 固定版本探测失败")
        return {
            "version": DWS_VERSION,
            "sha256": DWS_EXECUTABLE_SHA256,
            "source": DWS_SOURCE,
            "path": str(executable),
        }

    def popen(
        self,
        arguments: Sequence[object],
        *,
        config_dir: Path,
    ) -> subprocess.Popen[str]:
        executable = self.ensure_available()
        command = [str(executable), *self._arguments(arguments)]
        try:
            return self.popen_factory(
                command,
                env=self.environment(config_dir),
                cwd=str(self.data_root),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                **hidden_popen_kwargs({}),
            )
        except OSError as exc:
            raise DWSRuntimeError(f"DWS 订阅进程启动失败：{redact_text(exc)}") from None
