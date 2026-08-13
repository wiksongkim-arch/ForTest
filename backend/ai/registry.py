from collections.abc import Callable
from dataclasses import dataclass

from backend.ai.base import AIProvider, ProviderUnavailableError
from backend.ai.types import ProviderHealth
from backend.security.redaction import redact_text
from backend.settings.models import ProviderName, SettingsSnapshot


ProviderFactory = Callable[[SettingsSnapshot], AIProvider]


@dataclass(frozen=True)
class ProviderDecision:
    selected: ProviderName
    requested: ProviderName
    used_fallback: bool
    reason: str


class ProviderRegistry:
    def __init__(
        self,
        factories: dict[ProviderName, ProviderFactory],
        *,
        codex_path_resolver=None,
        allow_legacy: bool = True,
    ):
        self._factories = dict(factories)
        self._codex_path_resolver = codex_path_resolver
        self._allow_legacy = bool(allow_legacy)

    def health_for(
        self,
        provider_name: ProviderName,
        snapshot: SettingsSnapshot,
    ) -> ProviderHealth:
        provider: AIProvider | None = None
        try:
            provider = self._factories[provider_name](snapshot)
            health = provider.health_check()
            if not isinstance(health, ProviderHealth):
                return self._unavailable_health(provider_name)
            if health.provider != provider_name:
                return self._unavailable_health(provider_name)
            return ProviderHealth(
                ok=bool(health.ok),
                provider=provider_name,
                detail=redact_text(health.detail),
                runtime_mode=redact_text(health.runtime_mode),
            )
        except Exception:
            return self._unavailable_health(provider_name)
        finally:
            if provider is not None:
                self._safe_close(provider)

    def create_for_task(
        self,
        snapshot: SettingsSnapshot,
    ) -> tuple[AIProvider, ProviderDecision]:
        ai_settings = snapshot.settings.ai
        if ai_settings.configurations:
            from backend.ai.capability_router import CapabilityRouterProvider

            provider = CapabilityRouterProvider(
                snapshot,
                codex_path_resolver=self._codex_path_resolver,
            )
            health = provider.health_check()
            if not health.ok:
                provider.close()
                raise ProviderUnavailableError(
                    "No complete AI capability configuration is available."
                )
            return provider, ProviderDecision(
                selected=ProviderName.mixed,
                requested=ProviderName.mixed,
                used_fallback=False,
                reason="Stage-level AI capability routing is active.",
            )
        if not self._allow_legacy:
            raise ProviderUnavailableError(
                "No AI capability configuration is available."
            )
        requested = ai_settings.active_provider

        active = self._create_healthy(requested, snapshot)
        if active is not None:
            return active, ProviderDecision(
                selected=requested,
                requested=requested,
                used_fallback=False,
                reason="Active provider passed preflight.",
            )

        fallback_name = ai_settings.fallback_provider
        if ai_settings.fallback_enabled and fallback_name is not None:
            fallback = self._create_healthy(fallback_name, snapshot)
            if fallback is not None:
                return fallback, ProviderDecision(
                    selected=fallback_name,
                    requested=requested,
                    used_fallback=True,
                    reason="Active provider was unavailable; fallback passed preflight.",
                )

        raise ProviderUnavailableError(
            "No configured AI provider passed preflight."
        )

    def _create_healthy(
        self,
        provider_name: ProviderName,
        snapshot: SettingsSnapshot,
    ) -> AIProvider | None:
        provider: AIProvider | None = None
        try:
            provider = self._factories[provider_name](snapshot)
            health = provider.health_check()
            if isinstance(health, ProviderHealth) and health.ok:
                return provider
        except Exception:
            pass

        if provider is not None:
            self._safe_close(provider)
        return None

    @staticmethod
    def _safe_close(provider: AIProvider) -> None:
        try:
            provider.close()
        except Exception:
            pass

    @staticmethod
    def _unavailable_health(provider_name: ProviderName) -> ProviderHealth:
        return ProviderHealth(
            ok=False,
            provider=provider_name,
            detail="Provider is unavailable.",
        )


def build_provider_registry(
    *,
    codex_path_resolver=None,
    allow_legacy: bool = False,
) -> ProviderRegistry:
    from backend.ai.codex_provider import CodexProvider
    from backend.ai.minimax_provider import MiniMaxProvider
    from backend.ai.openai_compatible_provider import OpenAICompatibleProvider

    return ProviderRegistry(
        {
            ProviderName.codex: lambda snapshot: CodexProvider(
                snapshot.settings.ai.codex,
                api_key=snapshot.secrets.reveal("codex_api_key")
                if snapshot.settings.ai.codex.use_dedicated_api_key
                else None,
            ),
            ProviderName.minimax: lambda snapshot: MiniMaxProvider(
                snapshot.settings.ai.minimax,
                api_key=snapshot.secrets.reveal("minimax_api_key"),
            ),
            ProviderName.openai_compatible: lambda snapshot: (
                OpenAICompatibleProvider(
                    snapshot.settings.ai.openai_compatible,
                    api_key=snapshot.secrets.reveal(
                        "openai_compatible_api_key"
                    ),
                )
            ),
        },
        codex_path_resolver=codex_path_resolver,
        allow_legacy=allow_legacy,
    )
