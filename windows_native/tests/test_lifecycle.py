"""桌面进程退出归因、异常标记与日志脱敏回归测试。"""

from __future__ import annotations

import json

from windows_native.lifecycle import (
    LifecycleDiagnostics,
    activate_lifecycle,
    lifecycle_event,
    request_application_exit,
)


def _diagnostics(tmp_path) -> LifecycleDiagnostics:
    return LifecycleDiagnostics(
        tmp_path,
        version="9.8.7",
        enable_exception_hooks=False,
        enable_fatal_handler=False,
    )


def _events(tmp_path) -> list[dict]:
    path = tmp_path / "logs" / "lifecycle.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_clean_exit_records_root_reason_and_removes_active_marker(tmp_path) -> None:
    diagnostics = _diagnostics(tmp_path)
    diagnostics.start(launch_mode="interactive")
    diagnostics.request_exit("tray_menu_quit")
    diagnostics.request_exit("window_close_accepted")
    diagnostics.finish(exit_code=0, fallback_reason="main_returned")

    assert not diagnostics.marker_path.exists()
    events = _events(tmp_path)
    requests = [item for item in events if item["event"] == "exit_requested"]
    assert [item["reason"] for item in requests] == [
        "tray_menu_quit",
        "window_close_accepted",
    ]
    assert requests[0]["first_request"] is True
    assert requests[1]["first_request"] is False
    assert requests[1]["root_reason"] == "tray_menu_quit"
    assert events[-1]["event"] == "run_finished"
    assert events[-1]["reason"] == "tray_menu_quit"


def test_next_start_reports_marker_left_by_unclean_previous_run(tmp_path) -> None:
    marker = tmp_path / "data" / "active-session.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps(
            {
                "run_id": "previous-run",
                "pid": 4321,
                "started_at": "2026-09-01T01:02:03+00:00",
                "last_seen_at": "2026-09-01T05:06:07+00:00",
                "version": "1.2.3",
                "application_state": "Inactive",
                "window_visible": False,
            }
        ),
        encoding="utf-8",
    )

    diagnostics = _diagnostics(tmp_path)
    previous = diagnostics.start(launch_mode="autostart")
    diagnostics.finish(exit_code=0, fallback_reason="test_complete")

    assert previous is not None
    event = next(
        item for item in _events(tmp_path) if item["event"] == "previous_run_unclean"
    )
    assert event["previous_run_id"] == "previous-run"
    assert event["previous_pid"] == 4321
    assert event["previous_version"] == "1.2.3"
    assert event["previous_last_seen_at"] == "2026-09-01T05:06:07+00:00"
    assert event["previous_application_state"] == "Inactive"
    assert event["previous_window_visible"] is False


def test_heartbeat_updates_last_seen_window_state_without_log_spam(tmp_path) -> None:
    diagnostics = _diagnostics(tmp_path)
    diagnostics.start(launch_mode="interactive")
    before = len(_events(tmp_path))

    diagnostics.heartbeat(
        application_state="Active",
        window_visible=True,
        window_minimized=False,
        tray_usable=True,
    )

    marker = json.loads(diagnostics.marker_path.read_text(encoding="utf-8"))
    assert marker["last_seen_at"] >= marker["started_at"]
    assert marker["application_state"] == "Active"
    assert marker["window_visible"] is True
    assert marker["window_minimized"] is False
    assert marker["tray_usable"] is True
    assert len(_events(tmp_path)) == before
    diagnostics.finish(exit_code=0, fallback_reason="test_complete")


def test_global_helpers_route_events_to_active_run(tmp_path) -> None:
    diagnostics = _diagnostics(tmp_path)
    diagnostics.start(launch_mode="interactive")
    activate_lifecycle(diagnostics)

    lifecycle_event("qt_about_to_quit", reason="not_recorded")
    request_application_exit("windows_session_end")
    diagnostics.finish(exit_code=0, fallback_reason="main_returned")

    events = _events(tmp_path)
    assert any(item["event"] == "qt_about_to_quit" for item in events)
    assert events[-1]["reason"] == "windows_session_end"


def test_exception_text_is_redacted_and_bounded(tmp_path) -> None:
    diagnostics = _diagnostics(tmp_path)
    diagnostics.start(launch_mode="interactive")
    secret = "private" + "-token-value"
    exception = RuntimeError(f"token={secret}")
    diagnostics.record_exception(
        "uncaught_exception",
        type(exception),
        exception,
        exception.__traceback__,
        context="test",
    )
    diagnostics.finish(exit_code=1, fallback_reason="test_complete")

    content = diagnostics.log_path.read_text(encoding="utf-8")
    assert secret not in content
    event = next(
        item for item in _events(tmp_path) if item["event"] == "uncaught_exception"
    )
    assert event["exception_type"] == "RuntimeError"
    assert event["context"] == "test"
