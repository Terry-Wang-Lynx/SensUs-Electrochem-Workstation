PYTHON ?= python3
# Detect Windows vs Unix for venv paths
ifeq ($(OS),Windows_NT)
	VENV := .venv
	VENV_PYTHON := $(VENV)/Scripts/python
	VENV_PIP := $(VENV)/Scripts/pip
	RM := del /q
	RMDIR := rmdir /s /q
	MKDIR := mkdir
	NULL := nul
else
	VENV := .venv
	VENV_PYTHON := $(VENV)/bin/python
	VENV_PIP := $(VENV)/bin/pip
	RM := rm -rf
	RMDIR := rm -rf
	MKDIR := mkdir -p
	NULL := /dev/null
endif

.PHONY: install run app portable-macos dmg test firmware-test package clean-package clean help

help:
	@echo SensUs Electrochem Workstation build targets:
	@echo   make install          Create venv and install dependencies
	@echo   make run              Start the GUI server (browser interface)
	@echo   make app              Build macOS native app (macOS only)
	@echo   make portable-macos   Build self-contained macOS arm64 app
	@echo   make dmg              Build self-contained macOS arm64 DMG
	@echo   make test             Run host tests
	@echo   make firmware-test    Run firmware logic tests
	@echo   make package          Build wheel and source distribution
	@echo   make clean            Remove build artifacts

install:
	$(PYTHON) -m venv $(VENV)
	$(VENV_PIP) install -e ".[dev,analysis]"

run:
ifeq ($(OS),Windows_NT)
	set SENSUS_PROJECT_DIR=$(CURDIR) && $(VENV_PYTHON) -m pa_host.gui_server --open-browser
else
	SENSUS_PROJECT_DIR="$(CURDIR)" $(VENV_PYTHON) -m pa_host.gui_server --open-browser
endif

app:
ifeq ($(OS),Windows_NT)
	@echo "macOS native app not available on Windows."
	@echo "Use 'make run' for the browser interface, or 'windows\build_win.bat' to build a Windows EXE."
else
	./macos/build_app.sh
endif

portable-macos:
ifeq ($(OS),Windows_NT)
	@echo "Use windows\\build_win.bat on Windows."
else
	./packaging/build_macos_portable.sh
endif

dmg:
ifeq ($(OS),Windows_NT)
	@echo "DMG builds are only available on macOS."
else
	./packaging/create_dmg.sh
endif

test:
	$(VENV_PYTHON) -m pytest -q software/host/tests
	$(VENV_PYTHON) -m compileall -q software/host/pa_host
	node --check software/host/pa_host/gui/app.js
	node --check software/host/pa_host/gui/compact.js

firmware-test:
	$(MAKE) -C software/firmware/tests clean all

package: clean-package
ifeq ($(OS),Windows_NT)
	$(VENV_PYTHON) -m build
else
	$(VENV_PYTHON) -m build
endif

clean-package:
	$(RM) build dist software/host/*.egg-info 2>$(NULL) || true

clean: clean-package
	$(RM) .venv-installed 2>$(NULL) || true
