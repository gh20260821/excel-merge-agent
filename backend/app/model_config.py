from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, field_validator


ProviderName = Literal["openai", "minimax", "deepseek", "custom"]


class ModelProfile(BaseModel):
    provider: ProviderName = "custom"
    base_url: AnyHttpUrl
    model: str = Field(min_length=1, max_length=200)
    credential: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,100}$")
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    api_mode: Literal["chat_completions"] = "chat_completions"
    timeout: float = Field(default=60.0, gt=0, le=600)

    @field_validator("model")
    @classmethod
    def clean_model(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("model cannot be blank")
        return value

    def safe_summary(self, *, profile_id: str | None = None,
                     is_default: bool = False, has_api_key: bool = False) -> dict[str, object]:
        return {
            "id": profile_id, "provider": self.provider, "base_url": str(self.base_url),
            "model": self.model, "api_mode": self.api_mode, "timeout": self.timeout,
            "is_default": is_default, "has_api_key": has_api_key,
        }


class ModelRegistry(BaseModel):
    version: Literal[2] = 2
    default_profile: str = Field(alias="default", pattern=r"^[A-Za-z0-9._-]{1,100}$")
    profiles: dict[str, ModelProfile]
    model_config = {"populate_by_name": True}

    @field_validator("profiles")
    @classmethod
    def validate_profiles(cls, value: dict[str, ModelProfile]) -> dict[str, ModelProfile]:
        if not value:
            raise ValueError("at least one model profile is required")
        for profile_id in value:
            if not profile_id or len(profile_id) > 100 or not all(
                char.isalnum() or char in "._-" for char in profile_id
            ):
                raise ValueError(f"invalid model profile id: {profile_id!r}")
        return value

    def profile(self, profile_id: str | None = None) -> ModelProfile:
        selected = profile_id or self.default_profile
        try:
            return self.profiles[selected]
        except KeyError as exc:
            raise ValueError(f"model profile {selected!r} is not configured") from exc


class CredentialRecord(BaseModel):
    api_key: SecretStr


class KeyRegistry(BaseModel):
    version: Literal[1] = 1
    credentials: dict[str, CredentialRecord] = Field(default_factory=dict)

    def private_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "credentials": {
                credential_id: {"api_key": record.api_key.get_secret_value()}
                for credential_id, record in self.credentials.items()
            },
        }


class ModelConnectionInput(BaseModel):
    provider: ProviderName
    base_url: AnyHttpUrl
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr | None = None
    timeout: float = Field(default=60.0, gt=0, le=600)


