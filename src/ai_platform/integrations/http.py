"""Small JSON HTTP boundary used by provider clients and replaced in tests."""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    data: Any


class ProviderHttpError(RuntimeError):
    """An error that intentionally retains no provider response body or request secrets."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Provider request failed with HTTP {status}.")


class JsonHttpClient(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> HttpResponse: ...


class UrllibJsonClient:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> HttpResponse:
        encoded = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=encoded, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
                payload = response.read()
                data = json.loads(payload) if payload else None
                return HttpResponse(response.status, data)
        except urllib.error.HTTPError as error:
            # Never retain/log the response body: OAuth servers may echo request details.
            raise ProviderHttpError(error.code) from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise ProviderHttpError(503) from error
