"""Queue worker entrypoint (Part 4).

    QUEUE_MODE=redis REDIS_URL=redis://... python -m app.worker

Builds the same components as the API (tenants, agents, gateway), then drains the
Redis Stream. Scale replicas on queue depth (e.g. KEDA's redis-streams scaler) and
stop them gracefully with SIGTERM: in-flight messages finish, unacked ones are
reclaimed by another worker.
"""

from __future__ import annotations

import asyncio
import os
import signal

from prometheus_client import start_http_server

from app.logging_config import log
from app.main import create_app
from app.reliability.queue import Worker
from app.settings import get_settings


async def main() -> None:
    settings = get_settings()
    if not settings.redis_url:
        raise SystemExit("REDIS_URL is required for the worker")
    # Workers do the agent turns, so most metrics live here: expose them for Prometheus.
    start_http_server(int(os.environ.get("WORKER_METRICS_PORT", "9100")))
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        await app.state.queue.ensure_group()
        worker = Worker(app.state.redis, app.state.pipeline, concurrency=settings.worker_concurrency,
                        max_deliveries=settings.worker_max_deliveries)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, worker.stop)
        await worker.run()
    log.info("worker.stopped")


if __name__ == "__main__":
    asyncio.run(main())
