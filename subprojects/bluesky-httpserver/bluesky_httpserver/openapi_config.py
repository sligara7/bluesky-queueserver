"""
OpenAPI metadata and schema customization.

Split out of ``app.py`` so ``scripts/export_openapi.py`` can import these
without pulling in the runtime deps (REManagerAPI, SQLAlchemy, ZMQ, etc.)
that ``app.py`` drags in at import time.
"""
from fastapi.openapi.utils import get_openapi


OPENAPI_DESCRIPTION = """\
REST + WebSocket surface of the Bluesky Queue Server. Drives an experiment from
three angles: **queue** (plans and instructions waiting to run), **Run Engine**
(control over the currently-running plan), and **worker environment** (startup
scripts, plans, devices, and tasks inside the worker process).

All endpoints live under `/api`. Clients authenticate with a bearer token
(access or API key); anonymous access, when enabled, is scoped to `read:status`
only. Most endpoints return `{"success": bool, "msg": str, ...}` envelopes —
inspect `success` on each response.

### WebSocket endpoints (not listed below)

Three push streams are available; they do not render in Swagger UI because
OpenAPI has no WebSocket concept:

- `GET /api/status/ws` — pushes the `/api/status` payload as it changes
  (scope `read:monitor`).
- `GET /api/info/ws` — pushes system-info snapshots (scope `read:monitor`).
- `GET /api/console_output/ws` — pushes captured worker stdout/stderr
  (scope `read:console`).

Clients authenticate WebSockets by passing the bearer token via a cookie or
the `Authorization` header on the upgrade request.
"""


OPENAPI_TAGS_METADATA = [
    {"name": "Status", "description": "Liveness and the manager status snapshot used for polling."},
    {"name": "Config", "description": "Client-visible subset of the manager's configuration."},
    {"name": "Queue", "description": "Queue-level control: mode, autostart, start, stop, clear."},
    {"name": "Queue Items", "description": "Add, remove, move, update, and execute individual queue items."},
    {"name": "History", "description": "Completed-plan history."},
    {
        "name": "Environment",
        "description": "Lifecycle of the RE Worker subprocess that hosts the Run Engine.",
    },
    {
        "name": "Run Engine",
        "description": "Control the currently-running plan: pause, resume, stop, abort, halt.",
    },
    {"name": "Runs", "description": "Bluesky runs produced by the currently-running plan."},
    {"name": "Plans", "description": "Plans registered in the worker namespace, filtered by permissions."},
    {"name": "Devices", "description": "Devices registered in the worker namespace, filtered by permissions."},
    {"name": "Permissions", "description": "User-group permissions and allowed-plan/allowed-device definitions."},
    {
        "name": "Scripts & Functions",
        "description": "Run user code in the worker: upload scripts, execute named functions, poll task status.",
    },
    {"name": "Lock", "description": "Exclusive lock on the manager to prevent concurrent state changes."},
    {"name": "Manager", "description": "Lifecycle control of the manager process itself."},
    {"name": "Testing", "description": "Endpoints that exist solely to exercise client-side error paths."},
    {
        "name": "Console Output",
        "description": "Access captured worker stdout/stderr (poll, stream, or buffer UID).",
    },
    {
        "name": "Auth",
        "description": (
            "Session, API-key, and identity endpoints. Each configured authentication "
            "provider also exposes provider-specific routes under `/api/auth/provider/{provider}/`."
        ),
    },
]


def custom_openapi(app):
    """
    Build and cache the app's OpenAPI schema.

    Monkey-patched onto the FastAPI app as ``app.openapi`` per
    https://fastapi.tiangolo.com/advanced/extending-openapi/.
    """
    from . import __version__

    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title="Bluesky HTTP Server",
        version=__version__,
        description=OPENAPI_DESCRIPTION,
        routes=app.routes,
        tags=OPENAPI_TAGS_METADATA,
    )
    # Insert refreshUrl (absent when /docs triggers a schema build with no auth configured).
    if "securitySchemes" in openapi_schema["components"]:
        openapi_schema["components"]["securitySchemes"]["OAuth2PasswordBearer"]["flows"]["password"][
            "refreshUrl"
        ] = "token/refresh"
    app.openapi_schema = openapi_schema
    return app.openapi_schema
