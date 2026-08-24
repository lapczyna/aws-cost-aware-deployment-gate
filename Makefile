# Thin wrapper. scripts/dev.py is the canonical definition of every task, so that
# the same commands work on machines without make (see docs/adr and CONTRIBUTING).
PY ?= python
DEV := $(PY) scripts/dev.py

.DEFAULT_GOAL := help
.PHONY: help install format lint typecheck imports test test-contract test-integration \
        test-e2e test-cdk test-all coverage security build demo analyze synth clean all

help:
	@$(DEV) --list

install:          ; @$(DEV) install
format:           ; @$(DEV) format
format-check:     ; @$(DEV) format-check
lint:             ; @$(DEV) lint
typecheck:        ; @$(DEV) typecheck
imports:          ; @$(DEV) imports
test:             ; @$(DEV) test
test-contract:    ; @$(DEV) test-contract
test-integration: ; @$(DEV) test-integration
test-e2e:         ; @$(DEV) test-e2e
test-cdk:         ; @$(DEV) test-cdk
test-all:         ; @$(DEV) test-all
coverage:         ; @$(DEV) coverage
security:         ; @$(DEV) security
build:            ; @$(DEV) build
demo:             ; @$(DEV) demo
analyze:          ; @$(DEV) analyze
synth:            ; @$(DEV) synth
clean:            ; @$(DEV) clean
all:              ; @$(DEV) all
