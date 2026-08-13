import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import requests


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES = 5
MAX_REDIRECTS = 3
_CHUNK_SIZE = 64 * 1024
_MEDIA_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


class ImageDownloadError(RuntimeError):
    """An image could not be downloaded under the required safety policy."""


class ImageWorkspace:
    def __init__(self, session: Any | None = None):
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="prd-to-case-images-"
        )
        self.path = Path(self._temporary_directory.name)
        self._owns_session = session is None
        self._session = session if session is not None else requests.Session()
        self._closed = False

    def download(self, url: str) -> Path:
        self._ensure_open()
        self._require_https(url)

        response = None
        destination: Path | None = None
        try:
            response = self._session.get(
                url,
                allow_redirects=True,
                timeout=(5, 30),
                verify=True,
                stream=True,
            )
            self._validate_response_urls(response)
            response.raise_for_status()

            media_type = self._validated_media_type(response.headers)
            self._validate_declared_size(response.headers)
            destination = self.path / f"{uuid4().hex}{_MEDIA_SUFFIXES[media_type]}"
            self._write_response(response, destination)
            return destination
        except ImageDownloadError:
            self._remove_partial(destination)
            raise
        except Exception:
            self._remove_partial(destination)
            raise ImageDownloadError("Image download failed.") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def download_many(self, urls: tuple[str, ...]) -> tuple[Path, ...]:
        if len(urls) > MAX_IMAGES:
            raise ImageDownloadError("At most five images may be downloaded.")
        self._ensure_open()
        for url in urls:
            self._require_https(url)
        return tuple(self.download(url) for url in urls)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_session:
            try:
                self._session.close()
            except Exception:
                pass
        try:
            self._temporary_directory.cleanup()
        except Exception:
            pass

    def _validate_response_urls(self, response: Any) -> None:
        history = tuple(response.history)
        if len(history) > MAX_REDIRECTS:
            raise ImageDownloadError("Image redirect limit exceeded.")
        for redirect in history:
            self._require_https(redirect.url)
        self._require_https(response.url)

    @staticmethod
    def _validated_media_type(headers: Any) -> str:
        content_type = headers.get("Content-Type")
        if not isinstance(content_type, str):
            raise ImageDownloadError("Image content type is not allowed.")
        media_type = content_type.partition(";")[0].strip().lower()
        if media_type not in _MEDIA_SUFFIXES:
            raise ImageDownloadError("Image content type is not allowed.")
        return media_type

    @staticmethod
    def _validate_declared_size(headers: Any) -> None:
        value = headers.get("Content-Length")
        if value is None:
            return
        try:
            size = int(value)
        except (TypeError, ValueError):
            raise ImageDownloadError("Image size is invalid.") from None
        if size < 0 or size > MAX_IMAGE_BYTES:
            raise ImageDownloadError("Image size exceeds the allowed limit.")

    @staticmethod
    def _write_response(response: Any, destination: Path) -> None:
        total_bytes = 0
        with destination.open("xb") as output:
            for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                if not chunk:
                    continue
                total_bytes += len(chunk)
                if total_bytes > MAX_IMAGE_BYTES:
                    raise ImageDownloadError(
                        "Image size exceeds the allowed limit."
                    )
                output.write(chunk)

    @staticmethod
    def _remove_partial(destination: Path | None) -> None:
        if destination is None:
            return
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass

    def _ensure_open(self) -> None:
        if self._closed:
            raise ImageDownloadError("Image workspace is closed.")

    @staticmethod
    def _require_https(url: str) -> None:
        try:
            parsed = urlsplit(url)
        except (AttributeError, TypeError, ValueError):
            raise ImageDownloadError("Image URL must use HTTPS.") from None
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ImageDownloadError("Image URL must use HTTPS.")
