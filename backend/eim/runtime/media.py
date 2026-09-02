"""DWS 消息媒体的受限下载、哈希命名与本地保留。"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from backend.eim.models import CanonicalEvent, EIMConnection, MediaAsset, MediaPolicy


_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "video/mp4": ".mp4",
}


class MediaManager:
    def __init__(self, data_root: Path, runtime: Any):
        self.data_root = Path(data_root).resolve()
        self.root = self.data_root / "eim" / "media"
        self.runtime = runtime

    def download(
        self,
        task_id: str,
        event: CanonicalEvent,
        connection: EIMConnection,
        policy: MediaPolicy,
    ) -> CanonicalEvent:
        if not event.media_assets:
            return event
        if policy.archive_as == "link":
            if any(not self._safe_link(asset.stable_url) for asset in event.media_assets):
                raise ValueError("链接归档要求每个媒体都提供安全的 HTTPS 稳定链接")
            return event
        if not policy.download:
            return event
        if not event.message_id:
            raise ValueError("媒体事件缺少稳定 message_id，无法安全下载")
        unknown: dict[tuple[str, str], set[str]] = {}
        downloaded_bytes = 0
        retained = []
        for asset in event.media_assets:
            if (
                not asset.local_path
                and asset.resource_id
                and asset.size is None
                and not (policy.archive_as == "auto" and self._safe_link(asset.stable_url))
            ):
                source = (
                    asset.message_id or event.message_id,
                    asset.conversation_id or event.conversation_id,
                )
                unknown.setdefault(source, set()).add(asset.resource_id)
            else:
                retained.append(asset)
        if unknown:
            directory = self._event_directory(task_id, event.event_id)
            directory.mkdir(parents=True, exist_ok=True)
            downloaded = []
            for (message_id, conversation_id), resource_ids in unknown.items():
                assets = self._download_message_resources(
                    directory,
                    message_id=message_id,
                    conversation_id=conversation_id,
                    resource_ids=resource_ids,
                    connection=connection,
                    max_bytes=policy.max_bytes - downloaded_bytes,
                )
                downloaded.extend(assets)
                downloaded_bytes += sum(int(asset.size or 0) for asset in assets)
            event = event.model_copy(update={"media_assets": [*retained, *downloaded]})
        downloadable = []
        for asset in event.media_assets:
            if asset.local_path or not asset.resource_id:
                continue
            if asset.size is None:
                if policy.archive_as == "auto" and self._safe_link(asset.stable_url):
                    continue
                # DWS 下载接口不能流式限额，未知大小必须在写盘前失败关闭。
                raise ValueError("媒体缺少可校验大小，无法在任务限额内安全下载")
            downloadable.append(asset)
        if downloaded_bytes + sum(int(asset.size or 0) for asset in downloadable) > policy.max_bytes:
            raise ValueError("媒体合计大小超过任务限制")
        directory = self._event_directory(task_id, event.event_id)
        directory.mkdir(parents=True, exist_ok=True)
        for index, asset in enumerate(event.media_assets):
            if asset.local_path or not asset.resource_id:
                continue
            if asset not in downloadable:
                continue
            if asset.size is not None and asset.size > policy.max_bytes:
                raise ValueError(f"媒体 {asset.file_name or index + 1} 超过任务大小限制")
            temporary = directory / f".{index}.download"
            temporary.unlink(missing_ok=True)
            try:
                self.runtime.run_json(
                    [
                        "chat",
                        "message",
                        "download-media",
                        "--type",
                        "mediaId",
                        "--resource-id",
                        asset.resource_id,
                        "--message-id",
                        asset.message_id or event.message_id,
                        "--open-conversation-id",
                        asset.conversation_id or event.conversation_id,
                        "--output",
                        temporary.relative_to(self.data_root).as_posix(),
                        "--profile",
                        connection.profile,
                    ],
                    config_dir=self.runtime.config_dir(connection.connection_id),
                    timeout=120,
                )
                if not temporary.is_file() or temporary.is_symlink():
                    raise ValueError("DWS 未生成安全的媒体文件")
                size = temporary.stat().st_size
                downloaded_bytes += size
                if size <= 0 or downloaded_bytes > policy.max_bytes:
                    raise ValueError("下载媒体为空或超过任务大小限制")
                digest = self._sha256(temporary)
                extension = _MIME_EXTENSIONS.get(asset.mime_type.casefold(), ".bin")
                target = directory / f"{digest[:24]}{extension}"
                os.replace(temporary, target)
                relative = target.relative_to(self.data_root).as_posix()
                asset.size = size
                asset.sha256 = digest
                asset.local_path = relative
            finally:
                temporary.unlink(missing_ok=True)
        return event

    def _download_message_resources(
        self,
        directory: Path,
        *,
        message_id: str,
        conversation_id: str,
        resource_ids: set[str],
        connection: EIMConnection,
        max_bytes: int,
    ) -> list[MediaAsset]:
        """按真实消息回查下载媒体，并把下载账本约束在当前事件目录。"""

        if not message_id or not conversation_id or max_bytes <= 0:
            raise ValueError("媒体消息缺少稳定上下文或已超过任务大小限制")
        staging = Path(tempfile.mkdtemp(prefix=".message-", dir=directory)).resolve()
        try:
            value = self.runtime.run_json(
                [
                    "chat",
                    "+messages-mget",
                    "--msg-ids",
                    message_id,
                    "--download-resources",
                    "--output-dir",
                    staging.relative_to(self.data_root).as_posix(),
                    "--profile",
                    connection.profile,
                ],
                config_dir=self.runtime.config_dir(connection.connection_id),
                timeout=120,
            )
            ledger = value.get("resourceDownloads")
            downloads = ledger.get("downloads") if isinstance(ledger, dict) else None
            if (
                value.get("complete") is not True
                or not isinstance(downloads, list)
                or ledger.get("ok") is not True
                or ledger.get("partial") is True
                or int(ledger.get("failedCount") or 0)
            ):
                raise ValueError("DWS 媒体回查或下载不完整")
            result = []
            total = 0
            found: set[str] = set()
            for index, item in enumerate(downloads):
                if not isinstance(item, dict):
                    raise ValueError("DWS 媒体下载账本格式无效")
                resource_id = str(item.get("resourceId") or "")
                if resource_id not in resource_ids:
                    continue
                relative = Path(str(item.get("localPath") or ""))
                if relative.is_absolute():
                    raise ValueError("DWS 媒体下载路径不是安全相对路径")
                source = (self.data_root / relative).resolve()
                if staging not in source.parents or not source.is_file() or source.is_symlink():
                    raise ValueError("DWS 媒体下载文件越界或不是普通文件")
                size = source.stat().st_size
                if size <= 0 or size != int(item.get("sizeBytes") or 0):
                    raise ValueError("DWS 媒体下载大小校验失败")
                total += size
                if total > max_bytes:
                    raise ValueError("媒体合计大小超过任务限制")
                digest = self._sha256(source)
                suffix = source.suffix.casefold()
                if not suffix[1:].isalnum() or len(suffix) > 11:
                    suffix = ".bin"
                target = directory / f"{digest[:24]}{suffix}"
                os.replace(source, target)
                mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                result.append(
                    MediaAsset(
                        resource_id=resource_id,
                        message_id=message_id,
                        conversation_id=conversation_id,
                        file_name=f"eim-media-{index + 1}{suffix}",
                        mime_type=mime_type,
                        size=size,
                        sha256=digest,
                        local_path=target.relative_to(self.data_root).as_posix(),
                    )
                )
                found.add(resource_id)
            if found != resource_ids:
                raise ValueError("DWS 回查未下载全部媒体资源")
            return result
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _safe_link(value: str | None) -> bool:
        try:
            parsed = urlsplit(str(value or "").strip())
            return bool(
                parsed.scheme == "https"
                and parsed.hostname
                and not parsed.username
                and not parsed.password
            )
        except ValueError:
            return False

    def cleanup(self, *, before: datetime) -> int:
        """只删除媒体根目录下的普通旧文件，并保留仍有内容的目录。"""

        if not self.root.is_dir():
            return 0
        boundary = before.astimezone(UTC).timestamp()
        removed = 0
        for path in self.root.glob("*/*/*"):
            try:
                if path.is_file() and not path.is_symlink() and path.stat().st_mtime < boundary:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        for directory in sorted(self.root.glob("*/*"), reverse=True):
            try:
                directory.rmdir()
                directory.parent.rmdir()
            except OSError:
                continue
        return removed

    def cleanup_event(self, task_id: str, event_id: str, *, before: datetime) -> int:
        """按事件投递状态清理哈希目录，不接收外部路径或通配目标。"""

        task_key = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:24]
        event_key = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:24]
        event_root = (self.root / task_key / event_key).resolve()
        if self.root.resolve() not in event_root.parents or not event_root.is_dir():
            return 0
        boundary = before.astimezone(UTC).timestamp()
        removed = 0
        for path in event_root.glob("*"):
            try:
                if path.is_file() and not path.is_symlink() and path.stat().st_mtime < boundary:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        try:
            event_root.rmdir()
            event_root.parent.rmdir()
        except OSError:
            pass
        return removed

    def _event_directory(self, task_id: str, event_id: str) -> Path:
        task_key = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:24]
        event_key = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:24]
        path = (self.root / task_key / event_key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("EIM 媒体目录越界")
        return path

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
