"""轻量级界面错误格式化，避免启动时提前加载完整业务依赖。"""

from __future__ import annotations

from typing import Any


def _redact(value: object) -> str:
    """仅在真正发生异常时加载脱敏模块。"""

    text = str(value)
    try:
        from backend.security.redaction import redact_text

        return redact_text(text)
    except Exception:
        return text


def friendly_error(exc: BaseException) -> str:
    """以鸭子类型兼容 FastAPI 与 Pydantic 异常，保持启动导入轻量。"""

    detail = getattr(exc, "detail", None)
    if detail is not None:
        if isinstance(detail, dict):
            fields = detail.get("fields")
            if isinstance(fields, dict):
                return _redact(
                    "；".join(f"{key}：{value}" for key, value in fields.items())
                )
        return _redact(detail)
    errors_method = getattr(exc, "errors", None)
    if callable(errors_method):
        try:
            errors: list[dict[str, Any]] = errors_method(
                include_input=False,
                include_url=False,
            )
        except (TypeError, ValueError):
            errors = []
        details: list[str] = []
        for error in errors:
            field = ".".join(str(item) for item in error.get("loc", ()))
            message = str(error.get("msg", "输入验证失败"))
            details.append(f"{field}：{message}" if field else message)
        if details:
            return _redact("；".join(details))
    return _redact(str(exc).strip() or type(exc).__name__)
