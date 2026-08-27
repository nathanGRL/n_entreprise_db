PYTHON ?= python
PIP ?= $(PYTHON) -m pip

DATA_DIR ?= data
OUTPUT_DIR ?= output
BCE_INDEX ?= $(OUTPUT_DIR)/bce_reference.sqlite
IRAISER_INPUT ?= $(DATA_DIR)/iraiser_missing_enterprise_number.xlsx
IRAISER_KNOWN_INPUT ?= $(DATA_DIR)/iraiser_known_enterprise_numbers.xlsx
MATCH_OUTPUT ?= $(OUTPUT_DIR)/iraiser_enterprise_matches.xlsx
EVALUATION_OUTPUT ?= $(OUTPUT_DIR)/matching_evaluation.json
MATCH_CONFIG ?= match_config.example.json
COLUMN_MAP ?= column_map_iraiser.example.json
BRANCH ?= main

.PHONY: help install dev test testq lint check check-data run dryrun clean \
        index index-full match evaluate inspect status commit push sync

help:
	@echo "Project targets:"
	@echo "  install      Install runtime/test dependencies"
	@echo "  dev          Alias of install"
	@echo "  test         Run tests verbosely"
	@echo "  testq        Run tests quietly"
	@echo "  lint         Compile Python sources to catch syntax errors"
	@echo "  check        Run lint, tests and input-file checks"
	@echo "  check-data   Verify the three required BCE CSV files"
	@echo "  run          Generate the flattened BCE Excel with table_gen.py"
	@echo "  dryrun       Show the table-generation command without executing it"
	@echo "  index        Build the matcher index from registered offices"
	@echo "  index-full   Build the matcher index including establishments"
	@echo "  match        Match an iRaiser export"
	@echo "  evaluate     Evaluate with rows whose enterprise number is known"
	@echo "  inspect      Display matcher-index metadata"
	@echo "  clean        Remove generated matching artifacts"
	@echo "  status       Show Git status"
	@echo "  commit       Commit with: make commit m=\"message\""
	@echo "  push         Push the current branch"
	@echo "  sync         Pull/rebase, commit and push with: make sync m=\"message\""

install:
	$(PIP) install -r requirements.txt

dev: install

test:
	$(PYTHON) -m pytest -vv

testq:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m compileall -q enterprise_match.py table_gen.py tests

check-data:
	$(PYTHON) -c "from pathlib import Path; p=Path('$(DATA_DIR)'); required=('enterprise.csv','denomination.csv','address.csv'); missing=[x for x in required if not (p/x).is_file()]; assert not missing, 'Missing BCE files: '+', '.join(missing); print('BCE input files found in', p)"

check: lint testq check-data

run: check-data
	$(PYTHON) table_gen.py

dryrun: check-data
	@echo "Would run: $(PYTHON) table_gen.py"
	@echo "Expected output: $(OUTPUT_DIR)/entreprises_belgique_numero_nom_adresse_split.xlsx"

index: check-data
	$(PYTHON) enterprise_match.py build-index \
		--data-dir "$(DATA_DIR)" \
		--index "$(BCE_INDEX)" \
		--overwrite

index-full: check-data
	$(PYTHON) enterprise_match.py build-index \
		--data-dir "$(DATA_DIR)" \
		--index "$(BCE_INDEX)" \
		--include-establishments \
		--overwrite

match:
	$(PYTHON) enterprise_match.py match \
		--input "$(IRAISER_INPUT)" \
		--index "$(BCE_INDEX)" \
		--column-map "$(COLUMN_MAP)" \
		--config "$(MATCH_CONFIG)" \
		--output "$(MATCH_OUTPUT)" \
		--output-format both

evaluate:
	$(PYTHON) enterprise_match.py evaluate \
		--input "$(IRAISER_KNOWN_INPUT)" \
		--index "$(BCE_INDEX)" \
		--column-map "$(COLUMN_MAP)" \
		--config "$(MATCH_CONFIG)" \
		--output "$(EVALUATION_OUTPUT)" \
		--target-precision 0.995 \
		--target-probable-precision 0.95

inspect:
	$(PYTHON) enterprise_match.py inspect-index --index "$(BCE_INDEX)"

clean:
	rm -f "$(BCE_INDEX)" "$(BCE_INDEX)-shm" "$(BCE_INDEX)-wal"
	rm -f "$(MATCH_OUTPUT)" "$(EVALUATION_OUTPUT)"
	rm -rf "$(OUTPUT_DIR)/iraiser_enterprise_matches_csv"

status:
	git status --short --branch

commit:
	$(if $(strip $(m)),,$(error Usage: make commit m="message"))
	git add .
	git diff --cached --quiet || git commit -m "$(m)"

push:
	git push -u origin "$(BRANCH)"

sync:
	$(if $(strip $(m)),,$(error Usage: make sync m="message"))
	git pull --rebase origin "$(BRANCH)"
	git add .
	git diff --cached --quiet || git commit -m "$(m)"
	git push -u origin "$(BRANCH)"
