.PHONY: setup build-c models noise samples wavs test run stop status run-server run-client clean

# the whole app in one command: the server starts the softphone client
# itself, so you only ever open http://127.0.0.1:8080
run: stop
	uv run vad-server

# stop whatever holds the app's ports (server web+SIP, client web).
# two passes (TERM then KILL) because `uv run` wrappers can outlive a plain
# TERM and keep the UDP SIP socket bound.
stop:
	-@lsof -t -i tcp:8080 -i tcp:8081 -i udp:5060 2>/dev/null | sort -u | xargs kill 2>/dev/null; sleep 1; true
	-@lsof -t -i tcp:8080 -i tcp:8081 -i udp:5060 2>/dev/null | sort -u | xargs kill -9 2>/dev/null; true
	@echo "stopped (ports 8080/8081/5060 freed)"

status:
	@lsof -i tcp:8080 -i tcp:8081 -i udp:5060 2>/dev/null | grep -v '^COMMAND' || echo "nothing running"

setup: build-c
	uv sync --group dev --extra client --extra ten || uv sync --group dev --extra client
	./scripts/fetch_models.sh
	./scripts/fetch_noise.sh

build-c:
	$(MAKE) -C third_party/unimrcp_vad
	$(MAKE) -C third_party/libfvad
	$(MAKE) -C third_party/arf_vad
	$(MAKE) -C third_party/arf_enhance

models:
	./scripts/fetch_models.sh

# real ambient noise (MS-SNSD) for the noisy fixtures; optional
noise:
	./scripts/fetch_noise.sh

# a library of noisy speech recordings under data/samples/ to try from the UI
samples:
	./scripts/fetch_noise.sh
	uv run python scripts/make_sample_library.py

wavs:
	uv run python scripts/make_test_wavs.py
	uv run python scripts/make_noisy_wavs.py

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
	$(MAKE) -C third_party/arf_enhance clean
