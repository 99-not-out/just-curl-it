JCI_HOME ?= $(HOME)/.just_curl_it

# Pick a non-SIP python for the demos venv (brewed if available; otherwise
# whatever python3 the user has — fine on Linux, may not load the shim on
# macOS if it's the system /usr/bin/python3).
PYTHON ?= $(shell brew --prefix python 2>/dev/null)/bin/python3
ifeq ($(wildcard $(PYTHON)),)
PYTHON := python3
endif

.PHONY: all build install demos-install test clean

all: build

test: build
	tests/run.sh

build:
	cd lib && ./build.sh

install: build
	mkdir -p $(JCI_HOME)/lib $(JCI_HOME)/bin $(JCI_HOME)/handlers $(JCI_HOME)/envs
	@if [ -f lib/intercept.so ];    then cp lib/intercept.so    $(JCI_HOME)/lib/; fi
	@if [ -f lib/intercept.dylib ]; then cp lib/intercept.dylib $(JCI_HOME)/lib/; fi
	cp bin/jci $(JCI_HOME)/bin/
	chmod +x  $(JCI_HOME)/bin/jci
	cp handlers/* $(JCI_HOME)/handlers/
	chmod +x  $(JCI_HOME)/handlers/*
	cp -R envs/. $(JCI_HOME)/envs/
	@echo
	@echo "installed to $(JCI_HOME)"
	@echo "add to PATH:  export PATH=\"$(JCI_HOME)/bin:\$$PATH\""
	@echo "then try:     jci ls && jci use dev"

demos-install: install
	@echo "Setting up demos venv with $(PYTHON)..."
	$(PYTHON) -m venv $(JCI_HOME)/handlers/.venv
	$(JCI_HOME)/handlers/.venv/bin/pip install --quiet --upgrade pip
	$(JCI_HOME)/handlers/.venv/bin/pip install --quiet -r demos/requirements.txt
	cp demos/handlers/* $(JCI_HOME)/handlers/
	chmod +x $(JCI_HOME)/handlers/*
	mkdir -p $(JCI_HOME)/envs/demos
	cp demos/env/routes.conf    $(JCI_HOME)/envs/demos/
	cp demos/env/backends.conf  $(JCI_HOME)/envs/demos/
	@echo
	@echo "demos installed."
	@echo "  start backends:   cd demos && docker compose up -d"
	@echo "  activate:         jci use demos"
	@echo "  tour:             demos/scripts/tour.sh   (after activation)"

clean:
	rm -f lib/intercept.so lib/intercept.dylib
