JCI_HOME ?= $(HOME)/.just_curl_it

# Handlers with dependencies get a venv each from `jci handler install`,
# which picks the interpreter ($JCI_PYTHON, else brew's, else python3).
# Nothing in the core needs a venv.

.PHONY: all build install demos-install test test-handlers hooks clean

all: build

test: build
	tests/run.sh

# Docker-backed handler tests; needs `cd demos && docker compose up -d`
# (sections for services that aren't running skip) and network for pip.
test-handlers: build
	tests/handlers.sh

build:
	cd lib && ./build.sh

install: build
	mkdir -p $(JCI_HOME)/lib $(JCI_HOME)/bin $(JCI_HOME)/handlers $(JCI_HOME)/envs
	@if [ -f lib/intercept.so ];    then cp lib/intercept.so    $(JCI_HOME)/lib/; fi
	@if [ -f lib/intercept.dylib ]; then cp lib/intercept.dylib $(JCI_HOME)/lib/; fi
	cp lib/helpers.sh $(JCI_HOME)/lib/
	cp bin/jci $(JCI_HOME)/bin/
	chmod +x  $(JCI_HOME)/bin/jci
	# The handler library ships inert: source + READMEs, no venvs. Venvs are
	# created per handler by `jci handler install` and live outside lib/.
	cp handlers/run $(JCI_HOME)/handlers/run
	chmod +x $(JCI_HOME)/handlers/run
	rm -rf $(JCI_HOME)/handlers/lib
	cp -R handlers/lib $(JCI_HOME)/handlers/lib
	find $(JCI_HOME)/handlers/lib -name '__pycache__' -type d -prune -exec rm -rf {} +
	find $(JCI_HOME)/handlers/lib -name 'handler.sh' -exec chmod +x {} +
	@if [ -d envs ]; then cp -R envs/. $(JCI_HOME)/envs/; fi
	@echo
	@echo "installed to $(JCI_HOME)"
	@echo "add to PATH:  export PATH=\"$(JCI_HOME)/bin:\$$PATH\""
	@echo "then try:     jci handler ls      (and 'make demos-install' for a ready-made env)"

DEMO_HANDLERS = kafka redis postgres mysql mongo rabbit grpc mqtt ldap

demos-install: install
	@echo "Installing handlers for the demos env: $(DEMO_HANDLERS)"
	JCI_HOME=$(JCI_HOME) $(JCI_HOME)/bin/jci handler install $(DEMO_HANDLERS)
	mkdir -p $(JCI_HOME)/envs/demos
	cp demos/env/routes.conf    $(JCI_HOME)/envs/demos/
	cp demos/env/backends.conf  $(JCI_HOME)/envs/demos/
	cp demos/specs/*.json       $(JCI_HOME)/envs/demos/
	@echo
	@echo "demos installed."
	@echo "  start backends:   cd demos && docker compose up -d"
	@echo "  activate:         jci use demos"
	@echo "  tour:             demos/scripts/tour.sh   (after activation)"

# Maintainers: point git at the versioned hooks (pre-commit ADR reminder).
hooks:
	git config core.hooksPath .githooks
	@echo "git hooks -> .githooks (pre-commit reminds you to record decisions in docs/adr)"

clean:
	rm -f lib/intercept.so lib/intercept.dylib
