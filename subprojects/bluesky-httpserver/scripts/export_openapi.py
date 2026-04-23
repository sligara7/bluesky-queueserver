"""
Dump the static bluesky-httpserver OpenAPI schema to `openapi.json`.

The checked-in artifact intentionally reflects the **bare** server — static
core routes plus the built-in `/api/auth/*` endpoints — and omits the dynamic
`/api/auth/provider/{provider}/*` routes that are registered at runtime based
on deployment-specific auth configuration. SDK generators should regenerate
per-deployment if provider routes are needed.

Usage:
    python scripts/export_openapi.py
    python scripts/export_openapi.py -o some/other/path.json
"""
import argparse
import json
import sys
from functools import partial
from pathlib import Path

from fastapi import FastAPI

from bluesky_httpserver.authentication import base_authentication_router
from bluesky_httpserver.openapi_config import custom_openapi
from bluesky_httpserver.routers import core_api


def build_schema() -> dict:
    app = FastAPI()
    app.include_router(core_api.router)
    app.include_router(base_authentication_router, prefix="/api/auth")
    app.openapi = partial(custom_openapi, app)
    return app.openapi()


def main() -> int:
    default_output = Path(__file__).resolve().parent.parent / "openapi.json"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", type=Path, default=default_output)
    args = parser.parse_args()

    schema = build_schema()
    args.output.write_text(json.dumps(schema, indent=2) + "\n")
    print(f"Wrote {args.output} ({len(schema['paths'])} paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
