"""Letta proxy routes - forwards requests to Letta server to avoid CORS."""

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/v1/letta", tags=["letta"])

LETTA_BASE_URL = "http://neo-letta:8283"


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_letta(path: str, request: Request):
    """Proxy all requests to Letta server."""
    url = f"{LETTA_BASE_URL}/{path}"
    headers = dict(request.headers)
    headers.pop("host", None)

    body = await request.body()

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            response = await client.request(
                method=request.method,
                url=url,
                headers=headers,
                content=body if body else None,
            )
            return JSONResponse(
                content=response.json() if response.text else {},
                status_code=response.status_code,
                headers=dict(response.headers),
            )
        except httpx.ConnectError:
            return JSONResponse(
                content={"error": "Letta server not available", "url": url},
                status_code=503,
            )
        except Exception as e:
            return JSONResponse(
                content={"error": str(e)},
                status_code=500,
            )