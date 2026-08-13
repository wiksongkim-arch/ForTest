"""原生端不得重新引入 Web 服务架构。"""

from pathlib import Path


def _production_sources(root: Path) -> str:
    """只审计本工程源码，排除隔离环境与构建产物。"""

    paths = [*root.glob("*.py"), *(root / "ui").glob("*.py")]
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def test_native_entry_does_not_import_web_frontend_or_servers():
    root = Path(__file__).resolve().parents[1]
    sources = _production_sources(root)
    forbidden_imports = (
        "import streamlit",
        "from streamlit",
        "import uvicorn",
        "from uvicorn",
        "import webview",
        "from webview",
    )
    assert not any(item in sources for item in forbidden_imports)


def test_native_entry_has_no_socket_listener():
    root = Path(__file__).resolve().parents[1]
    sources = _production_sources(root)
    assert ".listen(" not in sources
    assert "localhost:" not in sources
    assert "127.0.0.1:" not in sources


def test_legacy_web_entrypoints_are_inactive_and_optional_archive_is_complete():
    project = Path(__file__).resolve().parents[2]
    archive_root = project / "预删除" / "web_legacy"
    assert "预删除/" in (project / ".gitignore").read_text(encoding="utf-8")
    legacy_paths = (
        "frontend",
        "backend/main.py",
        "backend/core/config.py",
        "start.py",
        "startup_browser.py",
        "startup_managed.py",
        "startup_runtime.py",
        "config.py",
    )
    for relative in legacy_paths:
        assert not (project / relative).exists()
        # 本机开发工作区保留可恢复副本；公开克隆按 .gitignore 不携带预删除内容。
        if archive_root.exists():
            assert (archive_root / relative).exists()

    # 当前依赖契约不再安装 Web 服务和页面运行时。
    requirements = (project / "requirements.txt").read_text(encoding="utf-8")
    assert "streamlit" not in requirements.casefold()
    assert "uvicorn" not in requirements.casefold()
