from __future__ import annotations

import json
import stat
from pathlib import Path

from pydantic import SecretStr
import pytest

from app.model_config import ModelConnectionInput, ModelConnectionRepository
from app.model_runtime import safe_provider_error


def test_legacy_registry_is_migrated_and_secrets_are_separated(tmp_path: Path) -> None:
    models_path, keys_path = tmp_path / "models", tmp_path / "keys"
    models_path.write_text(json.dumps({
        "default": {
            "base_url": "https://example.test/v1", "api_key": "secret-value",
            "model": "test-model", "api_mode": "chat_completions", "timeout": 30,
        },
        "deepseek": {
            "base_url": "https://api.deepseek.com", "api_key": "other-secret",
            "model": "deepseek-chat", "api_mode": "chat_completions",
        },
    }), encoding="utf-8")

    repository = ModelConnectionRepository(models_path, keys_path)
    summary = repository.summaries()

    assert summary["default"] == "default"
    assert {item["id"] for item in summary["profiles"]} == {"default", "deepseek"}
    assert repository.resolve_api_key(repository.registry().profile()) == "secret-value"
    assert "secret-value" not in models_path.read_text(encoding="utf-8")
    assert "other-secret" not in models_path.read_text(encoding="utf-8")
    assert "secret-value" in keys_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(models_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(keys_path.stat().st_mode) == 0o600


def test_connection_updates_preserve_key_when_api_key_is_omitted(tmp_path: Path) -> None:
    models_path, keys_path = tmp_path / "models", tmp_path / "keys"
    models_path.write_text(json.dumps({
        "version": 2, "default": "starter", "profiles": {
            "starter": {
                "provider": "custom", "base_url": "https://example.test/v1",
                "model": "old-model", "credential": "starter-key",
            }
        },
    }), encoding="utf-8")
    keys_path.write_text(json.dumps({
        "version": 1, "credentials": {"starter-key": {"api_key": "kept-secret"}}
    }), encoding="utf-8")
    repository = ModelConnectionRepository(models_path, keys_path)
    repository.save("starter", ModelConnectionInput(
        provider="custom", base_url="https://new.example.test/v1",
        model="new-model", api_key=None, timeout=90,
    ))
    assert repository.resolve_api_key(repository.registry().profile()) == "kept-secret"
    assert "kept-secret" not in models_path.read_text(encoding="utf-8")

    repository.save("added", ModelConnectionInput(
        provider="openai", base_url="https://api.openai.com/v1", model="gpt-test",
        api_key=SecretStr("new-secret"), timeout=60,
    ))
    repository.set_default("added")
    assert repository.registry().default_profile == "added"
    assert "new-secret" not in models_path.read_text(encoding="utf-8")


def test_masked_placeholder_is_not_accepted_as_a_real_key(tmp_path: Path) -> None:
    models_path, keys_path = tmp_path / "models", tmp_path / "keys"
    models_path.write_text(json.dumps({
        "version": 2, "default": "profile", "profiles": {
            "profile": {"provider": "custom", "base_url": "https://example.test/v1",
                        "model": "model", "credential": "profile-key"}
        },
    }), encoding="utf-8")
    keys_path.write_text(json.dumps({
        "version": 1, "credentials": {"profile-key": {"api_key": "**********"}}
    }), encoding="utf-8")
    repository = ModelConnectionRepository(models_path, keys_path)
    assert repository.summaries()["profiles"][0]["has_api_key"] is False
    with pytest.raises(ValueError, match="not configured"):
        repository.resolve_api_key(repository.registry().profile())


@pytest.mark.parametrize(("error", "expected"), [
    ("Error code: 402 - Insufficient Balance", "insufficient balance"),
    ("Error code: 401 - invalid api key", "rejected the API key"),
    ("status code 404: model not found", "endpoint or model was not found"),
    ("429 too many requests", "rate limit"),
    ("request timed out", "configured timeout"),
])
def test_provider_errors_are_specific_but_sanitized(error: str, expected: str) -> None:
    assert expected in safe_provider_error(RuntimeError(error))
