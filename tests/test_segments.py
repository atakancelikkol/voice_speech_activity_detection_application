from server.vad.base import EventKind, VadEvent
from server.vad.segments import ProbabilityHysteresis, SegmentBuilder


def test_segment_builder_pairs_events():
    b = SegmentBuilder()
    b.on_event(VadEvent(EventKind.SPEECH_START, 100.0))
    b.on_event(VadEvent(EventKind.SPEECH_END, 900.0))
    b.on_event(VadEvent(EventKind.SPEECH_START, 1500.0))
    b.on_event(VadEvent(EventKind.SPEECH_END, 2000.0))
    assert [(s.start_ms, s.end_ms) for s in b.segments] == [(100.0, 900.0), (1500.0, 2000.0)]
    assert all(s.final for s in b.segments)


def test_segment_builder_ignores_unbalanced_events():
    b = SegmentBuilder()
    b.on_event(VadEvent(EventKind.SPEECH_END, 50.0))  # END without START
    b.on_event(VadEvent(EventKind.SPEECH_START, 100.0))
    b.on_event(VadEvent(EventKind.SPEECH_START, 200.0))  # duplicate START
    b.on_event(VadEvent(EventKind.SPEECH_END, 300.0))
    assert [(s.start_ms, s.end_ms) for s in b.segments] == [(100.0, 300.0)]


def test_segment_builder_open_and_finalize():
    b = SegmentBuilder()
    b.on_event(VadEvent(EventKind.SPEECH_START, 100.0))
    open_seg = b.open_segment(now_ms=250.0)
    assert open_seg is not None and not open_seg.final
    assert (open_seg.start_ms, open_seg.end_ms) == (100.0, 250.0)
    b.finalize(end_ms=400.0)
    assert [(s.start_ms, s.end_ms) for s in b.segments] == [(100.0, 400.0)]
    assert b.open_segment(500.0) is None


def _run(h: ProbabilityHysteresis, probs: list[float], frame_ms: float = 10.0):
    events = []
    for i, p in enumerate(probs):
        ev = h.update(p, i * frame_ms, frame_ms)
        if ev:
            events.append(ev)
    return events


def test_hysteresis_onset_backdated():
    h = ProbabilityHysteresis(threshold=0.5, min_speech_ms=50.0, min_silence_ms=50.0, speech_pad_ms=0.0)
    # 5 frames of 10ms above threshold starting at t=100ms
    probs = [0.0] * 10 + [0.9] * 10
    events = _run(h, probs)
    assert len(events) == 1
    assert events[0].kind is EventKind.SPEECH_START
    assert events[0].at_ms == 100.0  # backdated to start of the loud run


def test_hysteresis_short_blip_ignored():
    h = ProbabilityHysteresis(threshold=0.5, min_speech_ms=100.0, min_silence_ms=100.0)
    probs = [0.0] * 5 + [0.9] * 3 + [0.0] * 10  # 30 ms blip < 100 ms min speech
    assert _run(h, probs) == []


def test_hysteresis_full_cycle_with_pad():
    h = ProbabilityHysteresis(threshold=0.5, min_speech_ms=50.0, min_silence_ms=50.0, speech_pad_ms=20.0)
    probs = [0.0] * 10 + [0.9] * 20 + [0.0] * 20
    events = _run(h, probs)
    assert [e.kind for e in events] == [EventKind.SPEECH_START, EventKind.SPEECH_END]
    assert events[0].at_ms == 80.0  # 100 - 20 pad
    assert events[1].at_ms == 320.0  # silence began at 300, + 20 pad


def test_hysteresis_dip_shorter_than_min_silence_keeps_segment():
    h = ProbabilityHysteresis(threshold=0.5, min_speech_ms=50.0, min_silence_ms=100.0, speech_pad_ms=0.0)
    probs = [0.9] * 10 + [0.0] * 5 + [0.9] * 10 + [0.0] * 15
    events = _run(h, probs)
    assert [e.kind for e in events] == [EventKind.SPEECH_START, EventKind.SPEECH_END]
    assert events[1].at_ms == 250.0  # final silence started at 250 ms
