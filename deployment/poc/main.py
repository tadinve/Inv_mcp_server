"""Cloud Run token-forwarding proof-of-concept (SPEC-AMENDMENT-1, A1.1).

Answers one question: when Cloud Run IAM admits a request, does the Google-signed
ID token in `Authorization` reach the container with its signature intact, so
the application can verify it independently?

For each of `Authorization` and `X-Serverless-Authorization` it reports only
structure and verification results. It never returns or logs a token, a
signature, or a subject.
"""

from __future__ import annotations

import os

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

EXPECTED_AUDIENCE = os.environ["EXPECTED_AUDIENCE"]
_transport = google_requests.Request()


def inspect_header(raw: str | None) -> dict[str, object]:
    if raw is None:
        return {"present": False}
    scheme, _, token = raw.partition(" ")
    segments = token.split(".") if token else []
    report: dict[str, object] = {
        "present": True,
        "scheme_is_bearer": scheme.lower() == "bearer",
        "jwt_segments": len(segments),
        "signature_segment_length": len(segments[2]) if len(segments) == 3 else None,
    }
    try:
        claims = id_token.verify_oauth2_token(token, _transport, audience=EXPECTED_AUDIENCE)
    except Exception as exc:  # report the category only, never the message (it can quote claims)
        report.update(signature_verifies=False, verify_error=type(exc).__name__)
        return report
    report.update(
        signature_verifies=True,
        issuer=claims.get("iss"),
        audience_matches=claims.get("aud") == EXPECTED_AUDIENCE,
        subject_present=bool(claims.get("sub")),
        email_verified=claims.get("email_verified") is True,
    )
    return report


async def probe(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "authorization": inspect_header(request.headers.get("authorization")),
            "x_serverless_authorization": inspect_header(request.headers.get("x-serverless-authorization")),
        }
    )


app = Starlette(routes=[Route("/probe", probe, methods=["GET"])])
