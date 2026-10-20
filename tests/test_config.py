import base64
import json
import logging

import httpx
import pytest

from cuesync.config import (
    InvalidConfig,
    RedactConfigFilter,
    UserConfig,
    decode_config,
    encode_config,
    redact_path,
)
from cuesync.debrid import RealDebrid, debrid_provider

FULL = UserConfig(subdl_api_key="subdl-key-789", debrid_api_key="rd-key-abc")


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def test_round_trip_all_fields():
    assert decode_config(encode_config(FULL)) == FULL


def test_round_trip_without_keys():
    config = UserConfig()
    assert decode_config(encode_config(config)) == config


def test_token_is_url_safe_and_unpadded():
    config = UserConfig(subdl_api_key="???>>>~~~")
    token = encode_config(config)
    assert not set(token) & {"=", "+", "/"}


def test_padded_token_decodes():
    token = encode_config(FULL)
    padded = token + "=" * (-len(token) % 4)
    assert decode_config(padded) == FULL


@pytest.mark.parametrize(
    "token",
    [
        "!!!not base64!!!",
        b64("this is not json"),
        b64('["eng"]'),
        b64('{"subdl_api_key": ["secret-key"]}'),
        "é",
    ],
)
def test_invalid_input_raises_without_echoing_it(token):
    with pytest.raises(InvalidConfig) as error:
        decode_config(token)
    assert token not in str(error.value)
    assert "secret-key" not in str(error.value)


def test_blank_optional_fields_become_none():
    config = UserConfig(subdl_api_key="  ", debrid_api_key="")
    assert config.subdl_api_key is None
    assert config.debrid_api_key is None


def test_unknown_fields_are_ignored():
    config = decode_config(b64('{"subdl_api_key": "k", "future_setting": 1}'))
    assert config == UserConfig(subdl_api_key="k")


def test_old_link_settings_are_ignored_and_another_debrid_key_is_dropped():
    old = {
        "languages": ["eng", "vie"],
        "opensubtitles_api_key": "os-key",
        "opensubtitles_username": "user",
        "opensubtitles_password": "pass",
        "subdl_api_key": "subdl-key",
        "max_per_language": 8,
        "debrid_provider": "torbox",
        "debrid_api_key": "other-service-key",
    }
    config = decode_config(b64(json.dumps(old)))
    assert config == UserConfig(subdl_api_key="subdl-key")
    assert config.debrid_api_key is None
    assert debrid_provider(config, httpx.AsyncClient()) is None


def test_old_link_with_only_opensubtitles_keys_decodes():
    config = decode_config(b64('{"languages": ["eng"], "opensubtitles_api_key": "k"}'))
    assert config == UserConfig()


def test_old_realdebrid_link_keeps_its_key():
    token = b64('{"debrid_provider": "realdebrid", "debrid_api_key": "k"}')
    config = decode_config(token)
    assert config.debrid_api_key == "k"
    provider = debrid_provider(config, httpx.AsyncClient())
    assert isinstance(provider, RealDebrid)


def test_debrid_key_without_provider_is_kept():
    assert decode_config(b64('{"debrid_api_key": "k"}')).debrid_api_key == "k"


def test_repr_hides_secrets():
    text = repr(FULL)
    for secret in ("subdl-key-789", "rd-key-abc"):
        assert secret not in text


def test_redact_path():
    token = encode_config(FULL)
    assert redact_path("/health") == "/health"
    assert redact_path("/manifest.json") == "/manifest.json"
    assert redact_path("/configure") == "/configure"
    assert redact_path("/") == "/"
    assert redact_path(f"/{token}/subtitles/movie/tt1.json") == "/<config>/subtitles/movie/tt1.json"
    assert redact_path(f"/{token}") == "/<config>"
    assert redact_path(f"/{token}?a=b") == "/<config>?a=b"


def test_filter_redacts_path_args_only():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, "%s %s %d", ("GET", "/abc/manifest.json", 200), None
    )
    assert RedactConfigFilter().filter(record)
    assert record.getMessage() == "GET /<config>/manifest.json 200"
