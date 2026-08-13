"""内置默认模板的完整性校验与用户工作副本管理。"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import uuid4

from filelock import FileLock


CONTENT_TEMPLATE = "content"
OUTPUT_TEMPLATE = "output"


@dataclass(frozen=True)
class DefaultTemplateDefinition:
    """描述一份不可写打包母版及其用户可见副本。"""

    key: str
    bundled_name: str
    user_name: str
    sha256: str


_DEFINITIONS: Final[dict[str, DefaultTemplateDefinition]] = {
    CONTENT_TEMPLATE: DefaultTemplateDefinition(
        key=CONTENT_TEMPLATE,
        bundled_name="test_case_content_template.xlsx",
        user_name="用例模板表格.xlsx",
        sha256="6d3e1395150390d84b460ea644420e9a6b9cd29ab6593f572143bd3bdd466978",
    ),
    OUTPUT_TEMPLATE: DefaultTemplateDefinition(
        key=OUTPUT_TEMPLATE,
        bundled_name="test_case_output_template.xlsx",
        user_name="输出文档模板.xlsx",
        sha256="26eaa30a569977e7b6297028888110f7840d5fd2bff8f1cb57c10e500d9a1157",
    ),
}


class DefaultTemplateError(RuntimeError):
    """默认模板缺失或完整性校验失败。"""


def bundled_templates_root() -> Path:
    """返回源码态或 PyInstaller 打包态的母版目录。"""

    return (
        Path(__file__).resolve().parents[1]
        / "windows_native"
        / "assets"
        / "default_templates"
    )


class DefaultTemplateManager:
    """只读母版、创建用户副本，并仅在用户确认后恢复副本。"""

    def __init__(
        self,
        data_root: str | Path,
        *,
        bundle_root: str | Path | None = None,
    ) -> None:
        self.data_root = Path(data_root).expanduser().resolve()
        self.user_root = self.data_root / "templates"
        self.bundle_root = (
            Path(bundle_root).expanduser().resolve()
            if bundle_root is not None
            else bundled_templates_root()
        )
        self._lock = FileLock(str(self.user_root / ".default-templates.lock"))

    def ensure_all(self) -> dict[str, Path]:
        """首次运行时补齐副本，已有用户文件绝不被静默覆盖。"""

        return {key: self.ensure(key) for key in _DEFINITIONS}

    def ensure(self, key: str) -> Path:
        definition = self._definition(key)
        destination = self._user_path(definition)
        with self._lock:
            if not destination.is_file():
                self._replace_from_bundle(definition, destination)
        return destination

    def restore(self, key: str) -> Path:
        """以已校验母版原子覆盖用户副本；调用方负责二次确认。"""

        definition = self._definition(key)
        destination = self._user_path(definition)
        with self._lock:
            self._replace_from_bundle(definition, destination)
        return destination

    def user_path(self, key: str) -> Path:
        return self.ensure(key)

    @staticmethod
    def keys() -> tuple[str, ...]:
        return tuple(_DEFINITIONS)

    @staticmethod
    def _definition(key: str) -> DefaultTemplateDefinition:
        normalized = str(key or "").strip().casefold()
        try:
            return _DEFINITIONS[normalized]
        except KeyError:
            raise DefaultTemplateError("默认模板类型无效") from None

    def _user_path(self, definition: DefaultTemplateDefinition) -> Path:
        destination = (self.user_root / definition.user_name).resolve()
        if destination.parent != self.user_root.resolve():
            raise DefaultTemplateError("默认模板用户路径越界")
        return destination

    def _verified_bundle_path(
        self,
        definition: DefaultTemplateDefinition,
    ) -> Path:
        source = (self.bundle_root / definition.bundled_name).resolve()
        if source.parent != self.bundle_root.resolve() or not source.is_file():
            raise DefaultTemplateError("程序内置默认模板缺失")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if digest.casefold() != definition.sha256.casefold():
            raise DefaultTemplateError("程序内置默认模板完整性校验失败")
        return source

    def _replace_from_bundle(
        self,
        definition: DefaultTemplateDefinition,
        destination: Path,
    ) -> None:
        # 母版在整个生命周期中只作为 copyfile 的读取源，系统逻辑从不写入它。
        source = self._verified_bundle_path(definition)
        self.user_root.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.name}.{uuid4().hex}.tmp"
        )
        try:
            shutil.copyfile(source, temporary)
            with temporary.open("rb+") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


__all__ = [
    "CONTENT_TEMPLATE",
    "OUTPUT_TEMPLATE",
    "DefaultTemplateError",
    "DefaultTemplateManager",
    "bundled_templates_root",
]
