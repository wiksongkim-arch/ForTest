"""旧版残留进程识别逻辑的安全边界测试。"""

from pathlib import Path

import windows_native.legacy_cleanup as cleanup


def test_cleanup_source_requires_exact_legacy_service_signature():
    source = (
        Path(__file__).resolve().parents[1] / "legacy_cleanup.py"
    ).read_text(encoding="utf-8")
    assert '"prdtocase.exe"' in source
    assert '"--service"' in source
    assert '{"backend", "frontend"}' in source
    assert "process_iter" in source


class _FakeProcess:
    def __init__(self, pid: int, executable: Path, command: list[str]):
        self.pid = pid
        self.info = {"pid": pid, "exe": str(executable), "cmdline": command}
        self.terminated = False

    def children(self, recursive: bool = False):
        return []

    def terminate(self):
        self.terminated = True


def test_previous_native_cleanup_only_targets_exact_old_install(monkeypatch, tmp_path):
    expected = tmp_path / "Programs" / "PRDtoCASE" / "PRDtoCASE.exe"
    previous_brand = tmp_path / "Programs" / "ForTester" / "ForTester.exe"
    immediate_previous = tmp_path / "Programs" / "QAQ" / "QAQ.exe"
    exact = _FakeProcess(12345, expected, [str(expected)])
    previous = _FakeProcess(12344, previous_brand, [str(previous_brand)])
    immediate = _FakeProcess(12343, immediate_previous, [str(immediate_previous)])
    unrelated = _FakeProcess(12346, tmp_path / "Other" / "PRDtoCASE.exe", ["PRDtoCASE.exe"])
    service = _FakeProcess(12347, expected, [str(expected), "--service", "backend"])
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(
        cleanup.psutil,
        "process_iter",
        lambda _fields: [immediate, previous, exact, unrelated, service],
    )
    monkeypatch.setattr(cleanup.psutil, "wait_procs", lambda processes, timeout: (processes, []))

    stopped = cleanup.cleanup_previous_native_application()

    assert stopped == [12343, 12344, 12345]
    assert immediate.terminated
    assert previous.terminated
    assert exact.terminated
    assert not unrelated.terminated
    assert not service.terminated
