"""EIM supervisor 的共享 consumer 与逐任务停止语义。"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from backend.eim.models import (
    ConnectionState,
    DestinationType,
    EIMConnection,
    EIMDestination,
    EIMTask,
    ObservedState,
)
from backend.eim.repository import EIMRepository
from backend.eim.runtime.supervisor import EIMSupervisor


class _Subscription:
    created = 0

    def __init__(self, *_args: Any, **_kwargs: Any):
        type(self).created += 1
        self.exited = threading.Event()
        self.stopped = False

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self.stopped = True
        self.exited.set()


def _wait_state(repository: EIMRepository, task_id: str, state: ObservedState) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if repository.get_task(task_id).observed_state is state:
            return
        time.sleep(0.01)
    raise AssertionError(f"任务未进入 {state}")


def test_same_connection_group_and_events_share_one_consumer(tmp_path: Path, monkeypatch) -> None:
    repository = EIMRepository(tmp_path / "eim.db")
    connection = repository.save_connection(
        EIMConnection(
            config_dir_ref="isolated",
            profile="corp:user",
            connection_state=ConnectionState.CONNECTED,
        )
    )
    destination = repository.save_destination(
        EIMDestination(
            connection_id=connection.connection_id,
            destination_type=DestinationType.DINGTALK_DOC,
            url="https://alidocs.dingtalk.com/i/nodes/shared",
        )
    )
    tasks = [
        repository.create_task(
            EIMTask(
                name=f"共享监听 {index}",
                connection_id=connection.connection_id,
                source_id="cid-shared",
                source_name="共享群",
                destination_id=destination.destination_id,
            )
        )
        for index in range(2)
    ]
    supervisor = EIMSupervisor(repository, object(), object())  # type: ignore[arg-type]
    monkeypatch.setattr("backend.eim.runtime.supervisor.DingTalkSubscription", _Subscription)
    monkeypatch.setattr(supervisor, "_preflight", repository.get_task)
    monkeypatch.setattr(repository, "start_run", lambda task_id, _version_id: f"run-{task_id}")
    monkeypatch.setattr(repository, "heartbeat_run", lambda _run_id: None)
    monkeypatch.setattr(repository, "stop_run", lambda _run_id, _reason: None)
    _Subscription.created = 0

    supervisor.start_task(tasks[0].task_id)
    _wait_state(repository, tasks[0].task_id, ObservedState.RUNNING)
    supervisor.start_task(tasks[1].task_id)
    _wait_state(repository, tasks[1].task_id, ObservedState.RUNNING)
    assert _Subscription.created == 1
    consumer = next(iter(supervisor._consumers.values()))
    assert consumer.task_ids == {tasks[0].task_id, tasks[1].task_id}

    supervisor.stop_task(tasks[0].task_id)
    assert consumer.subscription and not consumer.subscription.stopped
    supervisor.stop_task(tasks[1].task_id)
    assert consumer.subscription.stopped
    assert not supervisor._consumers
    supervisor.stop_all()
