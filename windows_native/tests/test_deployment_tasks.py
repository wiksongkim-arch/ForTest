"""迭代部署计划持久化与选择目录测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from windows_native.jenkins.tasks import DeploymentTaskRepository
from windows_native.jenkins.scheduling import (
    next_schedule_occurrence,
    normalize_schedule,
)
from windows_native.ui.deployment_dialog import deployment_catalog


PROJECT_SNAPSHOT = {
    "last_refreshed_at": "2026-08-08T01:02:03+08:00",
    "projects": [
        {
            "full_name": "dtmzp/dtmzp_admin",
            "name": "dtmzp_admin",
            "description": "打听猫招聘总后台管理前端",
            "eligible": True,
            "environments": ["test", "staging", "prod"],
            "target_branches": ["origin/master", "origin/feature/a"],
        },
        {
            "full_name": "dtmzp/no-branch",
            "name": "no-branch",
            "description": "参数不完整",
            "eligible": False,
            "environments": ["test"],
            "target_branches": [],
        },
    ],
}


def test_catalog_groups_by_environment_and_hides_prod_by_default():
    catalog = deployment_catalog(PROJECT_SNAPSHOT)

    assert list(catalog) == ["staging", "test"]
    assert catalog["test"][0]["full_name"] == "dtmzp/dtmzp_admin"
    assert catalog["test"][0]["label"] == (
        "dtmzp/dtmzp_admin（描述：打听猫招聘总后台管理前端）"
    )
    assert deployment_catalog(PROJECT_SNAPSHOT, show_prod=True)["prod"]


def test_task_creation_uses_timestamp_id_and_persists_full_plan(tmp_path):
    now = datetime(2026, 8, 8, 1, 2, 3, tzinfo=timezone.utc)
    repository = DeploymentTaskRepository(tmp_path, clock=lambda: now)

    task = repository.create(
        "8 月迭代",
        [
            {
                "environment": "test",
                "project_full_name": "dtmzp/dtmzp_admin",
                "project_name": "dtmzp_admin",
                "description": "总后台前端",
                "branch": "origin/feature/a",
            }
        ],
    )

    assert task["task_id"] == "20260808010203"
    assert task["status"] == "queued"
    assert task["orchestration_mode"] == "direct_parallel_subtasks"
    assert task["deployment_type"] == "iteration"
    assert task["total_steps"] == 1
    assert task["items"][0]["branch"] == "origin/feature/a"
    assert task["items"][0]["subtask_id"] == "20260808010203-001"
    assert repository.list()[0] == task
    assert repository.queued_count() == 1


def test_task_ids_remain_unique_when_created_in_same_second(tmp_path):
    now = datetime(2026, 8, 8, 1, 2, 3, tzinfo=timezone.utc)
    repository = DeploymentTaskRepository(tmp_path, clock=lambda: now)
    selection = [
        {
            "environment": "test",
            "project_full_name": "dtmzp/dtm_pc",
            "branch": "origin/master",
        }
    ]

    first = repository.create("第一批", selection)
    second = repository.create("第二批", selection)

    assert first["task_id"] == "20260808010203"
    assert second["task_id"] == "20260808010204"


def test_duplicate_project_in_same_environment_is_rejected(tmp_path):
    repository = DeploymentTaskRepository(tmp_path)
    selection = {
        "environment": "test",
        "project_full_name": "dtmzp/dtm_pc",
        "branch": "origin/master",
    }
    with pytest.raises(ValueError, match="不能重复"):
        repository.create("重复任务", [selection, selection])


def test_active_task_cannot_be_moved_to_recycle_bin(tmp_path):
    repository = DeploymentTaskRepository(tmp_path)
    task = repository.create(
        "执行中任务",
        [
            {
                "environment": "test",
                "project_full_name": "dtmzp/dtm_pc",
                "branch": "origin/master",
            }
        ],
    )
    with pytest.raises(ValueError, match="请先停止"):
        repository.trash(task["task_id"])


def test_single_deployment_has_stable_type_and_requires_exactly_one_selection(tmp_path):
    repository = DeploymentTaskRepository(tmp_path)
    selection = {
        "environment": "test",
        "project_full_name": "dtmzp/dtm_pc",
        "branch": "origin/master",
    }

    task = repository.create(
        "单点部署",
        [selection],
        deployment_type="single",
    )

    assert task["iteration_name"] == "单点部署"
    assert task["deployment_type"] == "single"
    with pytest.raises(ValueError, match="只能选择一个"):
        repository.create(
            "单点部署",
            [selection, {**selection, "environment": "staging"}],
            deployment_type="single",
        )


def test_schedule_normalization_supports_minute_and_daily_modes():
    now = datetime(2026, 8, 8, 1, 0, tzinfo=timezone.utc)
    interval = normalize_schedule(
        {
            "enabled": True,
            "mode": "interval_minutes",
            "start_at": "2026-08-08T01:00+00:00",
            "end_at": "2026-08-08T02:00+00:00",
            "interval_minutes": 15,
        },
        now=now,
    )
    assert interval["next_run_at"] == "2026-08-08T01:00+00:00"
    assert next_schedule_occurrence(
        interval,
        after=now,
    ).isoformat(timespec="minutes") == "2026-08-08T01:15+00:00"

    daily = normalize_schedule(
        {
            "enabled": True,
            "mode": "daily_time",
            "start_at": "2026-08-08T09:30+00:00",
            "end_at": "2026-08-10T18:00+00:00",
            "time_of_day": "10:20",
        },
        now=datetime(2026, 8, 8, 10, 21, tzinfo=timezone.utc),
    )
    assert daily["next_run_at"] == "2026-08-09T10:20+00:00"


def test_schedule_rejects_more_than_thirty_days_and_empty_daily_range():
    start = datetime(2026, 8, 8, 1, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="30 天"):
        normalize_schedule(
            {
                "enabled": True,
                "mode": "interval_minutes",
                "start_at": start.isoformat(),
                "end_at": (start + timedelta(days=30, minutes=1)).isoformat(),
                "interval_minutes": 10,
            },
            now=start,
        )
    with pytest.raises(ValueError, match="没有可执行"):
        normalize_schedule(
            {
                "enabled": True,
                "mode": "daily_time",
                "start_at": "2026-08-08T10:00+00:00",
                "end_at": "2026-08-08T11:00+00:00",
                "time_of_day": "12:00",
            },
            now=start,
        )


def test_scheduled_task_uses_waiting_state_and_active_schedule_count(tmp_path):
    now = datetime(2026, 8, 8, 1, 0, tzinfo=timezone.utc)
    repository = DeploymentTaskRepository(tmp_path, clock=lambda: now)
    task = repository.create(
        "定时迭代",
        [
            {
                "environment": "test",
                "project_full_name": "dtmzp/dtm_pc",
                "branch": "origin/master",
            }
        ],
        schedule={
            "enabled": True,
            "mode": "interval_minutes",
            "start_at": (now + timedelta(minutes=5)).isoformat(),
            "end_at": (now + timedelta(minutes=20)).isoformat(),
            "interval_minutes": 5,
        },
    )

    assert task["schema_version"] == 3
    assert task["status"] == "scheduled"
    assert task["schedule"]["state"] == "waiting"
    assert repository.queued_count() == 0
    assert repository.scheduled_count() == 1
    assert repository.active_count() == 1
