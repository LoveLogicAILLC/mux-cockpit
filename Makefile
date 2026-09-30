# MUX Host-Driven Cockpit — Mac Mini M4
VERSION ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo 0.2.0)
LDFLAGS := -s -w -X main.version=$(VERSION)
PREFIX  ?= $(HOME)/.local
SOCK    ?= /tmp/mux_host.sock

.PHONY: all build build-m4 test test-py test-go run host rpc demo status fmt clean install npm-build npm-publish

all: build

build:
	@mkdir -p bin
	go build -trimpath -ldflags="$(LDFLAGS)" -o bin/cockpit .

build-m4:
	@mkdir -p bin
	GOOS=darwin GOARCH=arm64 go build -trimpath -ldflags="$(LDFLAGS)" -o bin/cockpit-darwin-arm64 .

test: test-py test-go
test-py:
	python3 -m unittest discover -s tests -v
test-go:
	go vet ./... && go test ./...

run: build            ## full stack (ollama + host + TUI)
	bash run.sh

host:                 ## host only, foreground
	python3 host_orchestrator.py serve --sock $(SOCK)

rpc:                  ## Pi-compatible RPC agent on stdio
	python3 -m harness --mode rpc

demo: build           ## offline demo with the mock provider
	MUX_PROVIDER=mock bash run.sh

status:
	./bin/cockpit --status --sock $(SOCK) | python3 -m json.tool

fmt:
	gofmt -w *.go

clean:
	rm -rf bin/ memory/ dist/ $(SOCK) __pycache__ */__pycache__

npm-build:
	@bash scripts/build-npm.sh

npm-publish:
	@bash scripts/publish-npm.sh

install: build
	install -d $(PREFIX)/bin && install bin/cockpit $(PREFIX)/bin/mux-cockpit
	@echo "installed $(PREFIX)/bin/mux-cockpit"
