.PHONY: api web seed run-suite

MODELS ?= openai/gpt-4o

api:
	uv run uvicorn evalbench.api.app:app --reload --port 8000

web:
	npm --prefix web run dev -- --port 3000

run-suite:
	@test -n "$(SUITE)" || (echo "SUITE is required" >&2; exit 1)
	@test -n "$(DOMAIN)" || (echo "DOMAIN is required" >&2; exit 1)
	@test -n "$(MODELS)" || (echo "MODELS is required" >&2; exit 1)
	uv run python -m evalbench.runner --suite "$(SUITE)" --domain "$(DOMAIN)" --models "$(MODELS)"

seed:
	$(MAKE) run-suite SUITE=structured DOMAIN=software MODELS="$(MODELS)"
