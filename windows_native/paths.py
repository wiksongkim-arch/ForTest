"""原生桌面端路径约定。"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from backend.settings.paths import user_data_root


def project_root() -> Path:
    """返回源码项目根目录；打包后用于诊断显示。"""

    return Path(__file__).resolve().parents[1]


def app_data_root() -> Path:
    """返回 ForTest 统一用户数据根目录。"""

    return user_data_root()


def previous_app_data_root() -> Path:
    """返回上一品牌版本使用的用户数据目录。"""

    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "QAQ" / "UserData"


def older_brand_app_data_root() -> Path:
    """返回更早的 ForTester 用户数据目录。"""

    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "ForTester" / "UserData"


def legacy_app_data_root() -> Path:
    """返回 0.2.0 之前桌面端使用的旧数据目录。"""

    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "PRDtoCASE"


def migrate_legacy_user_data(destination: Path | None = None) -> Path:
    """首次启动时复制最近品牌的数据，保留旧目录以便安全回退。"""

    target = Path(destination) if destination is not None else app_data_root()
    marker = target / "data" / ".legacy-migration-complete"
    if marker.exists():
        return target
    source = next(
        (
            candidate
            for candidate in (
                previous_app_data_root(),
                older_brand_app_data_root(),
                legacy_app_data_root(),
            )
            if candidate.is_dir() and candidate.resolve() != target.resolve()
        ),
        None,
    )
    if source is None:
        return target

    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        # 进程锁不能跨版本复制，否则会把已退出的旧实例误判为仍在运行。
        if item.name.endswith(".lock") or item.name == "prd-to-case-native.lock":
            continue
        destination_item = target / item.name
        try:
            if item.is_dir():
                shutil.copytree(item, destination_item, dirs_exist_ok=True)
            elif not destination_item.exists():
                shutil.copy2(item, destination_item)
        except OSError:
            # 单个历史日志被占用不应阻止程序启动；业务配置会在下次启动继续迁移。
            continue
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        f"已从 {source} 复制用户数据；原目录未删除。\n",
        encoding="utf-8",
    )
    return target


def remove_user_data() -> None:
    """删除卸载器明确授权的 ForTest 及历史品牌用户数据。"""

    target = app_data_root().resolve()
    previous = previous_app_data_root().resolve()
    older_brand = older_brand_app_data_root().resolve()
    legacy = legacy_app_data_root().resolve()
    base = target.parents[1]
    expected_target = (base / "ForTest" / "UserData").resolve()
    expected_previous = (base / "QAQ" / "UserData").resolve()
    expected_older_brand = (base / "ForTester" / "UserData").resolve()
    expected_legacy = (base / "PRDtoCASE").resolve()
    if target != expected_target or target.name != "UserData":
        raise RuntimeError("拒绝删除未通过安全校验的用户数据目录")
    if previous != expected_previous or previous.name != "UserData":
        raise RuntimeError("拒绝删除未通过安全校验的旧品牌用户数据目录")
    if older_brand != expected_older_brand or older_brand.name != "UserData":
        raise RuntimeError("拒绝删除未通过安全校验的更早品牌用户数据目录")
    if legacy != expected_legacy or legacy.name != "PRDtoCASE":
        raise RuntimeError("拒绝删除未通过安全校验的旧用户数据目录")
    # Windows 不能删除进程当前工作目录，先切换到经过校验的共同父目录。
    os.chdir(base)
    for root in (target, previous, older_brand, legacy):
        if root.exists():
            shutil.rmtree(root)
    for brand_root in (target.parent, previous.parent, older_brand.parent):
        try:
            brand_root.rmdir()
        except OSError:
            pass


def prepare_runtime(root_override: Path | None = None) -> Path:
    """创建运行目录并切换工作目录，保证相对输出路径稳定。

    ``root_override`` 仅供打包启动自检使用，使诊断过程不会读取或改写正式
    用户数据；正常启动仍固定使用 ``app_data_root()``。
    """

    root = Path(root_override) if root_override is not None else migrate_legacy_user_data()
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "output").mkdir(parents=True, exist_ok=True)
    os.chdir(root)
    if not getattr(sys, "frozen", False):
        source_root = str(project_root())
        if source_root not in sys.path:
            sys.path.insert(0, source_root)
    return root
