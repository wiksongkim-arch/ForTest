import ipaddress
import socket
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import (
    ConnectTimeoutError,
    NameResolutionError,
    NewConnectionError,
)


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES = 5
MAX_REDIRECTS = 3
DEFAULT_RETRY_DELAYS_SECONDS = (0.25, 0.75)
_CHUNK_SIZE = 64 * 1024
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_MEDIA_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


class ImageDownloadError(RuntimeError):
    """An image could not be downloaded under the required safety policy."""


class _PinnedHTTPSConnection(HTTPSConnection):
    """TLS 仍校验原主机名，但 TCP 只能连接到已经审查过的地址。"""

    def __init__(self, *args, pinned_addresses: tuple[str, ...], **kwargs):
        super().__init__(*args, **kwargs)
        self._pinned_addresses = pinned_addresses

    def _new_conn(self):
        last_error: Exception | None = None
        original_dns_host = self._dns_host
        for address in self._pinned_addresses:
            try:
                # urllib3 的 TLS/SNI 仍使用恢复后的原主机；这里只固定 TCP 目的地址。
                self._dns_host = address
                return super()._new_conn()
            except (ConnectTimeoutError, NameResolutionError, NewConnectionError) as exc:
                last_error = exc
            finally:
                self._dns_host = original_dns_host
        if last_error is not None:
            raise last_error
        raise NewConnectionError(self, "No validated image address is available")


class _PinnedHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _PinnedHTTPSConnection

    def __init__(
        self,
        host: str,
        port: int | None,
        *,
        pinned_addresses: tuple[str, ...],
        **kwargs,
    ):
        super().__init__(host, port, **kwargs)
        self.conn_kw["pinned_addresses"] = pinned_addresses


