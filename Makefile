.PHONY: help install dev test testq lint format check check-data run dryrun clean status commit push sync

PYTHON ?= python
SCRIPT ?= $(firstword $(filter-out setup.py,$(wildcard *.py)))

DATA_DIR ?= data
OUTPUT_DIR ?= output

ENTERPRISE_CSV ?= $(DATA_DIR)/enterprise.csv
DENOMINATION_CSV ?= $(DATA_DIR)/denomination.csv
ADDRESS_CSV ?= $(DATA_DIR)/address.csv

OUTPUT_XLSX ?= $(OUTPUT_DIR)/entreprises_belgique_numero_nom_adresse_split.xlsx

m ?= update

help:
	@echo Available targets:
	@echo   make install              - install runtime dependencies
	@echo   make dev                  - install runtime dependencies + pytest
	@echo   make test                 - run full test suite
	@echo   make testq                - run tests quietly
	@echo   make lint                 - compile the Python script
	@echo   make format               - placeholder for formatter later
	@echo   make check                - lint + test
	@echo   make check-data           - verify the 3 BCE/KBO input CSV files
	@echo   make run                  - build the Belgian enterprise Excel database
	@echo   make dryrun               - show resolved script/input/output paths only
	@echo   make clean                - delete Python and pytest caches
	@echo   make status               - show git status
	@echo   make commit m="msg"       - add + commit
	@echo   make push                 - push main to origin
	@echo   make sync m="msg"         - add + commit + push
	@echo.
	@echo Variables:
	@echo   SCRIPT=script.py
	@echo   DATA_DIR=data
	@echo   OUTPUT_DIR=output

install:
	$(PYTHON) -m pip install pandas xlsxwriter

dev:
	$(PYTHON) -m pip install pandas xlsxwriter pytest

test:
	$(PYTHON) -m pytest -v

testq:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m py_compile "$(SCRIPT)"

format:
	@echo No formatter configured yet

check: lint test

check-data:
	PowerShell -NoProfile -Command "$$files = @('$(ENTERPRISE_CSV)', '$(DENOMINATION_CSV)', '$(ADDRESS_CSV)'); $$missing = $$files | Where-Object { -not (Test-Path $$_) }; if ($$missing) { Write-Error ('Missing input file(s): ' + ($$missing -join ', ')); exit 1 }; if (-not (Test-Path '$(OUTPUT_DIR)')) { New-Item -ItemType Directory -Path '$(OUTPUT_DIR)' | Out-Null }; Write-Host 'BCE/KBO input files OK'"

run: check-data
	@echo Running: $(SCRIPT)
	$(PYTHON) "$(SCRIPT)"

dryrun: check-data
	@echo Script:       $(SCRIPT)
	@echo Enterprise:   $(ENTERPRISE_CSV)
	@echo Denomination: $(DENOMINATION_CSV)
	@echo Address:      $(ADDRESS_CSV)
	@echo Output:       $(OUTPUT_XLSX)
	@echo.
	@echo No data processing was executed.

clean:
	PowerShell -NoProfile -Command "Get-ChildItem -Recurse -Directory -Filter __pycache__ -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force"
	PowerShell -NoProfile -Command "Get-ChildItem -Recurse -File -Include *.pyc -ErrorAction SilentlyContinue | Remove-Item -Force"
	PowerShell -NoProfile -Command "if (Test-Path .pytest_cache) { Remove-Item .pytest_cache -Recurse -Force }"

status:
	git status

commit:
	git add .
	git commit -m "$(m)"

push:
	git push origin main

sync:
	git add .
	git commit -m "$(m)"
	git push origin main
