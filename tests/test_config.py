import base64
import logging

import pytest

from cuesync.config import (
    InvalidConfig,
    RedactConfigFilter,
    UserConfig,
    decode_config,
    encode_config,
    redact_path,
)

FULL = UserConfig(
    languages=["vie", "eng"],
    opensubtitles_api_key="os-key-123",
    opensubtitles_username="user",
    opensubtitles_password="pass-456",
    subdl_api_key="subdl-key-789",
    debrid_provider="realdebrid",
    debrid_api_key="rd-key-abc",
    max_per_language=8,
)


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def test_round_trip_all_fields():
    assert decode_config(encode_config(FULL)) == FULL


def test_round_trip_languages_only():
    config = UserConfig(languages=["eng"])
    assert decode_config(encode_config(config)) == config


def test_max_per_language_defaults_to_five_and_stays_in_range():
    assert UserConfig(languages=["eng"]).max_per_language == 5
    assert UserConfig(languages=["eng"], max_per_language=1).max_per_language == 1
    assert UserConfig(languages=["eng"], max_per_language=20).max_per_language == 20
    for bad in (0, 21, -1):
        with pytest.raises(ValueError):
            UserConfig(languages=["eng"], max_per_language=bad)


def test_token_is_url_safe_and_unpadded():
    config = UserConfig(languages=["eng"], subdl_api_key="???>>>~~~")
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
        b64('{"languages": []}'),
        b64('{"languages": ["english"]}'),
        b64('{"languages": ["eng"], "debrid_provider": "alldebrid", "debrid_api_key": "k"}'),
        b64('{"languages": ["eng"], "debrid_provider": "torbox"}'),
        b64('{"languages": ["eng"], "opensubtitles_username": "secret-user"}'),
        "é",
    ],
)
def test_invalid_input_raises_without_echoing_it(token):
    with pytest.raises(InvalidConfig) as error:
        decode_config(token)
    assert token not in str(error.value)
    assert "secret-user" not in str(error.value)


def test_languages_are_normalized():
    config = UserConfig(languages=[" ENG ", "vie", "eng"])
    assert config.languages == ["eng", "vie"]


def test_blank_optional_fields_become_none():
    config = UserConfig(languages=["eng"], subdl_api_key="  ", debrid_provider="")
    assert config.subdl_api_key is None
    assert config.debrid_provider is None


def test_unknown_fields_are_ignored():
    config = decode_config(b64('{"languages": ["eng"], "future_setting": 1}'))
    assert config == UserConfig(languages=["eng"])


def test_repr_hides_secrets():
    text = repr(FULL)
    for secret in ("os-key-123", "pass-456", "subdl-key-789", "rd-key-abc"):
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
