"""安装包隐私门禁的无明文报告与源码约束测试。"""

from __future__ import annotations

from pathlib import Path

from windows_native.package_privacy import (
    SensitiveCandidate,
    _scan_code_object,
    audit_source,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_current_source_defaults_and_package_inputs_pass_privacy_gate():
    assert audit_source(PROJECT_ROOT) == []


def test_local_development_process_records_are_gitignored():
    ignored_patterns = {
        line.strip()
        for line in (PROJECT_ROOT / ".gitignore").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {
        ".codex/",
        ".codex-work/",
        ".p/",
        ".superpowers/",
        "docs/plans/",
        "docs/validation/",
        "docs/reports/",
        "docs/superpowers/",
        "docs/security/open-source-privacy-audit-*.md",
        "windows_native/DELIVERY*.md",
        "预删除/",
    } <= ignored_patterns


def test_retired_prd_to_case_plugin_is_not_active_or_called_by_build():
    build = (PROJECT_ROOT / "windows_native" / "build.ps1").read_text(
        encoding="utf-8"
    )

    assert not (PROJECT_ROOT / "plugins").exists()
    assert "plugins\\prd-to-case" not in build
    assert "plugin regression" not in build.casefold()


def test_code_audit_detects_secret_and_personal_path_without_reporting_value():
    # 运行时拼接可继续覆盖密钥检测，同时避免静态扫描器把测试夹具误判为真实凭据。
    secret = "sk-" + "test-private-value-123456789"
    code = compile(
        f"value = {secret!r}",
        r"C:\Users\private-user\workspace\module.py",
        "exec",
    )
    findings = _scan_code_object(
        "backend.example",
        code,
        [SensitiveCandidate("test-keyring-slot", secret, "secret")],
    )

    assert {item.code for item in findings} >= {
        "absolute-build-path",
        "embedded-secret",
        "hardcoded-secret-shape",
    }
    rendered = repr(findings)
    assert secret not in rendered
    assert "private-user" not in rendered


def test_sensitive_candidate_repr_never_contains_plaintext():
    secret = "opaque-private-token-value"
    candidate = SensitiveCandidate("environment:TEST_TOKEN", secret, "secret")

    assert secret not in repr(candidate)


def test_native_build_runs_source_and_artifact_privacy_gates():
    build = (PROJECT_ROOT / "windows_native" / "build.ps1").read_text(
        encoding="utf-8"
    )
    spec = (PROJECT_ROOT / "windows_native" / "ForTest.spec").read_text(
        encoding="utf-8"
    )

    assert "package_privacy.py') source" in build
    assert "package_privacy.py') artifact" in build
    assert "include_py_files=False" in spec


def test_installer_cleans_stale_frozen_runtime_and_build_smokes_local_backup():
    installer = (PROJECT_ROOT / "windows_native" / "installer.iss").read_text(
        encoding="utf-8"
    )
    build = (PROJECT_ROOT / "windows_native" / "build.ps1").read_text(
        encoding="utf-8"
    )

    assert 'Type: filesandordirs; Name: "{app}\\_internal"' in installer
    assert "--backup-smoke-test" in build
    assert "packaged-backup-diagnostics.json" in build
