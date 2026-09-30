.PHONY: dev test lint fmt deploy deploy-dry deploy-web deploy-full clean

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

# auto: web-only if only web/** changed since the box's .deployed-sha, else full.
# Needs the orb key in ssh-agent first, see docs/DEPLOY.md. Extra flags: DEPLOY_ARGS=...
deploy:
	@if [ -z "$$BOX_HOST" ]; then echo "set BOX_HOST=root@<ip>"; exit 1; fi
	./deploy/deploy.sh $(DEPLOY_ARGS)

deploy-dry:
	@if [ -z "$$BOX_HOST" ]; then echo "set BOX_HOST=root@<ip>"; exit 1; fi
	./deploy/deploy.sh --dry-run $(DEPLOY_ARGS)

deploy-web:
	@if [ -z "$$BOX_HOST" ]; then echo "set BOX_HOST=root@<ip>"; exit 1; fi
	./deploy/deploy.sh --web-only $(DEPLOY_ARGS)

deploy-full:
	@if [ -z "$$BOX_HOST" ]; then echo "set BOX_HOST=root@<ip>"; exit 1; fi
	./deploy/deploy.sh --full $(DEPLOY_ARGS)

clean:
	rm -rf .venv .pytest_cache .ruff_cache *.egg-info build dist
	find . -type d -name __pycache__ -exec rm -rf {} +
