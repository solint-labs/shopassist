.PHONY: install test evals evals-llm deps-up deps-down run worker chat ingest up up-tls down logs keys

install:            ## install the app + dev tools into the active venv
	pip install -e ".[dev]"

test:               ## unit + integration tests (offline, ~15 s)
	pytest -q

evals:              ## deterministic golden cases (no API keys needed)
	RAG_ENCODERS=hashing LLM_PROVIDER=fake python -m evals.run --fake --min-pass 1.0

evals-llm:          ## all golden cases against the LLM in .env, with the LLM judge
	python -m evals.run --report evals/report.json --junit evals/junit.xml

deps-up:            ## Postgres, Redis, Qdrant in Docker for local development
	docker compose up -d postgres redis qdrant

deps-down:
	docker compose stop postgres redis qdrant

run:                ## API with auto-reload on :8000
	uvicorn --factory app.main:create_app --reload --port 8000

worker:             ## queue worker (QUEUE_MODE=redis)
	python -m app.worker

chat:               ## talk to the agent in your terminal
	python -m scripts.chat_cli --channel whatsapp

ingest:             ## index knowledge/ (+ Shopify with ARGS=--shopify)
	python -m scripts.ingest $(ARGS)

up:                 ## full stack (no TLS)
	docker compose up -d --build

up-tls:             ## full stack behind Caddy with automatic HTTPS for $$SHOPASSIST_DOMAIN
	docker compose --profile tls up -d --build

down:
	docker compose --profile tls down

logs:
	docker compose logs -f api worker

keys:               ## generate PII encryption + blind-index keys for .env
	@python -c "from cryptography.fernet import Fernet; import secrets; print('PII_ENCRYPTION_KEYS=' + Fernet.generate_key().decode()); print('PII_BLIND_INDEX_KEY=' + secrets.token_urlsafe(48)); print('ADMIN_BOOTSTRAP_KEY=' + secrets.token_urlsafe(32)); print('META_VERIFY_TOKEN=' + secrets.token_urlsafe(24)); print('AGENT_API_KEY=' + secrets.token_urlsafe(32))"
