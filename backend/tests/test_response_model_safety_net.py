"""A response that breaks its declared model is served, not turned into a 500 (ADR §3.2)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.errors import STRICT_RESPONSE_MODELS_ENV, install_error_handlers


class Typed(BaseModel):
    name: str


def _app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/broken", response_model=Typed)
    async def broken():
        return {"name": None, "extra": 1}

    @app.post("/created", response_model=Typed, status_code=201)
    async def created():
        return {"name": None}

    return app


class SafetyNetTests(unittest.TestCase):
    def test_production_serves_the_unvalidated_body_with_the_route_status(self) -> None:
        with patch.dict(os.environ, {STRICT_RESPONSE_MODELS_ENV: "0"}):
            client = TestClient(_app())
            with self.assertLogs("app.core.errors", level="ERROR"):
                response = client.get("/broken")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"name": None, "extra": 1})
            with self.assertLogs("app.core.errors", level="ERROR"):
                self.assertEqual(client.post("/created").status_code, 201)

    def test_strict_mode_fails_loudly(self) -> None:
        with patch.dict(os.environ, {STRICT_RESPONSE_MODELS_ENV: "1"}):
            client = TestClient(_app())
            with self.assertRaises(Exception):
                client.get("/broken")


if __name__ == "__main__":
    unittest.main()
