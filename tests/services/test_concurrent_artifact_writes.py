"""多任务模式下共享缓存与同名本地输出的并发安全测试。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import Mock

from services.dingtalk_output import DingTalkOutputWriter
from utils.template_loader import TemplateLoader


def test_template_cache_concurrent_writes_are_always_complete(tmp_path: Path):
    cache_path = tmp_path / "output" / "test_case_template.json"
    loader = TemplateLoader(
        "https://alidocs.dingtalk.com/i/nodes/template",
        Mock(),
        cache_path=str(cache_path),
    )
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def write(index: int) -> None:
        try:
            barrier.wait(timeout=2)
            for sequence in range(8):
                loader._save_to_cache(
                    {
                        "writer": index,
                        "sequence": sequence,
                        "components": {"按钮": [str(index)] * 100},
                    }
                )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["writer"] in range(8)
    assert len(payload["components"]["按钮"]) == 100
    assert not list(cache_path.parent.glob(".*.tmp"))


def test_same_title_local_backups_are_serialized_and_atomically_replaced(
    tmp_path: Path,
):
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0

    class RecordingWriter:
        def __init__(self):
            self.cases = []

        def add_test_cases(self, cases):
            self.cases.extend(cases)

        def write_to_excel(self, path):
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                time.sleep(0.03)
                Path(path).write_text(
                    json.dumps(self.cases, ensure_ascii=False),
                    encoding="utf-8",
                )
            finally:
                with state_lock:
                    active -= 1

    writer = DingTalkOutputWriter(
        document_service=Mock(),
        spreadsheet_service=Mock(),
        document_template_url="https://alidocs.dingtalk.com/i/nodes/template",
        output_folder_url="https://alidocs.dingtalk.com/i/nodes/folder",
        excel_writer_factory=RecordingWriter,
        lock_dir=tmp_path / "locks",
    )
    destination = tmp_path / "output" / "同名需求-用例.xlsx"
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def save(value: str) -> None:
        try:
            barrier.wait(timeout=2)
            writer._write_local_backup(destination, [{"case_name": value}])
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=save, args=(value,)) for value in ("甲", "乙")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert maximum_active == 1
    assert json.loads(destination.read_text(encoding="utf-8"))[0]["case_name"] in {
        "甲",
        "乙",
    }
    assert not list(destination.parent.glob(".*.tmp.xlsx"))