def default_registry_path() -> Path:
    override = os.getenv("MERGE_AGENT_MODELS_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[3] / "models"


def default_keys_path(registry_path: Path | None = None) -> Path:
    override = os.getenv("MERGE_AGENT_KEYS_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return (registry_path or default_registry_path()).with_name("keys")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} must contain a JSON object")
    return payload


def _atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _guess_provider(base_url: str) -> ProviderName:
    host = base_url.lower()
    if "deepseek" in host:
        return "deepseek"
    if "minimax" in host:
        return "minimax"
    if "openai.com" in host:
        return "openai"
    return "custom"


def load_key_registry(path: Path | None = None, *, missing_ok: bool = False) -> KeyRegistry:
    keys_path = path or default_keys_path()
    if missing_ok and not keys_path.exists():
        return KeyRegistry()
    return KeyRegistry.model_validate(_read_json(keys_path, "key registry"))


def _migrate_legacy_payload(payload: dict[str, Any], registry_path: Path,
                            keys_path: Path) -> dict[str, Any]:
    profiles: dict[str, Any] = {}
    credentials: dict[str, Any] = {}
    for profile_id, raw in payload.items():
        if not isinstance(raw, dict) or "base_url" not in raw or "model" not in raw:
            continue
        item = dict(raw)
        api_key = item.pop("api_key", None)
        api_key_env = item.pop("api_key_env", None)
        credential_id = f"{profile_id}-key" if api_key else None
        if api_key:
            credentials[credential_id] = {"api_key": api_key}
        profiles[profile_id] = {
            "provider": item.pop("provider", _guess_provider(str(item["base_url"]))),
            "base_url": item["base_url"], "model": item["model"],
            "credential": credential_id, "api_key_env": api_key_env,
            "api_mode": item.get("api_mode", "chat_completions"),
            "timeout": item.get("timeout", 60.0),
        }
    if not profiles:
        raise RuntimeError("legacy model registry does not contain any usable profiles")
    default_profile = "default" if "default" in profiles else next(iter(profiles))
    migrated = {"version": 2, "default": default_profile, "profiles": profiles}
    existing_keys = load_key_registry(keys_path, missing_ok=True)
    for credential_id, record in credentials.items():
        existing_keys.credentials[credential_id] = CredentialRecord.model_validate(record)
    _atomic_private_json(keys_path, existing_keys.private_payload())
    _atomic_private_json(registry_path, migrated)
    return migrated


def load_model_registry(path: Path | None = None,
                        keys_path: Path | None = None) -> ModelRegistry:
    registry_path = path or default_registry_path()
    payload = _read_json(registry_path, "model registry")
    if payload.get("version") != 2 or "profiles" not in payload:
        payload = _migrate_legacy_payload(
            payload, registry_path, keys_path or default_keys_path(registry_path)
        )
    registry = ModelRegistry.model_validate(payload)
    registry.profile()
    return registry


class ModelConnectionRepository:
    """Local-only model settings and secrets with atomic, permission-restricted writes."""

    def __init__(self, models_path: Path | None = None, keys_path: Path | None = None) -> None:
        self.models_path = models_path or default_registry_path()
        self.keys_path = keys_path or default_keys_path(self.models_path)
        self._lock = threading.RLock()
        self._ensure_private(self.models_path)
        self._ensure_private(self.keys_path)

    @staticmethod
    def _ensure_private(path: Path) -> None:
        if path.exists():
            path.chmod(0o600)

    def registry(self) -> ModelRegistry:
        with self._lock:
            registry = load_model_registry(self.models_path, self.keys_path)
            self._ensure_private(self.models_path)
            self._ensure_private(self.keys_path)
            return registry

    def keys(self) -> KeyRegistry:
        with self._lock:
            return load_key_registry(self.keys_path, missing_ok=True)

    @staticmethod
    def _usable_secret(record: CredentialRecord | None) -> bool:
        if record is None:
            return False
        value = record.api_key.get_secret_value().strip()
        return bool(value) and set(value) != {"*"}

    def resolve_api_key(self, profile: ModelProfile) -> str:
        if profile.credential:
            record = self.keys().credentials.get(profile.credential)
            if not self._usable_secret(record):
                raise ValueError(f"credential {profile.credential!r} is not configured")
            assert record is not None
            return record.api_key.get_secret_value()
        if profile.api_key_env:
            value = os.getenv(profile.api_key_env)
            if value:
                return value
            raise ValueError(f"environment variable {profile.api_key_env!r} is not set")
        raise ValueError("model profile does not have a credential")

    def summaries(self) -> dict[str, Any]:
        registry, keys = self.registry(), self.keys()
        return {
            "version": registry.version, "default": registry.default_profile,
            "profiles": [profile.safe_summary(
                profile_id=profile_id,
                is_default=profile_id == registry.default_profile,
                has_api_key=bool(
                    (profile.credential and self._usable_secret(keys.credentials.get(profile.credential)))
                    or (profile.api_key_env and os.getenv(profile.api_key_env))
                ),
            ) for profile_id, profile in registry.profiles.items()],
        }

    def save(self, profile_id: str, request: ModelConnectionInput) -> ModelProfile:
        if not profile_id or len(profile_id) > 100 or not all(
            char.isalnum() or char in "._-" for char in profile_id
        ):
            raise ValueError("profile id may contain only letters, numbers, dots, dashes, and underscores")
        with self._lock:
            registry, keys = self.registry(), self.keys()
            previous = registry.profiles.get(profile_id)
            credential_id = previous.credential if previous else f"{profile_id}-key"
            if request.api_key is not None:
                if not request.api_key.get_secret_value().strip():
                    raise ValueError("API key cannot be blank")
                keys.credentials[credential_id] = CredentialRecord(api_key=request.api_key)
            elif not self._usable_secret(keys.credentials.get(credential_id)):
                raise ValueError("an API key is required for a new model connection")
            profile = ModelProfile(
                provider=request.provider, base_url=request.base_url,
                model=request.model, credential=credential_id, timeout=request.timeout,
            )
            registry.profiles[profile_id] = profile
            _atomic_private_json(self.keys_path, keys.private_payload())
            _atomic_private_json(
                self.models_path,
                registry.model_dump(mode="json", by_alias=True, exclude_none=True),
            )
            return profile

    def set_default(self, profile_id: str) -> ModelRegistry:
        with self._lock:
            registry = self.registry()
            registry.profile(profile_id)
            registry.default_profile = profile_id
            _atomic_private_json(
                self.models_path,
                registry.model_dump(mode="json", by_alias=True, exclude_none=True),
            )
            return registry
