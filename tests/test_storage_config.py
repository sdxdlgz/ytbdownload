from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.security import ArtifactDirectLinkSigner


def test_direct_link_signatures_are_domain_separated_and_tamper_evident(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        data_dir=tmp_path,
        app_secret="direct-link-test-secret",
    )
    signer = ArtifactDirectLinkSigner(settings)
    artifact_id = "db969278-ed80-45a2-9796-4ddc7c929a9d"
    digest = "a" * 64
    signature = signer.sign(artifact_id, digest, 2_000_000_000)
    assert signer.verify(artifact_id, digest, 2_000_000_000, signature)
    assert not signer.verify(artifact_id, "b" * 64, 2_000_000_000, signature)
    assert not signer.verify(artifact_id, digest, 2_000_000_001, signature)
    assert not signer.verify(artifact_id, digest, 2_000_000_000, signature + "x")


def test_production_direct_links_require_strong_stable_secret(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="direct links require"):
        Settings(
            _env_file=None,
            environment="production",
            data_dir=tmp_path,
            access_token="",
            app_secret="short",
            direct_links_enabled=True,
        )
    settings = Settings(
        _env_file=None,
        environment="production",
        data_dir=tmp_path,
        access_token="",
        app_secret="",
        direct_links_enabled=False,
    )
    assert settings.direct_links_enabled is False


@pytest.mark.parametrize(
    "updates",
    [
        {"s3_enabled": True, "s3_bucket": ""},
        {
            "s3_enabled": True,
            "s3_bucket": "bucket",
            "s3_access_key_id": SecretStr("key"),
        },
        {
            "s3_enabled": True,
            "s3_bucket": "bucket",
            "s3_endpoint_url": "http://minio.internal:9000",
        },
        {"s3_enabled": False, "s3_keep_local": False},
        {"s3_enabled": False, "s3_failure_mode": "required"},
    ],
)
def test_invalid_s3_cross_field_configuration_is_rejected(tmp_path: Path, updates: dict) -> None:
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            environment="test",
            data_dir=tmp_path,
            app_secret="storage-config-test",
            **updates,
        )


def test_settings_validation_errors_do_not_echo_secret_inputs(tmp_path: Path) -> None:
    app_secret = "SENSITIVE_APP_SECRET_SENTINEL"
    access_key = "SENSITIVE_ACCESS_KEY_SENTINEL"
    with pytest.raises(ValueError) as caught:
        Settings(
            _env_file=None,
            environment="test",
            data_dir=tmp_path,
            app_secret=app_secret,
            s3_enabled=True,
            s3_bucket="private-media",
            s3_access_key_id=access_key,
        )

    rendered = str(caught.value)
    assert app_secret not in rendered
    assert access_key not in rendered
    assert "input_value" not in rendered


def test_explicit_private_minio_configuration_is_supported(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        data_dir=tmp_path,
        app_secret="storage-config-test",
        s3_enabled=True,
        s3_bucket="private-media",
        s3_endpoint_url="http://127.0.0.1:9000",
        s3_allow_insecure_endpoint=True,
        s3_access_key_id=SecretStr("minio-user"),
        s3_secret_access_key=SecretStr("minio-password"),
        s3_addressing_style="path",
    )
    assert settings.s3_enabled
    assert settings.s3_prefix == "signal-artifacts"
