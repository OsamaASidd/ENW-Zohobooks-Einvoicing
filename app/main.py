"""Live Swagger UI explorer for the Zoho Books API.

Merges every OpenAPI file in ../API into one spec so Swagger UI (/docs)
shows all resources and operations. GET operations are wired to real,
live calls against your Zoho Books organization; other HTTP methods are
shown for reference only and return 501 if invoked, since this explorer
is read-only by design.
"""

import os
import ssl
from pathlib import Path

import httpx
import truststore
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse, Response

from spec_builder import build_merged_spec, get_paths_with_method
from zoho_auth import ZohoAuth, ZohoAuthError

# Use the OS certificate store (not just certifi's bundle) so this works
# behind corporate SSL-inspecting proxies whose root CA is trusted by
# Windows but not bundled with Python.
SSL_CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

BASE_DIR = Path(__file__).resolve().parent
API_SPECS_DIR = BASE_DIR.parent / "API"
ENV_PATH = BASE_DIR / ".env"

load_dotenv(ENV_PATH)

ZOHO_CLIENT_ID = os.getenv("ZOHO_CLIENT_ID", "")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET", "")
ZOHO_ORGANIZATION_ID = os.getenv("ZOHO_ORGANIZATION_ID", "")
ZOHO_GRANT_TOKEN = os.getenv("ZOHO_GRANT_TOKEN", "") or None
ZOHO_REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN", "") or None
ZOHO_ACCOUNTS_BASE = os.getenv("ZOHO_ACCOUNTS_BASE", "https://accounts.zoho.com")
ZOHO_API_BASE = os.getenv("ZOHO_API_BASE", "https://www.zohoapis.com/books/v3")

auth = ZohoAuth(
    client_id=ZOHO_CLIENT_ID,
    client_secret=ZOHO_CLIENT_SECRET,
    accounts_base=ZOHO_ACCOUNTS_BASE,
    env_path=ENV_PATH,
    refresh_token=ZOHO_REFRESH_TOKEN,
    ssl_context=SSL_CONTEXT,
)

http_client = httpx.AsyncClient(timeout=60.0, verify=SSL_CONTEXT)

MERGED_SPEC = build_merged_spec(
    API_SPECS_DIR,
    title="Zoho Books API Explorer (live GET)",
    description=(
        "Auto-generated from the Zoho Books OpenAPI specs in /API. GET operations "
        "are wired to live calls against your Zoho Books organization. Other "
        "methods are documented for reference only and return 501 here - this "
        "explorer is read-only by design.\n\n"
        "organization_id is auto-filled from ZOHO_ORGANIZATION_ID if you leave it "
        "blank. Check /auth/status if you get 401s."
    ),
)

# organization_id is required in every operation's spec; default it so
# "Try it out" works without retyping it on every single call. Swagger UI
# autofills from the parameter's own `example` before `schema.default`, so
# both must be overridden - the spec's placeholder ("10234695") otherwise
# wins and every call fails with "user is not associated with CompanyID".
for param in MERGED_SPEC.get("components", {}).get("parameters", {}).values():
    if param.get("name") == "organization_id":
        param["required"] = False
        param["example"] = ZOHO_ORGANIZATION_ID
        param.setdefault("schema", {})["default"] = ZOHO_ORGANIZATION_ID
        param["schema"]["example"] = ZOHO_ORGANIZATION_ID

GET_PATHS = get_paths_with_method(MERGED_SPEC["paths"], "get")

# Some yml `example:` values parse as Python date/datetime objects (bare
# dates in YAML); jsonable_encoder normalizes those to JSON-safe strings.
MERGED_SPEC = jsonable_encoder(MERGED_SPEC)

app = FastAPI(openapi_url="/openapi.json", docs_url="/docs", redoc_url=None)
app.openapi = lambda: MERGED_SPEC


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/docs")


@app.get("/auth/status", tags=["auth"], summary="Check current Zoho token status")
async def auth_status():
    return auth.status()


@app.post(
    "/auth/exchange",
    tags=["auth"],
    summary="Exchange a fresh Zoho grant token for a refresh token",
    description=(
        "Grant tokens are single-use and expire within minutes. Generate a new "
        "one in the Zoho API Console (Self Client tab) and POST it here as "
        '{"grant_token": "..."} to (re)establish the refresh token this app '
        "needs for all other calls."
    ),
)
async def auth_exchange(body: dict):
    grant_token = (body or {}).get("grant_token")
    if not grant_token:
        return JSONResponse({"detail": "grant_token is required"}, status_code=400)
    try:
        data = await auth.exchange_grant_token(grant_token)
    except ZohoAuthError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    return {"status": "ok", "expires_in": data.get("expires_in")}


async def proxy_get(request: Request):
    if request.method != "GET":
        return JSONResponse(
            {
                "detail": (
                    f"{request.method} is documented for reference but not "
                    "implemented - this explorer only executes GET requests."
                )
            },
            status_code=501,
        )

    query = dict(request.query_params)
    if not query.get("organization_id") and ZOHO_ORGANIZATION_ID:
        query["organization_id"] = ZOHO_ORGANIZATION_ID

    try:
        token = await auth.get_access_token()
    except ZohoAuthError as e:
        return JSONResponse({"detail": str(e)}, status_code=401)

    url = f"{ZOHO_API_BASE}{request.scope['path']}"
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}

    try:
        resp = await http_client.get(url, params=query, headers=headers)
    except httpx.HTTPError as e:
        return JSONResponse({"detail": f"Error calling Zoho: {e}"}, status_code=502)

    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            return JSONResponse(resp.json(), status_code=resp.status_code)
        except ValueError:
            pass
    return Response(resp.content, status_code=resp.status_code, media_type=content_type or None)


ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
for path in GET_PATHS:
    app.add_api_route(path, proxy_get, methods=ALL_METHODS, include_in_schema=False)


@app.on_event("startup")
async def on_startup():
    if not auth.refresh_token and ZOHO_GRANT_TOKEN:
        try:
            await auth.exchange_grant_token(ZOHO_GRANT_TOKEN)
            print("Zoho grant token exchanged successfully - refresh token stored in .env")
        except ZohoAuthError as e:
            print(f"WARNING: could not exchange ZOHO_GRANT_TOKEN on startup: {e}")
    print(f"Loaded {len(GET_PATHS)} live GET endpoints from {API_SPECS_DIR}")


@app.on_event("shutdown")
async def on_shutdown():
    await http_client.aclose()
    await auth.aclose()
