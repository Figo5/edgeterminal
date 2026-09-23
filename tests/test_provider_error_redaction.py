"""Failed authenticated requests must not disclose query credentials."""

import httpx
import pytest

from edgeterminal.providers import base


def test_http_failure_omits_query_credentials(monkeypatch):
    secret = "example-private-key-123"
    request = httpx.Request("GET", f"https://example.com/odds?apiKey={secret}")
    response = httpx.Response(401, request=request)

    class FailingClient:
        def get(self, *_args, **_kwargs):
            return response

    monkeypatch.setattr(base, "client", lambda: FailingClient())
    with pytest.raises(base.ProviderError) as error:
        base.get_json("https://example.com/odds", provider="test", retries=0)
    assert "HTTP 401" in error.value.detail
    assert "example.com/odds" in error.value.detail
    assert secret not in str(error.value)
    assert "apiKey" not in str(error.value)
