"""Credentials must never reach a log sink.

Provider URLs carry keys as query parameters and httpx logs full request URLs,
so the URL-bearing loggers are silenced and key-shaped text is scrubbed from
whatever still gets through.
"""
import logging

import pytest

from edgeterminal import logs


def test_redaction_filter_scrubs_query_keys_bearer_tokens_and_secrets():
    filt = logs.RedactSecrets(["supersecretvalue"])
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1,
                              'GET https://x/odds?apiKey=%s&regions=us',
                              ("supersecretvalue",), None)
    assert filt.filter(record) is True
    message = record.getMessage()
    assert "supersecretvalue" not in message
    assert "apiKey=[REDACTED]" in message
    assert "regions=us" in message

    record = logging.LogRecord("edgeterminal", logging.WARNING, __file__, 1,
                               "auth failed for Bearer abc123token", None, None)
    logs.RedactSecrets().filter(record)
    assert "abc123token" not in record.getMessage()


def test_install_redaction_silences_url_loggers_and_is_idempotent():
    httpx_logger = logging.getLogger("httpx")
    prior = httpx_logger.level
    try:
        logs.install_redaction(["anothersecret"])
        assert httpx_logger.level >= logging.WARNING
        assert logging.getLogger("httpcore").level >= logging.WARNING
        assert any(isinstance(f, logs.RedactSecrets) for f in logging.getLogger().filters)
        logs.install_redaction(["anothersecret"])
        installed = [f for f in logging.getLogger().filters if isinstance(f, logs.RedactSecrets)]
        assert len(installed) == 1
    finally:
        httpx_logger.setLevel(prior)


def test_env_secrets_discovers_key_shaped_values(monkeypatch):
    monkeypatch.setenv("SOMETHING_API_KEY", "abcdef123456")
    monkeypatch.setenv("SHORT_KEY", "abc")
    found = logs.env_secrets()
    assert "abcdef123456" in found
    assert "abc" not in found


def test_filtered_records_can_still_be_formatted_repeatedly():
    """Query strings carry literal %XX escapes; formatting must never raise."""
    filt = logs.RedactSecrets(["topsecretvalue"])
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1,
                               'GET /odds?apiKey=%s&markets=player_pass_yds%%2Cplayer_rush_yds',
                               ("topsecretvalue",), None)
    filt.filter(record)
    first = record.getMessage()
    second = record.getMessage()
    assert first == second
    assert "topsecretvalue" not in first
    assert "player_pass_yds%2Cplayer_rush_yds" in first
