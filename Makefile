PYTHON ?= python3

.PHONY: test test-quick lint install bench clean

test:
	$(PYTHON) -m pytest tests/ -v

test-quick:
	$(PYTHON) -m pytest tests/ -x --tb=short -q

lint:
	$(PYTHON) -m ruff check scripts/ tests/ --select=E,F,W,B --ignore=E501

install:
	./install.sh

bench:
	@echo "Cold run:" && time $(PYTHON) scripts/recall.py "x" --cache-clear --cache-stats >/dev/null 2>&1
	@echo "Warm run:" && time $(PYTHON) scripts/recall.py "x" --cache-stats >/dev/null 2>&1

clean:
	rm -rf .pytest_cache .ruff_cache __pycache__ scripts/__pycache__ tests/__pycache__
