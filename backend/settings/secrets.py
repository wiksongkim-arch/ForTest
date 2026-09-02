from __future__ import annotations

from typing import Protocol

import keyring


SERVICE_NAME = "PRDtoCASE"


class SecretStore(Protocol):
    def get(self, name: str) -> str | None:
        raise NotImplementedError

    def set(self, name: str, value: str) -> None:
        raise NotImplementedError

    def delete(self, name: str) -> None:
        raise NotImplementedError

    def list_names(self, prefix: str = "") -> list[str]:
        raise NotImplementedError


class KeyringSecretStore:
    def get(self, name: str) -> str | None:
        return keyring.get_password(SERVICE_NAME, name)

    def set(self, name: str, value: str) -> None:
        if not value:
            raise ValueError("密钥不能为空")
        keyring.set_password(SERVICE_NAME, name, value)

    def delete(self, name: str) -> None:
        try:
            keyring.delete_password(SERVICE_NAME, name)
        except keyring.errors.PasswordDeleteError:
            return

    def list_names(self, prefix: str = "") -> list[str]:
        """仅枚举本产品在 Windows 凭据库中的用户名，不读取其它凭据值。"""

        try:
            from keyring.backends.Windows import win32cred

            credentials = win32cred.CredEnumerate(None, 0)
        except Exception:
            return []
        names = {
            str(item.get("UserName") or "")
            for item in credentials
            if isinstance(item, dict)
            and (
                str(item.get("TargetName") or "") == SERVICE_NAME
                or str(item.get("TargetName") or "").endswith(f"@{SERVICE_NAME}")
            )
        }
        return sorted(name for name in names if name and name.startswith(prefix))


class MemorySecretStore:
    def __init__(self):
        self.values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)

    def list_names(self, prefix: str = "") -> list[str]:
        return sorted(name for name in self.values if name.startswith(prefix))


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:3]}{'•' * 6}{value[-4:]}"
