"""HTTP endpoints Meta calls.

GET  /webhooks/meta   one-time verification handshake when you register the webhook
POST /webhooks/meta   every message/comment event for WhatsApp, Messenger and Instagram

Meta expects a 200 within a few seconds or it retries (and eventually disables the
webhook). So the POST handler only verifies, parses and enqueues; the LLM work happens
after the response is sent: in a background task (QUEUE_MODE=inline) or on a worker
reading a Redis Stream (QUEUE_MODE=redis, Part 4).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response

from app.channels.parser import parse_handover_events, parse_webhook
from app.channels.signature import verify_meta_signature
from app.logging_config import log

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.get("/meta")
async def verify_subscription(
    request: Request,
    mode: str = Query(alias="hub.mode", default=""),
    token: str = Query(alias="hub.verify_token", default=""),
    challenge: str = Query(alias="hub.challenge", default=""),
) -> Response:
    expected = request.app.state.settings.meta_verify_token.get_secret_value()
    if mode == "subscribe" and expected and token == expected:
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="verification failed")


@router.post("/meta")
async def receive(request: Request, background: BackgroundTasks) -> dict:
    settings = request.app.state.settings
    body = await request.body()  # raw bytes: the signature is over these exact bytes
    # Instagram API with Instagram Login signs with the Instagram app secret.
    secrets = [settings.meta_app_secret.get_secret_value(),
               settings.instagram_app_secret.get_secret_value() if settings.instagram_app_secret else ""]
    if not verify_meta_signature(body, request.headers.get("x-hub-signature-256"), secrets):
        log.warning("webhook.bad_signature")
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json") from None

    # Handover protocol: thread control came back to our app -> the bot resumes.
    for ev in parse_handover_events(payload):
        if settings.meta_app_id and ev.new_owner_app_id == settings.meta_app_id:
            conv = await request.app.state.agent.store.find_by_key(f"{ev.channel}:{ev.business_id}:d:{ev.user_id}")
            if conv:
                handoff = request.app.state.registry.for_tenant(conv.tenant_id).agent.handoff
                background.add_task(handoff.release, conv.id)

    registry = request.app.state.registry
    messages = [m for m in parse_webhook(payload) if registry.resolve(m) is not None]  # drop unknown assets
    queue = request.app.state.queue
    for msg in messages:
        if settings.queue_mode == "redis" and queue is not None:
            await queue.enqueue(msg)  # durable: a worker picks it up (Part 4)
        else:
            background.add_task(request.app.state.pipeline.process, msg)  # runs after the 200 is sent
    log.info("webhook.received", object=payload.get("object"), messages=len(messages))
    return {"received": len(messages)}
