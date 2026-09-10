.PHONY: help install doctor target target2 test demo demo-handoff demo-crystallize discover replay clean

help:
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## install dependencies and the Chromium build Playwright needs
	pip install -r requirements.txt
	python3 -m playwright install chromium

target:  ## run the mock CoreServ application (tenant: meridian_cu, port 8799)
	python3 -m pcx.cli target

target2:  ## run a second tenant of the same product (northgate_cu, port 8798)
	python3 -m pcx.cli target --port 8798 --vocab customer --secret northgate

doctor:  ## check the environment and say what to run next
	python3 scripts/doctor.py

test:  ## unit tests (no browser, no model, no network)
	python3 -m pytest tests/ -q

demo:  ## the full thread: discovery -> artifact -> replay -> error handling
	python3 scripts/demo.py

demo-handoff:  ## escalation: a stuck replay handed to a human on the live session
	python3 scripts/demo_handoff.py

demo-crystallize:  ## promotion and demotion across the execution-type spectrum
	python3 scripts/demo_crystallize.py

clean:  ## remove evidence and recorded capabilities
	rm -rf evidence/* capabilities/*/ 2>/dev/null || true
