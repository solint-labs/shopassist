"""Part 4: multi-tenancy, RBAC + audit, analytics, per-customer personalisation."""

import asyncio
import shutil
from pathlib import Path

import httpx
import pytest
import yaml

from app.agent.tenant import load_tenant_config
from app.llm.base import LLMResponse
from app.main import create_app
from app.personalization.persona import StyleGuard, assign_variant, resolve_persona
from app.personalization.profiles import detect_preferences
from tests import payloads as p
from tests.conftest import ROOT, sent_bodies, signed

ADMIN = {"authorization": "Bearer boot-admin"}


async def _settle():
    for _ in range(15):
        await asyncio.sleep(0)


async def _send(client, payload):
    body, headers = signed(payload)
    await client.post("/webhooks/meta", content=body, headers=headers)
    await _settle()


@pytest.fixture
def settings(settings):
    settings.admin_bootstrap_key = __import__("pydantic").SecretStr("boot-admin")
    return settings


# ── Personalisation units ────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("can you reply in Hindi please", {"language": "hi"}),
    ("tamil please", {"language": "ta"}),
    ("please be brief, no emojis", {"verbosity": "brief", "emoji": "none"}),
    ("be more formal", {"formality": "formal"}),
    ("explain in detail", {"verbosity": "detailed"}),
    ("where is my order", {}),
    ("yes please", {}),
])
def test_detect_preferences(text, expected):
    assert detect_preferences(text) == expected


def test_style_guard_enforces_persona():
    cfg = load_tenant_config(ROOT / "config/tenants/brewline.yaml")
    persona = cfg.persona.model_copy(update={"emoji": "none"})
    text, fixes = StyleGuard().apply("Great choice! ☕ As an AI language model I love it. Enjoy 😊", persona)
    assert "☕" not in text and "😊" not in text and "AI language model" not in text
    assert set(fixes) == {"emoji", "avoid_phrase"}


def test_persona_layers_and_variants():
    cfg = load_tenant_config(ROOT / "config/tenants/brewline.yaml")
    ig, _ = resolve_persona(cfg, "instagram", "instagram:u1", None)
    wa, _ = resolve_persona(cfg, "whatsapp", "whatsapp:u1", None)
    assert ig.emoji == "expressive" and wa.emoji == "light"  # channel override from YAML
    seen = {assign_variant(cfg, f"whatsapp:{i}") for i in range(200)}
    assert seen == {"concise", "warm"}  # both arms of the A/B test get traffic
    assert assign_variant(cfg, "whatsapp:42") == assign_variant(cfg, "whatsapp:42")  # sticky


async def test_customer_language_preference_persists(client, fake_llm):
    await _send(client, p.whatsapp_text("reply in Hindi please", wamid="wamid.l1"))
    await _send(client, p.whatsapp_text("what's your return policy?", wamid="wamid.l2"))
    assert "Always reply in Hindi" in fake_llm.calls[-1]["system"]


async def test_no_emoji_request_is_enforced_even_if_model_ignores_it(client, graph_mock, fake_llm):
    fake_llm.script = [LLMResponse(text="Sure thing! 😊☕ Our V60 recipe uses 15 g of coffee.")]
    await _send(client, p.messenger_text("no emojis please. what's the V60 recipe?", mid="m_e1"))
    text = [b["message"]["text"] for _, b in sent_bodies(graph_mock) if "message" in b][-1]
    assert "😊" not in text and "☕" not in text


# ── RBAC + audit ─────────────────────────────────────────────────────────────

async def test_rbac_audit_and_scoping(client, graph_mock):
    await _send(client, p.messenger_text("talk to a human. my email is asha@example.com", mid="m_r1"))

    mk = lambda role, tenant=None: client.post("/admin/api-keys", headers=ADMIN,
                                               json={"name": f"{role}-1", "role": role, "tenant_id": tenant})
    agent_key = (await mk("agent", "brewline")).json()["key"]
    analyst_key = (await mk("analyst")).json()["key"]
    other_tenant_key = (await mk("agent", "some-other-store")).json()["key"]
    A = {"authorization": f"Bearer {agent_key}"}
    N = {"authorization": f"Bearer {analyst_key}"}

    queue = (await client.get("/agent/conversations", headers=A)).json()
    cid = queue[0]["id"]
    revealed = (await client.get(f"/agent/conversations/{cid}", headers=A)).json()
    assert revealed["pii_revealed"] and "asha@example.com" in revealed["messages"][0]["text"]

    hidden = (await client.get(f"/agent/conversations/{cid}", headers=N)).json()
    assert not hidden["pii_revealed"] and "<EMAIL_1>" in hidden["messages"][0]["text"]
    assert (await client.post(f"/agent/conversations/{cid}/messages", headers=N, json={"text": "x"})).status_code == 403
    assert (await client.get(f"/agent/conversations/{cid}",
                             headers={"authorization": f"Bearer {other_tenant_key}"})).status_code == 404

    audit = (await client.get("/admin/audit", headers=ADMIN, params={"action": "pii.reveal"})).json()
    assert len(audit) == 1 and audit[0]["actor"] == "agent-1"

    keys = (await client.get("/admin/api-keys", headers=ADMIN)).json()
    await client.delete(f"/admin/api-keys/{next(k['id'] for k in keys if k['name'] == 'agent-1')}", headers=ADMIN)
    assert (await client.get("/agent/conversations", headers=A)).status_code == 401
    assert (await client.get("/admin/api-keys", headers=N)).status_code == 403


