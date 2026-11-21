.PHONY: up down test test-pg test-report eval eval-rules eval-docker check-gemini dataset logs secrets hooks
up:            ## build and start everything (http://localhost:8080)
	docker compose up --build
down:
	docker compose down
test:          ## backend tests (SQLite, no services needed)
	cd backend && python -m pytest
test-pg:       ## backend tests against PostgreSQL + pgvector
	cd backend && TEST_DATABASE_URL=postgresql://docintel:docintel@localhost:5432/docintel python -m pytest
test-report:   ## run the tests and write docs/test-results.md (add POSTGRES_URL=... to also run on PostgreSQL)
	python scripts/make_test_report.py $(if $(POSTGRES_URL),--postgres $(POSTGRES_URL),)
check-gemini:  ## 10-second check that your Gemini key / model / embeddings work
	python scripts/check_gemini.py
eval-docker:   ## same as `eval`, but inside the worker container (no local Python / Tesseract needed)
	docker compose run --rm -v ./eval:/srv/eval -v ./docs:/srv/docs worker python /srv/eval/run_eval.py --extractor llm
dataset:       ## regenerate the labelled evaluation dataset
	python eval/generate_dataset.py
eval-rules:    ## offline baseline (no API key)
	python eval/run_eval.py --extractor rules
eval:          ## evaluation with Gemini (needs GEMINI_API_KEY in .env) -> docs/evaluation.md
	python eval/run_eval.py --extractor llm
logs:
	docker compose logs -f api worker
secrets:       ## scan the files git would commit for API keys / tokens
	python scripts/check_secrets.py
hooks:         ## check for secrets automatically before every commit and push
	sh scripts/install_hooks.sh
