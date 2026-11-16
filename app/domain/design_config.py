"""Project design configuration and validated incremental changes."""

import json
import re
import unicodedata
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator


ConfigStatus = Literal["pending", "implemented", "verified"]


class ConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    module: str = Field(min_length=1, max_length=128, pattern=r"^[\w$][\w$. -]*$")
    instance: str | None = Field(default=None, max_length=200, pattern=r"^[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*$")
    parameter: str = Field(min_length=1, max_length=100, pattern=r"^[a-z_][a-z0-9_]*$")
    value: JsonValue
    evidence: str = Field(min_length=1, max_length=1_000)

    @field_validator("module", "instance", "parameter", mode="before")
    @classmethod
    def normalize_identifier(cls, value, info):
        if not isinstance(value, str):
            return value
        value = unicodedata.normalize("NFKC", value).strip()
        if info.field_name == "parameter":
            value = normalize_parameter(value)
        return value

    @field_validator("value")
    @classmethod
    def validate_value(cls, value):
        if value is None or value == "" or len(json.dumps(value, ensure_ascii=False, allow_nan=False)) > 4_000:
            raise ValueError("A design parameter requires a bounded, concrete JSON value")
        return value


class ConfigChanges(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    updates: list[ConfigUpdate] = Field(max_length=32)


class ConfigEvidence(BaseModel):
    kind: Literal["patch_approval", "artifact", "tool_run"]
    source_id: str
    location: str
    content_hash: str
    testcase: str | None = None


class ConfigItemRevision(BaseModel):
    value: JsonValue
    revision: int = Field(ge=1)
    source_message_id: str
    source_evidence: str
    status: ConfigStatus = "pending"
    evidence_refs: list[ConfigEvidence] = Field(default_factory=list)


class DesignConfigItem(ConfigItemRevision):
    config_item_id: str = Field(default_factory=lambda: str(uuid4()))
    history: list[ConfigItemRevision] = Field(default_factory=list)

    def previous_revision(self) -> ConfigItemRevision:
        return ConfigItemRevision.model_validate(self.model_dump(exclude={"config_item_id", "history"}))

    def context(self) -> dict:
        return self.model_dump(include={"value", "status", "revision"})


class ModuleDesignConfig(BaseModel):
    parameters: dict[str, DesignConfigItem] = Field(default_factory=dict)
    instances: dict[str, dict[str, DesignConfigItem]] = Field(default_factory=dict)

    def items(self):
        for parameter, item in self.parameters.items():
            yield None, parameter, item
        for instance, parameters in self.instances.items():
            for parameter, item in parameters.items():
                yield instance, parameter, item

    def get(self, instance: str | None, parameter: str) -> DesignConfigItem | None:
        return (self.instances.get(instance, {}) if instance else self.parameters).get(parameter)

    def set(self, instance: str | None, parameter: str, item: DesignConfigItem) -> None:
        parameters = self.instances.setdefault(instance, {}) if instance else self.parameters
        parameters[parameter] = item


class ProjectDesignConfig(BaseModel):
    project_id: str
    workspace_id: str | None = None
    revision: int = Field(default=0, ge=0)
    modules: dict[str, ModuleDesignConfig] = Field(default_factory=dict)

    def scope(self, module_name: str) -> "ProjectDesignConfig":
        name = normalize_module(module_name)
        selected = {name: self.modules[name]} if name in self.modules else {}
        return self.model_copy(update={"modules": selected})

    def context(self, module_name: str) -> dict:
        name = normalize_module(module_name)
        module = self.modules.get(name, ModuleDesignConfig())
        return {
            "module": name,
            "revision": self.revision,
            "parameters": {key: item.context() for key, item in module.parameters.items()},
            "instances": {
                instance: {key: item.context() for key, item in parameters.items()}
                for instance, parameters in module.instances.items()
            },
        }


def normalize_module(name: str) -> str:
    return unicodedata.normalize("NFKC", name).strip()


def normalize_parameter(name: str) -> str:
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return re.sub(r"[\s-]+", "_", name).lower()


def same_config_value(left: JsonValue, right: JsonValue) -> bool:
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)
