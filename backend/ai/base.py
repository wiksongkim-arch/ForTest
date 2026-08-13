from typing import Protocol

from backend.ai.types import ProviderHealth, SectionAIRequest, SectionAIResult
from backend.settings.models import ProviderName


class ProviderError(RuntimeError):
    """Base class for safe AI Provider failures."""


class ProviderUnavailableError(ProviderError):
    """Provider cannot start or pass preflight."""


class ProviderResponseError(ProviderError):
    """Provider started but returned an invalid/incomplete response."""


class AIProvider(Protocol):
    name: ProviderName

    def health_check(self) -> ProviderHealth:
        raise NotImplementedError

    def process_section(self, request: SectionAIRequest) -> SectionAIResult:
        raise NotImplementedError

    def cancel(self) -> None:
        """尽快中止当前调用；实现必须允许从其它线程安全触发。"""

        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError
