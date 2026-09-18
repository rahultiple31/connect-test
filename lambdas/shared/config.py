"""Configuration loader for Lambda functions.

Reads configurable parameters from SSM Parameter Store with
environment variable fallback. Caches values per invocation.
Secrets are loaded from AWS Secrets Manager.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import ClassVar

import boto3


_DEFAULT_HOLD_MESSAGES: list[str] = [
    "I'm still working on that for you, one moment please.",
    "Still looking into that, thank you for your patience.",
    "Just a moment longer while I gather that information.",
]

_SSM_PARAM_KEYS: dict[str, str] = {
    "POLLING_INTERVAL_MS": "polling-interval-ms",
    "MAX_SESSION_DURATION_S": "max-session-duration-s",
    "INITIAL_RESPONSE_TIMEOUT_S": "initial-response-timeout-s",
    "HOLD_MESSAGE_POOL": "hold-message-pool",
    "MAX_RESPONSE_LENGTH": "max-response-length",
    "MAX_POLL_ITERATIONS": "max-poll-iterations",
    "SILENCE_TIMEOUT_S": "silence-timeout-s",
    "POLLY_VOICE_ID": "polly-voice-id",
    "NOVA_SONIC_VOICE": "nova-sonic-voice",
    "LEX_BOT_ALIAS": "lex-bot-alias",
    "NEXTHINK_AGENT_URL": "nexthink-agent-url",
    # Nexthink backend selection + Spark (A2A) settings
    "NEXTHINK_BACKEND": "nexthink-backend",
    "NEXTHINK_TENANT_ID": "nexthink-tenant-id",
    "NEXTHINK_TOKEN_URL": "nexthink-token-url",
    "NEXTHINK_SPARK_URL": "nexthink-spark-url",
    "NEXTHINK_USER_PRINCIPAL": "nexthink-user-principal",
}

# Config keys that are free-form strings (everything else is parsed as int)
_STRING_KEYS: frozenset[str] = frozenset({
    "POLLY_VOICE_ID",
    "NOVA_SONIC_VOICE",
    "LEX_BOT_ALIAS",
    "NEXTHINK_AGENT_URL",
    "NEXTHINK_BACKEND",
    "NEXTHINK_TENANT_ID",
    "NEXTHINK_TOKEN_URL",
    "NEXTHINK_SPARK_URL",
    "NEXTHINK_USER_PRINCIPAL",
})


@dataclass
class Config:
    """Centralised configuration loaded from env vars, SSM, and Secrets Manager."""

    # --- Tunable parameters (SSM / env) ---
    POLLING_INTERVAL_MS: int = 3000
    MAX_SESSION_DURATION_S: int = 300
    INITIAL_RESPONSE_TIMEOUT_S: int = 30
    HOLD_MESSAGE_POOL: list[str] = field(default_factory=lambda: list(_DEFAULT_HOLD_MESSAGES))
    MAX_RESPONSE_LENGTH: int = 3000
    MAX_POLL_ITERATIONS: int = 10
    SILENCE_TIMEOUT_S: int = 4
    POLLY_VOICE_ID: str = "Matthew"
    NOVA_SONIC_VOICE: str = "Matthew"
    LEX_BOT_ALIAS: str = ""
    NEXTHINK_AGENT_URL: str = ""

    # --- Nexthink backend: "mock" (Bedrock-backed simulator, default) or "spark" (real A2A API) ---
    NEXTHINK_BACKEND: str = "mock"
    # Spark (A2A) settings — only used when NEXTHINK_BACKEND == "spark"
    NEXTHINK_TENANT_ID: str = ""
    NEXTHINK_TOKEN_URL: str = ""
    NEXTHINK_SPARK_URL: str = ""
    NEXTHINK_USER_PRINCIPAL: str = ""

    # --- Secrets (Secrets Manager) ---
    # NEXTHINK_API_KEY: mock mode → sent as X-API-Key header (mock ignores it)
    #                   spark mode → OAuth2 Basic credential, i.e. base64("client_id:client_secret")
    NEXTHINK_API_KEY: str = ""
    CALLBACK_API_KEY: str = ""

    # --- Internal ---
    _SSM_PREFIX: ClassVar[str] = "/connect-async-multi-response/"

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, ssm_prefix: str | None = None) -> Config:
        """Build a *Config* by merging env vars → SSM → defaults.

        Priority: environment variable > SSM parameter > dataclass default.
        """
        prefix = ssm_prefix or os.environ.get("SSM_PREFIX", cls._SSM_PREFIX)
        if not prefix.endswith("/"):
            prefix += "/"

        ssm_values = _load_ssm_parameters(prefix)
        cfg = cls._apply_values(ssm_values)
        cfg = cls._load_secrets(cfg)
        return cfg

    @classmethod
    def _apply_values(cls, ssm_values: dict[str, str]) -> Config:
        """Resolve each parameter: env var first, then SSM, then default."""
        kwargs: dict[str, object] = {}

        for env_key, ssm_suffix in _SSM_PARAM_KEYS.items():
            env_val = os.environ.get(env_key)
            ssm_val = ssm_values.get(ssm_suffix)
            raw = env_val if env_val is not None else ssm_val

            if raw is None:
                continue  # use dataclass default

            if env_key == "HOLD_MESSAGE_POOL":
                kwargs[env_key] = json.loads(raw)
            elif env_key in _STRING_KEYS:
                kwargs[env_key] = raw
            else:
                kwargs[env_key] = int(raw)

        return cls(**kwargs)

    @classmethod
    def _load_secrets(cls, cfg: Config) -> Config:
        """Load secrets from Secrets Manager into the config."""
        nexthink_secret_name = os.environ.get("NEXTHINK_API_KEY_SECRET_NAME")
        callback_secret_name = os.environ.get("CALLBACK_API_KEY_SECRET_NAME")

        if not nexthink_secret_name and not callback_secret_name:
            return cfg

        client = boto3.client("secretsmanager")

        if nexthink_secret_name:
            cfg.NEXTHINK_API_KEY = _get_secret(client, nexthink_secret_name)
        if callback_secret_name:
            cfg.CALLBACK_API_KEY = _get_secret(client, callback_secret_name)

        return cfg


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _load_ssm_parameters(prefix: str) -> dict[str, str]:
    """Batch-load all SSM parameters under *prefix*.

    Returns a dict mapping the parameter suffix (after the prefix) to its value.
    """
    client = boto3.client("ssm")
    params: dict[str, str] = {}
    paginator = client.get_paginator("get_parameters_by_path")

    for page in paginator.paginate(Path=prefix, Recursive=False, WithDecryption=True):
        for p in page.get("Parameters", []):
            # Strip the prefix to get the suffix key
            name: str = p["Name"]
            suffix = name[len(prefix):]
            params[suffix] = p["Value"]

    return params


def _get_secret(client, secret_name: str) -> str:
    """Retrieve a plaintext secret from Secrets Manager."""
    response = client.get_secret_value(SecretId=secret_name)
    return response["SecretString"]


# ------------------------------------------------------------------
# Singleton cache
# ------------------------------------------------------------------

_config_instance: Config | None = None


def get_config(*, _force_reload: bool = False) -> Config:
    """Return the cached *Config* singleton.

    The config is loaded once per Lambda cold start and reused for the
    lifetime of the execution environment.  Pass ``_force_reload=True``
    in tests to reset the cache.
    """
    global _config_instance
    if _config_instance is None or _force_reload:
        _config_instance = Config.load()
    return _config_instance
