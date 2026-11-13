.PHONY: install test run chat up

install:
	pip install -e ".[dev]"

test:
	pytest -q

run:
	uvicorn --factory app.main:create_app --reload --port 8000

chat:
	python -m scripts.chat_cli --channel whatsapp

up:
	docker compose up --build
