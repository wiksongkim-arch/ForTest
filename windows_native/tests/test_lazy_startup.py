"""首屏不得被完整业务依赖阻塞。"""

import os
import subprocess
import sys
from pathlib import Path


def test_translation_reverse_lookup_uses_precompiled_cache():
    from windows_native.i18n import (
        _cached_source_text_and_values,
        _source_text_and_values,
        _translation_indexes,
    )

    _cached_source_text_and_values.cache_clear()
    source, values = _source_text_and_values("可部署项目：12")
    assert source == "可部署项目：{count}"
    assert values == {"count": "12"}

    before = _cached_source_text_and_values.cache_info()
    for _index in range(200):
        assert _source_text_and_values("可部署项目：12")[0] == source
    after = _cached_source_text_and_values.cache_info()
    assert after.hits - before.hits == 200

    exact_sources, dynamic_templates = _translation_indexes()
    # 动态模板远少于静态词条，控件翻译不再逐项扫描整个目录。
    assert len(dynamic_templates) < len(exact_sources)


def test_main_window_import_does_not_load_backend_routes():
    project_root = Path(__file__).resolve().parents[2]
    code = (
        "import sys; "
        "import windows_native.ui.main_window; "
        "assert 'backend.api.routes' not in sys.modules; "
        "assert 'windows_native.jenkins.service' not in sys.modules; "
        "print('lazy-ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "lazy-ok" in result.stdout


def test_single_instance_import_does_not_load_main_window():
    project_root = Path(__file__).resolve().parents[2]
    code = (
        "import sys; "
        "import windows_native.single_instance; "
        "assert 'windows_native.ui.main_window' not in sys.modules; "
        "print('single-instance-lightweight')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "single-instance-lightweight" in result.stdout


def test_unused_jenkins_proxy_stops_without_loading_service(tmp_path):
    from windows_native.lazy_service import LazyJenkinsDeploymentService

    proxy = LazyJenkinsDeploymentService(tmp_path)
    proxy.stop()
    assert proxy._instance is None


def test_main_window_construction_stays_responsive():
    project_root = Path(__file__).resolve().parents[2]
    code = """
import os
import time
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication
from windows_native.tests.test_ui_workflow import (
    FakeDeploymentService,
    FakeService,
    FakeTaskManager,
    FakeThemeManager,
)
from windows_native.ui.main_window import MainWindow
app = QApplication.instance() or QApplication([])
started = time.perf_counter()
window = MainWindow(
    FakeService(),
    QIcon(),
    FakeTaskManager(),
    FakeThemeManager(),
    FakeDeploymentService(),
    onboarding_enabled=False,
)
elapsed = time.perf_counter() - started
assert elapsed < 1.0, elapsed
window.close()
print(f'responsive:{elapsed:.3f}')
"""
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "responsive:" in result.stdout
