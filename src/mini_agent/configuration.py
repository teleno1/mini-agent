"""Strict, provenance-aware configuration for Mini Agent.

Configuration is deliberately kept independent from the CLI and the model
provider.  Every source is parsed into the same small field vocabulary before
the values are merged.  This makes it possible to inspect why a value won and
to enforce the host's safety ceilings at the last point where a value changes.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, cast
from urllib.parse import urlparse

from mini_agent.domain.sessions import JSONValue, SessionEventType


class _SessionWriterLike(Protocol):
    def __enter__(self) -> _SessionWriterLike: ...

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...

    def append(self, event_type: str, payload: Mapping[str, JSONValue]) -> object: ...


class _ResumedSessionLike(Protocol):
    @property
    def configuration_overrides(self) -> Mapping[str, JSONValue]: ...


class _SessionStoreLike(Protocol):
    def resume(self, session_id: str) -> _ResumedSessionLike: ...

    def open_writer(self, session_id: str) -> _SessionWriterLike: ...


class ConfigurationError(ValueError):
    """Raised when a configuration source cannot be safely used."""


class UnknownConfigurationKey(ConfigurationError):
    """Raised for a key outside the versioned configuration vocabulary."""


class ForbiddenConfigurationKey(ConfigurationError):
    """Raised when a source is not allowed to provide a sensitive field."""


class SessionConfigurationError(ConfigurationError):
    """Raised for an invalid or forbidden active-Session override."""


class SessionOverrideConfirmationRequired(SessionConfigurationError):
    """Raised when an override makes the permission policy less restrictive."""


class ConfigurationSource(StrEnum):
    BUILTIN = "built-in"
    USER_TOML = "user TOML"
    PROJECT_TOML = "project TOML"
    ENVIRONMENT = "environment"
    CLI = "CLI"
    SESSION = "Session override"


class PermissionMode(StrEnum):
    SUGGEST = "suggest"
    AUTO_EDIT = "auto-edit"
    FULL_AUTO = "full-auto"


class PermissionStrictness(IntEnum):
    SUGGEST = 0
    AUTO_EDIT = 1
    FULL_AUTO = 2


# These are host ceilings, not defaults.  A source may choose a smaller value
# but cannot silently make the Agent Loop less bounded.
SAFETY_CEILINGS: Mapping[str, int] = MappingProxyType(
    {
        "max_model_requests": 25,
        "max_tool_calls": 50,
        "max_active_seconds": 30 * 60,
        "context_window_tokens": 1_000_000,
        "response_reserve_tokens": 300_000,
        "artifact_threshold_bytes": 64 * 1024,
        "instruction_file_bytes": 32 * 1024,
        "instruction_chain_bytes": 128 * 1024,
    }
)

DEFAULTS: Mapping[str, object] = MappingProxyType(
    {
        "model": "gpt-4o-mini",
        "permission_mode": PermissionMode.SUGGEST.value,
        "provider_base_url": "https://api.openai.com/v1",
        "max_model_requests": 25,
        "max_tool_calls": 50,
        "max_active_seconds": 30 * 60,
        "context_window_tokens": 128_000,
        "response_reserve_tokens": 16_000,
        "artifact_threshold_bytes": 32 * 1024,
        "instruction_file_bytes": 32 * 1024,
        "instruction_chain_bytes": 128 * 1024,
    }
)

# ``plan_mode`` is a runtime control, not a project/user/environment value.
# It is still part of the effective configuration so the Agent Loop can
# capture it at Turn start and Session overrides retain normal provenance.
CONFIGURATION_FIELDS = frozenset((*DEFAULTS, "plan_mode"))
ACTIVE_SESSION_FORBIDDEN_FIELDS = frozenset(
    {"api_key", "provider_base_url", "base_url", "workspace", "session_storage"}
)
SESSION_OVERRIDE_FIELDS = frozenset(
    {
        "model",
        "permission_mode",
        "max_model_requests",
        "max_tool_calls",
        "max_active_seconds",
        "context_window_tokens",
        "response_reserve_tokens",
        "artifact_threshold_bytes",
        "instruction_file_bytes",
        "instruction_chain_bytes",
        "plan_mode",
    }
)

_ALIASES = {"base_url": "provider_base_url"}
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "credentials",
        "password",
        "secret",
        "token",
        "provider_api_key",
    }
)
_ENVIRONMENT_FIELDS = {
    "MINI_AGENT_MODEL": "model",
    "MINI_AGENT_PERMISSION_MODE": "permission_mode",
    "MINI_AGENT_PROVIDER_BASE_URL": "provider_base_url",
    "MINI_AGENT_MAX_MODEL_REQUESTS": "max_model_requests",
    "MINI_AGENT_MAX_TOOL_CALLS": "max_tool_calls",
    "MINI_AGENT_MAX_ACTIVE_SECONDS": "max_active_seconds",
    "MINI_AGENT_CONTEXT_WINDOW_TOKENS": "context_window_tokens",
    "MINI_AGENT_RESPONSE_RESERVE_TOKENS": "response_reserve_tokens",
    "MINI_AGENT_ARTIFACT_THRESHOLD_BYTES": "artifact_threshold_bytes",
    "MINI_AGENT_INSTRUCTION_FILE_BYTES": "instruction_file_bytes",
    "MINI_AGENT_INSTRUCTION_CHAIN_BYTES": "instruction_chain_bytes",
}


@dataclass(frozen=True, slots=True)
class FieldProvenance:
    """The winning source and safety adjustment for one field."""

    source: ConfigurationSource
    requested_value: JSONValue
    applied_safety_cap: int | None = None

    def as_dict(self) -> dict[str, JSONValue]:
        return {
            "source": self.source.value,
            "requested_value": self.requested_value,
            "applied_safety_cap": self.applied_safety_cap,
        }


@dataclass(frozen=True, slots=True)
class EffectiveConfiguration:
    """The immutable configuration used by one operation."""

    model: str
    permission_mode: PermissionMode
    provider_base_url: str
    max_model_requests: int
    max_tool_calls: int
    max_active_seconds: int
    context_window_tokens: int
    response_reserve_tokens: int
    artifact_threshold_bytes: int
    instruction_file_bytes: int
    instruction_chain_bytes: int
    plan_mode: bool = False
    api_key: str | None = field(default=None, repr=False)
    provenance: Mapping[str, FieldProvenance] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))

    @property
    def base_url(self) -> str:
        """Compatibility spelling for the provider Base URL."""

        return self.provider_base_url

    @property
    def api_key_present(self) -> bool:
        return bool(self.api_key)

    def non_secret_values(self) -> dict[str, JSONValue]:
        """Return effective values suitable for hashing and a Manifest."""

        return {
            "model": self.model,
            "permission_mode": self.permission_mode.value,
            "provider_base_url": self.provider_base_url,
            "max_model_requests": self.max_model_requests,
            "max_tool_calls": self.max_tool_calls,
            "max_active_seconds": self.max_active_seconds,
            "context_window_tokens": self.context_window_tokens,
            "response_reserve_tokens": self.response_reserve_tokens,
            "artifact_threshold_bytes": self.artifact_threshold_bytes,
            "instruction_file_bytes": self.instruction_file_bytes,
            "instruction_chain_bytes": self.instruction_chain_bytes,
            "plan_mode": self.plan_mode,
        }

    def as_dict(self, *, include_provenance: bool = True) -> dict[str, JSONValue]:
        """Return a safe inspection view; the API key is never returned."""

        result = self.non_secret_values()
        result["api_key"] = "<redacted>" if self.api_key else None
        result["api_key_present"] = self.api_key_present
        if include_provenance:
            result["provenance"] = {key: value.as_dict() for key, value in self.provenance.items()}
        return result

    def configuration_hash(self) -> str:
        """Hash effective non-secret values and their winning sources."""

        return _sha256_json(
            {
                "values": self.non_secret_values(),
                "provenance": {key: value.as_dict() for key, value in self.provenance.items()},
            }
        )

    def session_override_values(self) -> dict[str, JSONValue]:
        """Return only values that can legally be carried into a Session."""

        return {
            key: _json_value(getattr(self, key))
            for key in SESSION_OVERRIDE_FIELDS
            if self.provenance.get(key, None) is not None
            and self.provenance[key].source is ConfigurationSource.SESSION
        }


class ConfigurationResolver:
    """Load and merge the six configuration sources in fixed precedence."""

    def __init__(
        self,
        workspace_root: Path | str,
        *,
        user_config_path: Path | str | None = None,
        project_config_path: Path | str | None = None,
        environment: Mapping[str, str] | None = None,
        cli_values: Mapping[str, object] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.user_config_path = (
            Path(user_config_path).expanduser()
            if user_config_path is not None
            else default_user_config_path()
        )
        self.project_config_path = (
            Path(project_config_path)
            if project_config_path is not None
            else self.workspace_root / ".mini-agent" / "config.toml"
        )
        self.environment = dict(os.environ if environment is None else environment)
        self.cli_values = dict(cli_values or {})

    def resolve(
        self,
        *,
        session_overrides: Mapping[str, object] | None = None,
        session_reset: bool = False,
        confirm_less_restrictive: bool = False,
    ) -> EffectiveConfiguration:
        values: dict[str, object] = dict(DEFAULTS)
        values["plan_mode"] = False
        provenance = {
            key: FieldProvenance(ConfigurationSource.BUILTIN, _json_value(value))
            for key, value in values.items()
        }

        user_values = self._read_toml(self.user_config_path, ConfigurationSource.USER_TOML)
        project_values = self._read_toml(
            self.project_config_path, ConfigurationSource.PROJECT_TOML, project=True
        )
        for source, source_values in (
            (ConfigurationSource.USER_TOML, user_values),
            (ConfigurationSource.PROJECT_TOML, project_values),
            (ConfigurationSource.ENVIRONMENT, self._read_environment()),
            (ConfigurationSource.CLI, self._read_cli()),
        ):
            self._merge(values, provenance, source, source_values)

        raw_api_key = self.environment.get("MINI_AGENT_API_KEY")
        if raw_api_key is not None and not raw_api_key.strip():
            raise ConfigurationError(
                "Provider authentication is unavailable: MINI_AGENT_API_KEY must not be blank. "
                "Set a non-blank API key in the MINI_AGENT_API_KEY environment variable; "
                "API keys are read only "
                "from the environment, never from TOML or CLI options."
            )
        api_key = raw_api_key or None

        base_before_session = _build_configuration(values, provenance, api_key)
        overrides = dict(session_overrides or {})
        if overrides:
            self._validate_session_override_keys(overrides)
        if session_reset:
            overrides = {}
        if overrides:
            _check_permission_change(
                base_before_session.permission_mode,
                overrides.get("permission_mode"),
                confirm_less_restrictive=confirm_less_restrictive,
            )
            self._merge(values, provenance, ConfigurationSource.SESSION, overrides)
        return _build_configuration(values, provenance, api_key)

    load = resolve

    def _read_toml(
        self,
        path: Path,
        source: ConfigurationSource,
        *,
        project: bool = False,
    ) -> dict[str, object]:
        if not path.exists():
            return {}
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(f"{source.value} {path}: invalid TOML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigurationError(f"{source.value} {path}: TOML root must be a table")
        values: dict[str, object] = {}
        for raw_key, raw_value in raw.items():
            key = _canonical_key(raw_key, source, path)
            if key == "plan_mode":
                raise ForbiddenConfigurationKey(
                    f"{source.value} {path}: plan_mode is runtime-only; use --plan-mode "
                    "or /plan on|off"
                )
            if key in _SENSITIVE_KEYS or key in {"provider_base_url", "base_url"} and project:
                raise ForbiddenConfigurationKey(
                    f"{source.value} {path}: {raw_key!r} cannot be set in project configuration"
                )
            if key not in CONFIGURATION_FIELDS:
                raise UnknownConfigurationKey(
                    f"{source.value} {path}: unknown configuration key {raw_key!r}"
                )
            if isinstance(raw_value, dict):
                raise ConfigurationError(
                    f"{source.value} {path}: {raw_key!r} must be a scalar value"
                )
            values[key] = raw_value
        return values

    def _read_environment(self) -> dict[str, object]:
        values: dict[str, object] = {}
        for environment_key, field_name in _ENVIRONMENT_FIELDS.items():
            raw = self.environment.get(environment_key)
            if raw is not None:
                values[field_name] = raw
        return values

    def _read_cli(self) -> dict[str, object]:
        values: dict[str, object] = {}
        for raw_key, raw_value in self.cli_values.items():
            key = _canonical_key(raw_key, ConfigurationSource.CLI, None)
            if key in _SENSITIVE_KEYS or key in {"workspace", "session_storage"}:
                raise ForbiddenConfigurationKey(
                    f"CLI cannot set sensitive configuration {raw_key!r}"
                )
            if key not in CONFIGURATION_FIELDS:
                raise UnknownConfigurationKey(f"CLI: unknown configuration key {raw_key!r}")
            values[key] = raw_value
        return values

    @staticmethod
    def _merge(
        values: dict[str, object],
        provenance: dict[str, FieldProvenance],
        source: ConfigurationSource,
        incoming: Mapping[str, object],
    ) -> None:
        for key, requested in incoming.items():
            canonical = _canonical_key(key, source, None)
            if canonical not in CONFIGURATION_FIELDS:
                raise UnknownConfigurationKey(f"{source.value}: unknown configuration key {key!r}")
            validated = _validate_field(canonical, requested, source)
            effective, cap = _apply_safety_cap(canonical, validated)
            values[canonical] = effective
            provenance[canonical] = FieldProvenance(
                source,
                _json_value(validated),
                cap,
            )

    @staticmethod
    def _validate_session_override_keys(overrides: Mapping[str, object]) -> None:
        for raw_key in overrides:
            key = _canonical_key(raw_key, ConfigurationSource.SESSION, None)
            if key in ACTIVE_SESSION_FORBIDDEN_FIELDS:
                raise SessionConfigurationError(
                    f"{ConfigurationSource.SESSION.value} cannot change {raw_key!r}"
                )
            if key not in SESSION_OVERRIDE_FIELDS:
                raise SessionConfigurationError(
                    f"{ConfigurationSource.SESSION.value} does not allow {raw_key!r}"
                )


class SessionConfigurationService:
    """Persist allowlisted configuration changes as non-secret Session events."""

    def __init__(self, resolver: ConfigurationResolver, session_store: _SessionStoreLike) -> None:
        self.resolver = resolver
        self.session_store = session_store

    def update(
        self,
        session_id: str,
        overrides: Mapping[str, object] | None = None,
        *,
        reset: bool = False,
        confirm_less_restrictive: bool = False,
    ) -> EffectiveConfiguration:
        resumed = self.session_store.resume(session_id)
        existing = dict(resumed.configuration_overrides)
        requested = {} if reset else {**existing, **dict(overrides or {})}
        effective = self.resolver.resolve(
            session_overrides=requested,
            session_reset=reset,
            confirm_less_restrictive=confirm_less_restrictive,
        )
        with self.session_store.open_writer(session_id) as writer:
            session_provenance: dict[str, JSONValue] = {
                key: effective.provenance[key].as_dict()
                for key in effective.provenance
                if effective.provenance[key].source is ConfigurationSource.SESSION
            }
            writer.append(
                SessionEventType.CONFIGURATION_CHANGED,
                {
                    "overrides": {
                        key: _json_value(value)
                        for key, value in (dict(overrides or {}) if not reset else {}).items()
                    },
                    "reset": reset,
                    "configuration_hash": effective.configuration_hash(),
                    "provenance": session_provenance,
                },
            )
        return effective


def default_user_config_path() -> Path:
    """Return the platform-appropriate user TOML path without creating it."""

    if os.name == "nt":
        root = os.environ.get("APPDATA")
        base = Path(root) if root else Path.home() / "AppData" / "Roaming"
        return base / "mini-agent" / "config.toml"
    return Path.home() / ".config" / "mini-agent" / "config.toml"


def initialize_project(workspace_root: Path | str, *, confirmed: bool) -> tuple[Path, Path | None]:
    """Create safe project defaults and an ignore rule after explicit consent."""

    if not confirmed:
        raise ConfigurationError("initialization requires explicit confirmation")
    root = Path(workspace_root).resolve()
    config_path = root / ".mini-agent" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        content = (
            "# Mini Agent project configuration (no credentials are stored here).\n"
            f"model = {DEFAULTS['model']!r}\n"
            f"permission_mode = {DEFAULTS['permission_mode']!r}\n"
        ).replace("'", '"')
        config_path.write_text(content, encoding="utf-8", newline="\n")

    gitignore = root / ".gitignore"
    ignore_path: Path | None = None
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if ".mini-agent/" not in {line.strip() for line in existing.splitlines()}:
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        gitignore.write_text(
            f"{existing}{prefix}# Mini Agent runtime data\n.mini-agent/\n",
            encoding="utf-8",
            newline="\n",
        )
        ignore_path = gitignore
    return config_path, ignore_path


def redact_secrets(value: object, secrets: tuple[str, ...] = ()) -> str:
    """Render data with known secrets and common credential forms removed.

    Detection is intentionally conservative and best-effort; callers must not
    treat a clean result as proof that no credential was present.
    """

    text = str(value)
    candidates = tuple(secret for secret in secrets if secret)
    for secret in candidates:
        text = text.replace(secret, "<redacted>")
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1<redacted>", text)
    text = re.sub(
        r"(?i)(api[_ -]?key\s*[:=]\s*)[\"']?[^\s,;\"']+",
        r"\1<redacted>",
        text,
    )
    text = re.sub(
        r"\b(?:sk|pk|ghp|gho|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{8,}\b",
        "<redacted>",
        text,
    )
    text = re.sub(r"\bAKIA[0-9A-Z]{16}\b", "<redacted>", text)
    return text


def _canonical_key(raw_key: object, source: ConfigurationSource, path: Path | None) -> str:
    if not isinstance(raw_key, str) or not raw_key.strip():
        location = f" {path}" if path is not None else ""
        raise UnknownConfigurationKey(
            f"{source.value}{location}: configuration keys must be strings"
        )
    key = _ALIASES.get(raw_key, raw_key)
    return key


def _validate_field(key: str, value: object, source: ConfigurationSource) -> object:
    if key in {
        "max_model_requests",
        "max_tool_calls",
        "max_active_seconds",
        "context_window_tokens",
        "response_reserve_tokens",
        "artifact_threshold_bytes",
        "instruction_file_bytes",
        "instruction_chain_bytes",
    }:
        if isinstance(value, bool):
            raise ConfigurationError(f"{source.value}: {key} must be an integer")
        if isinstance(value, int):
            integer = value
        elif isinstance(value, str):
            try:
                integer = int(value, 10)
            except ValueError as exc:
                raise ConfigurationError(f"{source.value}: {key} must be an integer") from exc
        else:
            raise ConfigurationError(f"{source.value}: {key} must be an integer")
        if integer < 1:
            raise ConfigurationError(f"{source.value}: {key} must be at least 1")
        return integer
    if key == "permission_mode":
        if not isinstance(value, str) or value not in {item.value for item in PermissionMode}:
            raise ConfigurationError(
                f"{source.value}: permission_mode must be suggest, auto-edit, or full-auto"
            )
        return PermissionMode(value)
    if key == "plan_mode":
        if not isinstance(value, bool):
            raise ConfigurationError(f"{source.value}: plan_mode must be a boolean")
        return value
    if key in {"model", "provider_base_url"}:
        if not isinstance(value, str) or not value.strip():
            raise ConfigurationError(f"{source.value}: {key} must be a non-blank string")
        if key == "provider_base_url":
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ConfigurationError(
                    f"{source.value}: provider_base_url must be an HTTP(S) URL"
                )
            if parsed.username is not None or parsed.password is not None:
                raise ConfigurationError(
                    f"{source.value}: provider_base_url must not contain credentials"
                )
        return value.strip()
    raise UnknownConfigurationKey(f"{source.value}: unknown configuration key {key!r}")


def _apply_safety_cap(key: str, value: object) -> tuple[object, int | None]:
    cap = SAFETY_CEILINGS.get(key)
    if cap is not None and isinstance(value, int) and value > cap:
        return cap, cap
    return value, None


def _build_configuration(
    values: Mapping[str, object], provenance: Mapping[str, FieldProvenance], api_key: str | None
) -> EffectiveConfiguration:
    return EffectiveConfiguration(
        model=cast(str, values["model"]),
        permission_mode=PermissionMode(cast(str | PermissionMode, values["permission_mode"])),
        provider_base_url=cast(str, values["provider_base_url"]),
        max_model_requests=cast(int, values["max_model_requests"]),
        max_tool_calls=cast(int, values["max_tool_calls"]),
        max_active_seconds=cast(int, values["max_active_seconds"]),
        context_window_tokens=cast(int, values["context_window_tokens"]),
        response_reserve_tokens=cast(int, values["response_reserve_tokens"]),
        artifact_threshold_bytes=cast(int, values["artifact_threshold_bytes"]),
        instruction_file_bytes=cast(int, values["instruction_file_bytes"]),
        instruction_chain_bytes=cast(int, values["instruction_chain_bytes"]),
        plan_mode=cast(bool, values["plan_mode"]),
        api_key=api_key,
        provenance=provenance,
    )


def _check_permission_change(
    current: PermissionMode,
    requested: object,
    *,
    confirm_less_restrictive: bool,
) -> None:
    if requested is None:
        return
    validated = _validate_field("permission_mode", requested, ConfigurationSource.SESSION)
    mode = PermissionMode(cast(str, validated))
    rank = {PermissionMode.SUGGEST: 0, PermissionMode.AUTO_EDIT: 1, PermissionMode.FULL_AUTO: 2}
    if rank[mode] > rank[current] and not confirm_less_restrictive:
        raise SessionOverrideConfirmationRequired(
            f"changing permission_mode from {current.value} to {mode.value} requires confirmation"
        )


def _json_value(value: object) -> JSONValue:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, PermissionMode):
        return value.value
    raise TypeError(f"configuration value is not JSON-compatible: {type(value).__name__}")


def _sha256_json(value: object) -> str:
    import hashlib

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
