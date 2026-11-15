PYTHON ?= .venv/bin/python
VENV_PYTHON ?= python3.12
NETHACK_CONTAINER_ENGINE ?= podman
PORT ?= 8756
CONCURRENCY ?= 100
GITHUB_AUTH_CONFIG ?= $(wildcard config/github-auth.local.json)
comma := ,
CONTAINER_NAME ?= nethack-run
RUN_FLAGS = -p 127.0.0.1:$(PORT):8000 --mount type=volume,src=nethack-runs,dst=/runs $(if $(GITHUB_AUTH_CONFIG),--secret nethack-github-auth$(comma)target=/run/github-auth.json$(comma)uid=1000$(comma)gid=1000$(comma)mode=0400) -e NETHACK_IMAGE_DIGEST="$$(cat build/image-id)"
SERVER_ARGS = serve --host 0.0.0.0 --port 8000 --state /runs --concurrency $(CONCURRENCY) $(if $(GITHUB_AUTH_CONFIG),--github-auth /run/github-auth.json)
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

.PHONY: run up down logs auth-secret
auth-secret:
	$(if $(GITHUB_AUTH_CONFIG),$(NETHACK_CONTAINER_ENGINE) secret create --replace nethack-github-auth "$(abspath $(GITHUB_AUTH_CONFIG))",@true)

run: auth-secret
	$(NETHACK_CONTAINER_ENGINE) run --rm $(RUN_FLAGS) "$$(cat build/image-id)" $(SERVER_ARGS)

up: auth-secret
	$(NETHACK_CONTAINER_ENGINE) run --detach --replace --name $(CONTAINER_NAME) $(RUN_FLAGS) "$$(cat build/image-id)" $(SERVER_ARGS)
	@printf 'NetHack dashboard: http://127.0.0.1:$(PORT)\n'

down:
	$(NETHACK_CONTAINER_ENGINE) stop $(CONTAINER_NAME)

logs:
	$(NETHACK_CONTAINER_ENGINE) logs --tail 100 $(CONTAINER_NAME)

.PHONY: demo cross-platform-test cross-platform-user-test
demo:
	$(PYTHON) scripts/demo.py --url http://127.0.0.1:$(PORT)

cross-platform-test:
	test -n "$(PACKAGE)"
	$(NETHACK_CONTAINER_ENGINE) build --platform linux/arm64 -t localhost/nethack-run:arm64 .
	$(NETHACK_CONTAINER_ENGINE) run --rm --platform linux/arm64 --network none --mount type=bind,src=$(abspath $(PACKAGE)),dst=/package,readonly localhost/nethack-run:arm64 replay /package --diagnostic-cross-build

cross-platform-user-test:
	test -n "$(PACKAGE)"
	python3 scripts/arm_user_test.py "$(PACKAGE)"

.PHONY: venv
venv:
	$(VENV_PYTHON) -m venv .venv
	.venv/bin/pip install --require-hashes -r requirements.lock

# Keep release tooling separate from the hash-pinned runtime environment.
PART ?= patch
RELEASE_PYTHON ?= python3.12
RELEASE_TOOL = build/release-tools/bin/bump-my-version

$(RELEASE_TOOL): requirements-release.txt
	$(RELEASE_PYTHON) -m venv build/release-tools
	build/release-tools/bin/python -m pip install -r requirements-release.txt

.PHONY: bump bump-dry
bump: $(RELEASE_TOOL)
	$(RELEASE_TOOL) bump $(PART)

bump-dry: $(RELEASE_TOOL)
	$(RELEASE_TOOL) bump --dry-run --verbose $(PART)
