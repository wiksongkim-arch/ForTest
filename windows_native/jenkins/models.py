"""Jenkins 配置、项目和参数的稳定数据模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class JenkinsConfiguration:
    """磁盘仅保存地址和用户名，Token 始终由密钥存储提供。"""

    base_url: str
    username: str


@dataclass(frozen=True)
class JenkinsParameter:
    """构建参数的最小通用表示。"""

    name: str
    kind: str = ""
    description: str = ""
    choices: tuple[str, ...] = ()
    default: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["choices"] = list(self.choices)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "JenkinsParameter":
        return cls(
            name=str(value.get("name") or ""),
            kind=str(value.get("kind") or ""),
            description=str(value.get("description") or ""),
            choices=tuple(str(item) for item in value.get("choices") or []),
            default=str(value.get("default") or ""),
        )


@dataclass(frozen=True)
class JenkinsProject:
    """用于快捷部署选择器的项目快照。"""

    full_name: str
    name: str
    description: str
    url: str
    project_class: str
    buildable: bool
    parameters: tuple[JenkinsParameter, ...] = field(default_factory=tuple)

    def parameter(self, name: str) -> JenkinsParameter | None:
        normalized = str(name).strip().upper()
        return next(
            (
                item
                for item in self.parameters
                if item.name.strip().upper() == normalized
            ),
            None,
        )

    @property
    def environments(self) -> tuple[str, ...]:
        parameter = self.parameter("ENV_NAME")
        return _parameter_values(parameter)

    @property
    def target_branches(self) -> tuple[str, ...]:
        parameter = self.parameter("TARGET_BRANCH")
        return _parameter_values(parameter)

    @property
    def eligible(self) -> bool:
        return bool(self.environments and self.target_branches and self.buildable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "project_class": self.project_class,
            "buildable": self.buildable,
            "parameters": [item.to_dict() for item in self.parameters],
            "environments": list(self.environments),
            "target_branches": list(self.target_branches),
            "eligible": self.eligible,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "JenkinsProject":
        return cls(
            full_name=str(value.get("full_name") or ""),
            name=str(value.get("name") or ""),
            description=str(value.get("description") or ""),
            url=str(value.get("url") or ""),
            project_class=str(value.get("project_class") or ""),
            buildable=bool(value.get("buildable", True)),
            parameters=tuple(
                JenkinsParameter.from_dict(item)
                for item in value.get("parameters") or []
                if isinstance(item, dict)
            ),
        )


def _parameter_values(parameter: JenkinsParameter | None) -> tuple[str, ...]:
    if parameter is None:
        return ()
    values: list[str] = []
    for item in (*parameter.choices, parameter.default):
        normalized = str(item).strip()
        if normalized and normalized not in values:
            values.append(normalized)
    return tuple(values)
