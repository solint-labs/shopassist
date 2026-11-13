"""Shared fixtures: an isolated app with a temp SQLite DB, fake LLM and mocked Graph API."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.channels.signature import compute_signature
from app.llm.fake import FakeLLM
from app.main import create_app
from app.settings import Settings
from tests.payloads import IG_ID, PAGE_ID, WA_PHONE_ID

ROOT = Path(__file__).resolve().parents[1]
APP_SECRET = "test-app-secret"
GRAPH = "https://graph.facebook.com/v27.0"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        tenant_config_path=str(ROOT / "config/tenants/brewline.yaml"),
        llm_provider="fake",
        meta_app_secret=SecretStr(APP_SECRET),
        meta_verify_token=SecretStr("verify-me"),
        whatsapp_phone_number_id=WA_PHONE_ID,
        whatsapp_token=SecretStr("wa-token"),
        page_id=PAGE_ID,
        page_access_token=SecretStr("page-token"),
        instagram_account_id=IG_ID,
    )


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def graph_mock():
    """Intercept every Graph API call and record it."""
    with respx.mock(base_url=GRAPH, assert_all_called=False) as mock:
        mock.post(url__regex=r".*").mock(return_value=httpx.Response(200, json={"message_id": "out_1"}))
        yield mock


@pytest.fixture
async def app(settings, fake_llm, graph_mock):
    application = create_app(settings, llm=fake_llm)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def signed(payload: dict) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    return body, {"x-hub-signature-256": compute_signature(APP_SECRET, body), "content-type": "application/json"}


def sent_bodies(graph_mock) -> list[tuple[str, dict]]:
    """(path, json body) for every Graph call made, in order."""
    return [(c.request.url.path, json.loads(c.request.content or b"{}")) for c in graph_mock.calls]