async def test_analytics_containment(client):
    await _send(client, p.messenger_text("what's the V60 recipe?", mid="m_a1"))
    await _send(client, p.whatsapp_text("I want to talk to a person", wamid="wamid.a2"))
    stats = (await client.get("/admin/analytics", headers=ADMIN)).json()
    assert stats["conversations"] == 2 and stats["escalations_by_reason"] == {"human_request": 1}
    assert stats["containment_rate"] == 0.5


async def test_vip_profile_raises_handoff_priority(app, client):
    key = f"whatsapp:919800000001"
    r = await client.put(f"/admin/tenants/brewline/profiles/{key}", headers=ADMIN, json={"vip": True})
    assert r.json()["vip"] is True
    await _send(client, p.whatsapp_text("can I speak to someone", wamid="wamid.v1"))
    from sqlalchemy import select
    from app.storage.models import Handoff
    async with app.state.db.session() as s:
        h = (await s.execute(select(Handoff))).scalar_one()
    assert h.priority == "high"


# ── Multi-tenancy ────────────────────────────────────────────────────────────

@pytest.fixture
def multi_tenant_settings(settings, tmp_path, monkeypatch):
    tdir, kdir = tmp_path / "tenants", tmp_path / "knowledge"
    tdir.mkdir()
    shutil.copytree(ROOT / "knowledge/brewline", kdir / "brewline")
    brew = yaml.safe_load((ROOT / "config/tenants/brewline.yaml").read_text())
    brew["channels"] = {"page_id": p.PAGE_ID, "whatsapp_phone_number_id": p.WA_PHONE_ID}
    (tdir / "brewline.yaml").write_text(yaml.safe_dump(brew))
    leaf = {
        "tenant_id": "leafline",
        "brand": {"name": "Leafline Teas", "website": "https://leafline.example", "support_email": "hi@leafline.example",
                  "support_hours": "Mon-Fri 10-6"},
        "persona": {"agent_name": "Tara", "formality": "formal", "emoji": "none"},
        "scope": {"allowed_topics": ["Leafline teas and orders"]},
        "channels": {"page_id": "999888777"},
        "secrets": {"page_access_token": "env:LEAF_PAGE_TOKEN"},
    }
    (tdir / "leafline.yaml").write_text(yaml.safe_dump(leaf))
    monkeypatch.setenv("LEAF_PAGE_TOKEN", "leaf-token")
    settings.tenants_dir, settings.knowledge_root = str(tdir), str(kdir)
    return settings


async def test_messages_route_to_the_right_tenant(multi_tenant_settings, fake_llm, graph_mock):
    app = create_app(multi_tenant_settings, llm=fake_llm)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            leaf_msg = p.messenger_text("what is your return policy?", mid="m_t1")
            leaf_msg["entry"][0]["id"] = "999888777"
            await _send(c, leaf_msg)
            call = [x for x in graph_mock.calls if x.request.url.path.endswith("/999888777/messages")][-1]
            assert call.request.headers["authorization"] == "Bearer leaf-token"
            system = fake_llm.calls[-1]["system"]
            assert "You are Tara" in system and "Brewline" not in system
            assert "no articles matched" in system  # Leafline has no KB; Brewline's is invisible to it

            await _send(c, p.messenger_text("what is your return policy?", mid="m_t2"))
            assert "You are Bree" in fake_llm.calls[-1]["system"] and "<sources>" in fake_llm.calls[-1]["system"]

            unknown = p.messenger_text("hello", mid="m_t3")
            unknown["entry"][0]["id"] = "123123123"
            body, headers = signed(unknown)
            assert (await c.post("/webhooks/meta", content=body, headers=headers)).json() == {"received": 0}
