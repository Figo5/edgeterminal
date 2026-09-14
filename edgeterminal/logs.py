"""Log hygiene: credentials must never be written to a log sink.

The Odds API (and several other providers) authenticate with a query-string
key, and httpx logs whole request URLs at INFO. Those loggers are therefore
raised to WARNING and every remaining record is scrubbed of key-shaped text.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Iterable, List, Optional

REDACTED = "[REDACTED]"

_PATTERNS = (
    # `%` is excluded from the value class on purpose: `apiKey=%s` is a logging
    # placeholder, not a value, and must survive so the record still formats.
    re.compile(r"(api[_-]?key=)[^&\s\"'<>%]+", re.I),
    re.compile(r"(x-api-key[=:\s]+)[^&\s\"'<>%]+", re.I),
    re.compile(r"(bearer\s+)[A-Za-z0-9._\-]{6,}", re.I),
)

_URL_LOGGERS = ("httpx", "httpcore", "hpack")
_OTHER_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access", "edgeterminal")
_MIN_SECRET_LEN = 8


def env_secrets() -> List[str]:
    """Every plausible credential value in the environment (never printed)."""
    out: List[str] = []
    for name, value in os.environ.items():
        if not value or len(value) < _MIN_SECRET_LEN:
            continue
        if name.upper().endswith(("_API_KEY", "_KEY", "_SECRET", "_TOKEN", "_PASSWORD")):
            out.append(value)
    return out


class RedactSecrets(logging.Filter):
    """Scrub key-shaped text from a record's message and its arguments."""

    def __init__(self, secrets: Optional[Iterable[str]] = None):
        super().__init__(name="edgeterminal.redact")
        self.secrets = [s for s in (secrets or []) if s and len(s) >= _MIN_SECRET_LEN]
        self.secrets.sort(key=len, reverse=True)

    def scrub(self, text: str) -> str:
        if not isinstance(text, str):
            return text
        for pattern in _PATTERNS:
            text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
        for secret in self.secrets:
            text = text.replace(secret, REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        try:
            # Scrub the arguments too -- httpx logs the whole request URL (key
            # included) as an %s argument -- but never touch placeholder text, or
            # the record's own formatting breaks.
            if isinstance(record.args, dict):
                record.args = {k: self.scrub(v) for k, v in record.args.items()}
            elif record.args:
                record.args = tuple(self.scrub(a) for a in record.args)
            record.msg = self.scrub(str(record.msg))
        except Exception:  # noqa: BLE001 - logging must never raise
            return True
        return True


def install_redaction(secrets: Optional[Iterable[str]] = None) -> RedactSecrets:
    """Idempotently attach redaction and raise URL-logging loggers to WARNING."""
    filt = RedactSecrets(list(secrets or []) + env_secrets())
    for name in _OTHER_LOGGERS:
        logger = logging.getLogger(name) if name else logging.getLogger()
        if not any(isinstance(f, RedactSecrets) for f in logger.filters):
            logger.addFilter(filt)
        for handler in list(logger.handlers):
            if not any(isinstance(f, RedactSecrets) for f in handler.filters):
                handler.addFilter(filt)
    for name in _URL_LOGGERS:
        logger = logging.getLogger(name)
        if logger.level < logging.WARNING or logger.level == logging.NOTSET:
            logger.setLevel(logging.WARNING)
        if not any(isinstance(f, RedactSecrets) for f in logger.filters):
            logger.addFilter(filt)
    return filt
