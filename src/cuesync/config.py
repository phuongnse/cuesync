"""Per-user settings, their encoding in the addon URL, and log redaction.

Stremio addons carry the user's settings in the install URL. CueSync puts them in the
first path segment as unpadded base64url JSON: `/{config}/manifest.json`.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

LANGUAGE_RE = re.compile(r"^[a-z]{3}$")
OPTIONAL_FIELDS = (
    "opensubtitles_api_key",
    "opensubtitles_username",
    "opensubtitles_password",
    "subdl_api_key",
    "debrid_provider",
    "debrid_api_key",
)
# First path segments that are not a config and so are safe to log.
PLAIN_SEGMENTS = frozenset({"health", "manifest.json", "configure"})
FIRST_SEGMENT_RE = re.compile(r"^/([^/?#]+)")


class InvalidConfig(ValueError):
    """The config segment of a URL could not be decoded or is not valid."""


class UserConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    languages: list[str] = Field(min_length=1)
    opensubtitles_api_key: str | None = Field(default=None, repr=False)
    opensubtitles_username: str | None = Field(default=None, repr=False)
    opensubtitles_password: str | None = Field(default=None, repr=False)
    subdl_api_key: str | None = Field(default=None, repr=False)
    debrid_provider: Literal["realdebrid", "torbox"] | None = None
    debrid_api_key: str | None = Field(default=None, repr=False)

    @field_validator(*OPTIONAL_FIELDS, mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("languages")
    @classmethod
    def _check_languages(cls, value: list[str]) -> list[str]:
        languages: list[str] = []
        for code in value:
            code = code.strip().lower()
            if not LANGUAGE_RE.match(code):
                raise ValueError("language codes are three lowercase letters")
            if code not in languages:
                languages.append(code)
        return languages

    @model_validator(mode="after")
    def _check_dependencies(self) -> UserConfig:
        if self.debrid_provider and not self.debrid_api_key:
            raise ValueError("a debrid provider needs its API key")
        if bool(self.opensubtitles_username) != bool(self.opensubtitles_password):
            raise ValueError("OpenSubtitles username and password go together")
        return self


def encode_config(config: UserConfig) -> str:
    raw = json.dumps(config.model_dump(exclude_none=True), separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_config(token: str) -> UserConfig:
    """Decode a config segment. The error never includes the input, which holds secrets."""
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        return UserConfig.model_validate(json.loads(raw.decode("utf-8")))
    except (binascii.Error, UnicodeError, ValueError, ValidationError):
        raise InvalidConfig("invalid configuration") from None


def redact_path(path: str) -> str:
    """Replace the first path segment with `<config>` unless it is a known plain route."""
    match = FIRST_SEGMENT_RE.match(path)
    if match is None or match.group(1) in PLAIN_SEGMENTS:
        return path
    return f"/<config>{path[match.end() :]}"


class RedactConfigFilter(logging.Filter):
    """Keeps the config segment, which holds API keys, out of uvicorn's access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_path(arg) if isinstance(arg, str) and arg.startswith("/") else arg
                for arg in record.args
            )
        return True
