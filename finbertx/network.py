from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class JsonResponse:
    data: Any
    headers: Mapping[str, str]


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    payload: dict | None = None,
    timeout: int = 20,
) -> JsonResponse:
    body = None
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")

    request = Request(
        url,
        data=body,
        headers=request_headers,
        method=method.upper(),
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            response_headers = {
                key.lower(): value for key, value in response.headers.items()
            }
    except HTTPError as exc:
        raise ApiError(
            f"Upstream service returned HTTP {exc.code}.", status=exc.code
        ) from None
    except URLError as exc:
        reason_name = type(exc.reason).__name__
        raise ApiError(f"Could not reach upstream service ({reason_name}).") from None
    except TimeoutError:
        raise ApiError("Upstream service timed out.") from None

    if not raw:
        return JsonResponse(data={}, headers=response_headers)
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError("Upstream service returned invalid JSON.") from None
    return JsonResponse(data=decoded, headers=response_headers)
