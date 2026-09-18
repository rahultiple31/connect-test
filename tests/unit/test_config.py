"""Unit tests for lambdas/shared/config.py — configuration loader."""

from __future__ import annotations

import json
import os

import boto3
import pytest
from moto import mock_aws

from lambdas.shared.config import Config, get_config, _DEFAULT_HOLD_MESSAGES

SSM_PREFIX = "/connect-async-multi-response/"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Remove config-related env vars and reset the singleton between tests."""
    for key in [
        "POLLING_INTERVAL_MS", "MAX_SESSION_DURATION_S", "INITIAL_RESPONSE_TIMEOUT_S",
        "HOLD_MESSAGE_POOL", "MAX_RESPONSE_LENGTH", "MAX_POLL_ITERATIONS",
        "SILENCE_TIMEOUT_S", "POLLY_VOICE_ID", "NOVA_SONIC_VOICE",
        "LEX_BOT_ALIAS", "NEXTHINK_AGENT_URL", "SSM_PREFIX",
        "NEXTHINK_API_KEY_SECRET_NAME", "CALLBACK_API_KEY_SECRET_NAME",
        "NEXTHINK_BACKEND", "NEXTHINK_TENANT_ID", "NEXTHINK_TOKEN_URL",
        "NEXTHINK_SPARK_URL", "NEXTHINK_USER_PRINCIPAL",
    ]:
        monkeypatch.delenv(key, raising=False)
    # Reset the singleton WITHOUT loading (loading would hit real SSM
    # outside any @mock_aws scope). Each test loads inside its own mock.
    import lambdas.shared.config as _cfg_mod
    _cfg_mod._config_instance = None


def _put_ssm(client, name: str, value: str) -> None:
    client.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)


# ------------------------------------------------------------------
# Defaults (no SSM, no env vars)
# ------------------------------------------------------------------

class TestDefaults:
    @mock_aws
    def test_defaults_when_ssm_empty(self):
        cfg = Config.load()
        assert cfg.POLLING_INTERVAL_MS == 3000
        assert cfg.MAX_SESSION_DURATION_S == 300
        assert cfg.INITIAL_RESPONSE_TIMEOUT_S == 30
        assert cfg.HOLD_MESSAGE_POOL == _DEFAULT_HOLD_MESSAGES
        assert cfg.MAX_RESPONSE_LENGTH == 3000
        assert cfg.MAX_POLL_ITERATIONS == 10
        assert cfg.SILENCE_TIMEOUT_S == 4
        assert cfg.POLLY_VOICE_ID == "Matthew"
        assert cfg.NOVA_SONIC_VOICE == "Matthew"
        assert cfg.LEX_BOT_ALIAS == ""
        assert cfg.NEXTHINK_AGENT_URL == ""
        assert cfg.NEXTHINK_API_KEY == ""
        assert cfg.CALLBACK_API_KEY == ""


# ------------------------------------------------------------------
# SSM loading
# ------------------------------------------------------------------

class TestSSMLoading:
    @mock_aws
    def test_loads_int_params_from_ssm(self):
        ssm = boto3.client("ssm", region_name="us-east-1")
        _put_ssm(ssm, f"{SSM_PREFIX}polling-interval-ms", "5000")
        _put_ssm(ssm, f"{SSM_PREFIX}max-session-duration-s", "600")
        _put_ssm(ssm, f"{SSM_PREFIX}max-poll-iterations", "20")

        cfg = Config.load()
        assert cfg.POLLING_INTERVAL_MS == 5000
        assert cfg.MAX_SESSION_DURATION_S == 600
        assert cfg.MAX_POLL_ITERATIONS == 20

    @mock_aws
    def test_loads_string_params_from_ssm(self):
        ssm = boto3.client("ssm", region_name="us-east-1")
        _put_ssm(ssm, f"{SSM_PREFIX}polly-voice-id", "Joanna")
        _put_ssm(ssm, f"{SSM_PREFIX}nova-sonic-voice", "Amy")
        _put_ssm(ssm, f"{SSM_PREFIX}lex-bot-alias", "arn:aws:lex:us-east-1:123:bot-alias/BOT/ALIAS")
        _put_ssm(ssm, f"{SSM_PREFIX}nexthink-agent-url", "https://nexthink.example.com/api")

        cfg = Config.load()
        assert cfg.POLLY_VOICE_ID == "Joanna"
        assert cfg.NOVA_SONIC_VOICE == "Amy"
        assert cfg.LEX_BOT_ALIAS == "arn:aws:lex:us-east-1:123:bot-alias/BOT/ALIAS"
        assert cfg.NEXTHINK_AGENT_URL == "https://nexthink.example.com/api"

    @mock_aws
    def test_loads_hold_message_pool_json(self):
        ssm = boto3.client("ssm", region_name="us-east-1")
        messages = ["Hold on!", "Almost there!", "Working on it!"]
        _put_ssm(ssm, f"{SSM_PREFIX}hold-message-pool", json.dumps(messages))

        cfg = Config.load()
        assert cfg.HOLD_MESSAGE_POOL == messages

    @mock_aws
    def test_custom_ssm_prefix(self):
        ssm = boto3.client("ssm", region_name="us-east-1")
        _put_ssm(ssm, "/custom/prefix/polling-interval-ms", "9999")

        cfg = Config.load(ssm_prefix="/custom/prefix/")
        assert cfg.POLLING_INTERVAL_MS == 9999


# ------------------------------------------------------------------
# Environment variable override
# ------------------------------------------------------------------

class TestEnvVarOverride:
    @mock_aws
    def test_env_var_takes_precedence_over_ssm(self, monkeypatch):
        ssm = boto3.client("ssm", region_name="us-east-1")
        _put_ssm(ssm, f"{SSM_PREFIX}polling-interval-ms", "5000")
        monkeypatch.setenv("POLLING_INTERVAL_MS", "7000")

        cfg = Config.load()
        assert cfg.POLLING_INTERVAL_MS == 7000

    @mock_aws
    def test_env_var_overrides_default(self, monkeypatch):
        monkeypatch.setenv("MAX_SESSION_DURATION_S", "120")

        cfg = Config.load()
        assert cfg.MAX_SESSION_DURATION_S == 120

    @mock_aws
    def test_env_var_hold_message_pool(self, monkeypatch):
        msgs = ["Wait please."]
        monkeypatch.setenv("HOLD_MESSAGE_POOL", json.dumps(msgs))

        cfg = Config.load()
        assert cfg.HOLD_MESSAGE_POOL == msgs

    @mock_aws
    def test_env_var_string_params(self, monkeypatch):
        monkeypatch.setenv("NEXTHINK_AGENT_URL", "https://env.example.com")
        monkeypatch.setenv("LEX_BOT_ALIAS", "arn:aws:lex:us-east-1:123:bot-alias/X/Y")

        cfg = Config.load()
        assert cfg.NEXTHINK_AGENT_URL == "https://env.example.com"
        assert cfg.LEX_BOT_ALIAS == "arn:aws:lex:us-east-1:123:bot-alias/X/Y"

    @mock_aws
    def test_ssm_prefix_from_env(self, monkeypatch):
        monkeypatch.setenv("SSM_PREFIX", "/from-env/")
        ssm = boto3.client("ssm", region_name="us-east-1")
        _put_ssm(ssm, "/from-env/silence-timeout-s", "8")

        cfg = Config.load()
        assert cfg.SILENCE_TIMEOUT_S == 8


# ------------------------------------------------------------------
# Secrets Manager
# ------------------------------------------------------------------

class TestSecretsManager:
    @mock_aws
    def test_loads_secrets(self, monkeypatch):
        monkeypatch.setenv("NEXTHINK_API_KEY_SECRET_NAME", "nexthink-key")
        monkeypatch.setenv("CALLBACK_API_KEY_SECRET_NAME", "callback-key")

        sm = boto3.client("secretsmanager", region_name="us-east-1")
        sm.create_secret(Name="nexthink-key", SecretString="nk-secret-123")
        sm.create_secret(Name="callback-key", SecretString="cb-secret-456")

        cfg = Config.load()
        assert cfg.NEXTHINK_API_KEY == "nk-secret-123"
        assert cfg.CALLBACK_API_KEY == "cb-secret-456"

    @mock_aws
    def test_no_secrets_when_env_vars_absent(self):
        cfg = Config.load()
        assert cfg.NEXTHINK_API_KEY == ""
        assert cfg.CALLBACK_API_KEY == ""

    @mock_aws
    def test_loads_only_one_secret(self, monkeypatch):
        monkeypatch.setenv("NEXTHINK_API_KEY_SECRET_NAME", "only-nexthink")

        sm = boto3.client("secretsmanager", region_name="us-east-1")
        sm.create_secret(Name="only-nexthink", SecretString="single-secret")

        cfg = Config.load()
        assert cfg.NEXTHINK_API_KEY == "single-secret"
        assert cfg.CALLBACK_API_KEY == ""


# ------------------------------------------------------------------
# Singleton / get_config
# ------------------------------------------------------------------

class TestGetConfig:
    @mock_aws
    def test_returns_cached_instance(self):
        c1 = get_config(_force_reload=True)
        c2 = get_config()
        assert c1 is c2

    @mock_aws
    def test_force_reload_creates_new_instance(self):
        c1 = get_config(_force_reload=True)
        c2 = get_config(_force_reload=True)
        assert c1 is not c2
