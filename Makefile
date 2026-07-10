.PHONY: setup build-c models wavs test run-server run-client clean

setup: build-c
	uv sync --group dev --extra client --extra ten || uv sync --group dev --extra client
	./scripts/fetch_models.sh

build-c:
	$(MAKE) -C third_party/unimrcp_vad
	$(MAKE) -C third_party/libfvad
	$(MAKE) -C third_party/arf_vad

models:
	./scripts/fetch_models.sh

wavs:
	uv run python scripts/make_test_wavs.py

test:
	uv run pytest

run-server:
	uv run vad-server

run-client:
	uv run vad-client

clean:
	$(MAKE) -C third_party/unimrcp_vad clean
	$(MAKE) -C third_party/libfvad clean
	$(MAKE) -C third_party/arf_vad clean
