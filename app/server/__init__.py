"""HTTP/SSE layer of Deep Researcher (FastAPI app factory, exporters, schemas)."""
from app.server.app import create_app

__all__ = ["create_app"]
