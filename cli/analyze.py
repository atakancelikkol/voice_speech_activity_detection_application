"""Offline harness: run VAD engines over a WAV file and report segments.

    uv run python -m cli.analyze tests/fixtures/pattern1.wav --engines unimrcp_vad
    uv run python -m cli.analyze speech.wav --engines all --json
    uv run python -m cli.analyze x.wav --engines silero_vad --param silero_vad.threshold=0.6

Audio is resampled to 8 kHz and fed in 20 ms chunks, mimicking the RTP path.
"""

from __future__ import annotations

import argparse
import json
import sys

from server.audio.wav_io import load_wav
from server.vad import registry
from server.vad.runner import SOURCE_RATE, EngineRunner


def parse_params(pairs: list[str]) -> dict[str, dict[str, str]]:
    by_engine: dict[str, dict[str, str]] = {}
    for pair in pairs:
        try:
            key, value = pair.split("=", 1)
            engine, param = key.split(".", 1)
        except ValueError:
            raise SystemExit(f"bad --param {pair!r}, expected engine.param=value")
        by_engine.setdefault(engine, {})[param] = value
    return by_engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wav")
    parser.add_argument("--engines", default="all", help="comma-separated engine names, or 'all'")
    parser.add_argument("--param", action="append", default=[], help="engine.param=value (repeatable)")
    parser.add_argument("--chunk-ms", type=int, default=20)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    params = parse_params(args.param)
    infos = registry.discover()
    wanted = list(infos) if args.engines == "all" else args.engines.split(",")
    unknown = [name for name in wanted if name not in infos]
    if unknown:
        raise SystemExit(f"unknown engine(s): {', '.join(unknown)} (have: {', '.join(infos)})")

    pcm = load_wav(args.wav, SOURCE_RATE)
    duration_ms = len(pcm) * 1000.0 / SOURCE_RATE

    report: dict = {"wav": args.wav, "duration_ms": round(duration_ms, 1), "engines": {}}
    runners: dict[str, EngineRunner] = {}
    for name in wanted:
        info = infos[name]
        if not info.available:
            report["engines"][name] = {"available": False, "reason": info.reason}
            continue
        runners[name] = EngineRunner(registry.create(info, params.get(name)))

    chunk = SOURCE_RATE * args.chunk_ms // 1000
    for start in range(0, len(pcm), chunk):
        block = pcm[start : start + chunk]
        for runner in runners.values():
            runner.feed(block)

    for name, runner in runners.items():
        segments = runner.finalize()
        report["engines"][name] = {
            "available": True,
            "segments": [s.as_dict() for s in segments],
            "events": [{"kind": e.kind.value, "at_ms": round(e.at_ms, 1)} for e in runner.events],
        }

    if args.as_json:
        json.dump(report, sys.stdout, indent=2)
        print()
        return 0

    print(f"{args.wav}: {duration_ms:.0f} ms @ {SOURCE_RATE} Hz")
    for name in wanted:
        result = report["engines"][name]
        if not result["available"]:
            print(f"  {name:<14} UNAVAILABLE: {result['reason']}")
            continue
        segs = result["segments"]
        if not segs:
            print(f"  {name:<14} (no speech detected)")
        for i, seg in enumerate(segs):
            label = name if i == 0 else ""
            print(f"  {label:<14} {seg['start_ms']:>8.0f} .. {seg['end_ms']:>8.0f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
