"""ForTest 桌面端统一产品元数据。"""

from __future__ import annotations

import os
from collections.abc import Mapping

PRODUCT_NAME = "ForTest"
PRODUCT_VERSION = "0.2.16"
EXECUTABLE_NAME = "ForTest.exe"


def eim_feature_enabled(environment: Mapping[str, str] | None = None) -> bool:
    """EIM 在完成真实 G0 前仅允许通过显式内部试运行开关启用。"""

    source = os.environ if environment is None else environment
    return source.get("FORTEST_ENABLE_EIM", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
