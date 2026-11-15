PYTHON ?= .venv/bin/python
VENV_PYTHON ?= python3.12
NETHACK_CONTAINER_ENGINE ?= podman
export NETHACK_CONTAINER_ENGINE
.PHONY: setup test verify-build
setup:
	python3 scripts/setup.py

test:
	$(PYTHON) -m pytest -q

verify-build:
	$(PYTHON) scripts/verify_build.py

.PHONY: image
image:
	mkdir -p build
	$(NETHACK_CONTAINER_ENGINE) build --platform linux/amd64 -t localhost/nethack-run:local .
	python3 scripts/record_image.py localhost/nethack-run:local

.PHONY: run
run:
	$(NETHACK_CONTAINER_ENGINE) run --rm -p 127.0.0.1:8000:8000 --mount type=volume,src=nethack-runs,dst=/runs -e NETHACK_IMAGE_DIGEST="$$(cat build/image-id)" "$$(cat build/image-id)"

.PHONY: demo cross-platform-test
demo:
	$(PYTHON) scripts/demo.py

cross-platform-test:
	test -n "$(PACKAGE)"
	$(NETHACK_CONTAINER_ENGINE) build --platform linux/arm64 -t localhost/nethack-run:arm64 .
	$(NETHACK_CONTAINER_ENGINE) run --rm --platform linux/arm64 --network none --mount type=bind,src=$(abspath $(PACKAGE)),dst=/package,readonly localhost/nethack-run:arm64 replay /package --diagnostic-cross-build

.PHONY: venv
venv:
	$(VENV_PYTHON) -m venv .venv
	.venv/bin/pip install --require-hashes -r requirements.lock