class _PinnedHTTPSAdapter(HTTPAdapter):
    """为单次图片请求创建不复用 DNS 的 HTTPS 连接池。"""

    def __init__(
        self,
        hostname: str,
        port: int,
        addresses: tuple[str, ...],
    ):
        self._hostname = hostname.casefold().rstrip(".")
        self._port = int(port)
        self._addresses = addresses
        super().__init__(max_retries=0)

    def _connection_pool(self, url: str, **pool_kwargs):
        parsed = urlsplit(url)
        hostname = str(parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port or 443
        if (
            parsed.scheme.lower() != "https"
            or hostname != self._hostname
            or port != self._port
        ):
            raise requests.exceptions.InvalidURL(
                "Image request no longer matches the validated target."
            )
        return _PinnedHTTPSConnectionPool(
            hostname,
            port,
            pinned_addresses=self._addresses,
            **pool_kwargs,
        )

    def get_connection_with_tls_context(
        self,
        request,
        verify,
        proxies=None,
        cert=None,
    ):
        """兼容新版 requests，并显式忽略可能改变解析边界的代理。"""

        _host_params, pool_kwargs = self.build_connection_pool_key_attributes(
            request,
            verify,
            cert,
        )
        return self._connection_pool(request.url, **pool_kwargs)

    def get_connection(self, url, proxies=None):
        """兼容 requests 2.31 的旧连接入口。"""

        return self._connection_pool(url)


class ImageWorkspace:
    def __init__(
        self,
        session: Any | None = None,
        *,
        retry_delays_seconds: tuple[float, ...] = DEFAULT_RETRY_DELAYS_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        resolver: Callable[..., Any] = socket.getaddrinfo,
    ):
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="prd-to-case-images-"
        )
        self.path = Path(self._temporary_directory.name)
        self._owns_session = session is None
        self._session = session if session is not None else requests.Session()
        self._retry_delays_seconds = tuple(
            max(0.0, float(value)) for value in retry_delays_seconds
        )
        self._sleep = sleep
        self._resolver = resolver
        self._closed = False

    def download(self, url: str) -> Path:
        self._ensure_open()
        self._require_https(url)

        response = None
        transport = None
        destination: Path | None = None
        try:
            current_url = url
            redirect_count = 0
            while True:
                # 每次真正联网前都重新解析目标；自动重定向会绕过这一安全边界。
                hostname, port, addresses = self._validated_public_network_target(
                    current_url
                )
                response, transport = self._request_image(
                    current_url,
                    hostname=hostname,
                    port=port,
                    addresses=addresses,
                )
                status = int(response.status_code)
                if status not in _REDIRECT_STATUS_CODES:
                    break
                if redirect_count >= MAX_REDIRECTS:
                    raise ImageDownloadError("Image redirect limit exceeded.")
                location = response.headers.get("Location")
                if not isinstance(location, str) or not location.strip():
                    raise ImageDownloadError("Image redirect target is invalid.")
                next_url = urljoin(current_url, location.strip())
                self._close_response(response)
                self._close_transport(transport)
                response = None
                transport = None
                self._require_https(next_url)
                current_url = next_url
                redirect_count += 1

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
            self._close_response(response)
            self._close_transport(transport)

    def download_many(self, urls: tuple[str, ...]) -> tuple[Path, ...]:
        if len(urls) > MAX_IMAGES:
            raise ImageDownloadError("At most five images may be downloaded.")
        self._ensure_open()
        for url in urls:
            self._require_https(url)

        downloaded: list[Path] = []
        failed = 0
        for url in urls:
            for attempt in range(len(self._retry_delays_seconds) + 1):
                try:
                    downloaded.append(self.download(url))
                    break
                except ImageDownloadError:
                    if attempt >= len(self._retry_delays_seconds):
                        failed += 1
                        break
                    # 短暂网络抖动或签名资源尚未就绪时做有限退避；日志不得包含地址。
                    self._sleep(self._retry_delays_seconds[attempt])

        # 单张失败不再丢弃同区块已经下载成功的图片；全部失败仍保持显式异常。
        if failed and not downloaded:
            raise ImageDownloadError("Image batch download failed.")
        return tuple(downloaded)

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

    @staticmethod
    def _close_response(response: Any | None) -> None:
        if response is None:
            return
        try:
            response.close()
        except Exception:
            pass

    @staticmethod
    def _close_transport(transport: Any | None) -> None:
        if transport is None:
            return
        try:
            transport.close()
        except Exception:
            pass

    def _ensure_open(self) -> None:
        if self._closed:
            raise ImageDownloadError("Image workspace is closed.")

    def _validated_public_network_target(
        self,
        url: str,
    ) -> tuple[str, int, tuple[str, ...]]:
        """拒绝回环、私网、链路本地等不可公开路由的图片目标。"""

        self._require_https(url)
        parsed = urlsplit(url)
        hostname = str(parsed.hostname or "")
        if "%" in hostname:
            raise ImageDownloadError("Image URL target is not allowed.")
        try:
            ascii_hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            raise ImageDownloadError("Image URL target is not allowed.") from None
        try:
            port = parsed.port or 443
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            literal = None

        if literal is not None:
            addresses = (literal,)
        else:
            try:
                records = self._resolver(
                    ascii_hostname,
                    port,
                    type=socket.SOCK_STREAM,
                )
                addresses = tuple(
                    ipaddress.ip_address(str(record[4][0])) for record in records
                )
            except Exception:
                raise ImageDownloadError(
                    "Image URL target could not be validated."
                ) from None

        # 只要 DNS 返回任一内部地址就整体拒绝，避免地址轮换绕过目标策略。
        if not addresses or any(not address.is_global for address in addresses):
            raise ImageDownloadError("Image URL target is not allowed.")
        return ascii_hostname, port, tuple(str(address) for address in addresses)

    def _request_image(
        self,
        url: str,
        *,
        hostname: str,
        port: int,
        addresses: tuple[str, ...],
    ) -> tuple[Any, Any | None]:
        if not isinstance(self._session, requests.Session):
            return (
                self._session.get(
                    url,
                    allow_redirects=False,
                    timeout=(5, 30),
                    verify=True,
                    stream=True,
                ),
                None,
            )

        adapter = _PinnedHTTPSAdapter(hostname, port, addresses)
        prepared = self._session.prepare_request(requests.Request("GET", url))
        try:
            response = adapter.send(
                prepared,
                stream=True,
                timeout=(5, 30),
                verify=True,
                cert=None,
            )
        except Exception:
            adapter.close()
            raise
        return response, adapter

    @staticmethod
    def _require_https(url: str) -> None:
        try:
            parsed = urlsplit(url)
            _ = parsed.port
        except (AttributeError, TypeError, ValueError):
            raise ImageDownloadError("Image URL must use HTTPS.") from None
        if (
            not isinstance(url, str)
            or any(
                character == "\\"
                or ord(character) <= 0x20
                or ord(character) == 0x7F
                for character in url
            )
            or parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ImageDownloadError("Image URL must use HTTPS.")
