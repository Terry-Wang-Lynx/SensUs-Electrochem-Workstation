PYTHON ?= python3
VENV := .venv
VENV_PYTHON := $(VENV)/bin/python

.PHONY: install run app test firmware-test package clean-package

install:
	$(PYTHON) -m venv $(VENV)
	$(VENV_PYTHON) -m pip install -e ".[dev,analysis]"

run:
	SENSUS_PROJECT_DIR="$(CURDIR)" $(VENV_PYTHON) -m pa_host.gui_server --open-browser

app:
	./macos/build_app.sh

test:
	$(VENV_PYTHON) software/host/tests/test_record.py
	$(VENV_PYTHON) software/host/tests/test_analyze.py
	$(VENV_PYTHON) software/host/tests/test_it.py
	$(VENV_PYTHON) software/host/tests/test_gui_workflow.py
	$(VENV_PYTHON) software/host/tests/test_transient_phase.py
	$(VENV_PYTHON) -m compileall -q software/host/pa_host
	node --check software/host/pa_host/gui/app.js
	node --check software/host/pa_host/gui/compact.js

firmware-test:
	$(MAKE) -C software/firmware/tests clean all

package: clean-package
	$(VENV_PYTHON) -m build

clean-package:
	rm -rf build dist software/host/*.egg-info
