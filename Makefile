.PHONY: dev test lint fmt deploy clean

dev:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip wheel
	.venv/bin/pip install -e ".[dev]"

test:
	.venv/bin/pytest -q
	.venv/bin/ruff check .

lint:
	.venv/bin/ruff check .

fmt:
	.venv/bin/ruff format .

deploy:
	@if [ -z "$$BOX_HOST" ]; then echo "set BOX_HOST=root@<ip>"; exit 1; fi
	./deploy/deploy.sh

clean:
	rm -rf .venv .pytest_cache .ruff_cache *.egg-info build dist
	find . -type d -name __pycache__ -exec rm -rf {} +
