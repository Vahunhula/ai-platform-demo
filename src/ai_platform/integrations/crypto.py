"""Authenticated encryption for per-connection provider credentials."""

import base64
import binascii
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ai_platform.integrations.models import IntegrationConfigurationError

_VERSION = b"v1."


class CredentialCipher:
    """AES-256-GCM envelope encryption with caller-supplied associated data."""

    def __init__(self, encoded_key: str | None) -> None:
        self._key = self._decode_key(encoded_key) if encoded_key else None

    @property
    def available(self) -> bool:
        return self._key is not None

    def encrypt(self, value: dict[str, Any], *, context: str) -> str:
        if self._key is None:
            raise IntegrationConfigurationError(
                "Integration credential encryption is not configured."
            )
        nonce = os.urandom(12)
        plaintext = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
        ciphertext = AESGCM(self._key).encrypt(nonce, plaintext, context.encode())
        return (_VERSION + base64.urlsafe_b64encode(nonce + ciphertext)).decode()

    def decrypt(self, envelope: str, *, context: str) -> dict[str, Any]:
        if self._key is None:
            raise IntegrationConfigurationError(
                "Encrypted integration credentials exist but the encryption key is unavailable."
            )
        if not envelope.startswith(_VERSION.decode()):
            raise IntegrationConfigurationError("Unsupported encrypted credential format.")
        try:
            raw = base64.urlsafe_b64decode(envelope[len(_VERSION) :])
            plaintext = AESGCM(self._key).decrypt(raw[:12], raw[12:], context.encode())
            value = json.loads(plaintext)
        except (ValueError, binascii.Error, InvalidTag, json.JSONDecodeError) as error:
            raise IntegrationConfigurationError(
                "Integration credentials cannot be decrypted with the configured key."
            ) from error
        if not isinstance(value, dict):
            raise IntegrationConfigurationError("Encrypted integration credentials are invalid.")
        return value

    @staticmethod
    def _decode_key(encoded_key: str) -> bytes:
        try:
            if len(encoded_key) == 64:
                key = bytes.fromhex(encoded_key)
            else:
                key = base64.urlsafe_b64decode(encoded_key + "=" * (-len(encoded_key) % 4))
        except (ValueError, binascii.Error) as error:
            raise IntegrationConfigurationError(
                "AI_PLATFORM_INTEGRATION_ENCRYPTION_KEY must encode exactly 32 bytes."
            ) from error
        if len(key) != 32:
            raise IntegrationConfigurationError(
                "AI_PLATFORM_INTEGRATION_ENCRYPTION_KEY must encode exactly 32 bytes."
            )
        return key
