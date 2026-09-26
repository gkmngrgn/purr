---
title: Using an ai-coustics VAD as FastRTC's pause detector
date: 2026-09-27
category: integration-issues
module: app
problem_type: integration_issue
component: service_object
symptoms:
  - "silero_vad.onnx is downloaded at startup even though audio processing is meant to be entirely ai-coustics"
  - "ReplyOnPause gives no signal that a second, unrelated VAD is in use"
root_cause: config_error
resolution_type: code_fix
severity: medium
tags: [fastrtc, vad, ai-coustics, pause-detection, gradio, adapter]
---

# Using an ai-coustics VAD as FastRTC's pause detector

## Problem

FastRTC's `ReplyOnPause` defaults to the bundled Silero VAD, so the app fetched `silero_vad.onnx` at runtime even though the pipeline is meant to be a single ai-coustics stack. Replacing the detector requires implementing FastRTC's `PauseDetectionModel` protocol and working around several framework behaviours that are not obvious from the call site.

## Symptoms

- `silero_vad.onnx: downloading bytes...` appears in the startup logs, pulled from the Hub by FastRTC.
- `ReplyOnPause(process)` raises no error and names no detector, so nothing signals that a second, unrelated VAD is running.

## What Didn't Work

- **Assuming the detector is not configurable.** `ReplyOnPause.__init__` does `self.model = model or get_silero_model()`, so Silero is only skipped when a truthy `model=` implementing the protocol is passed.
- **Removing the `fastrtc[vad]` extra before wiring the adapter.** The default detector's import then raises at `import app`; the wiring and the dependency edit must land together.
- **Overriding `copy()` via `super().copy()`.** FastRTC's base `copy()` returns a `ReplyOnPause` (not the subclass) and passes the same model instance, which defeats the per-connection detector.

## Solution

Implement the protocol and inject it:

```python
class AICVadAdapter:
    def warmup(self) -> None:
        ...  # FastRTC never calls this on a supplied model

    def vad(self, audio, options=None) -> tuple[float, list[AudioChunk]]:
        sr, arr = audio            # ~0.6 s window at 48 kHz, not the whole utterance
        # resample to the model's native rate, chunk with tail padding,
        # reset the detector context per window, count blocks whose raw
        # probability clears a threshold -> speech seconds
        return speech_seconds, []
```

Wire it in and drop Silero's dependency trail:

```python
ReplyOnPause(process, model=AICVadAdapter())
# pyproject: "fastrtc[vad]==0.0.34" -> "fastrtc==0.0.34"
```

Supporting pieces that made it work:

- **Per-connection detector.** Override `copy()` to build a fresh adapter per connection; cache only the downloaded model process-wide.
- **Fail closed.** On any detector error return `(0.0, [])` and remember the failure, so a broken detector produces no turns and does not accumulate the utterance buffer.
- **Speechless press.** A `ReplyOnPause` subclass emits a no-speech line when a press ends with no speech, guarded by whether the detector actually evaluated a window for that press.

## Why This Works

- `determine_pause` consumes only the returned speech-seconds value; the adapter is the seam, so FastRTC's turn state machine is untouched.
- Returning `0.0` keeps `started_talking` False and the buffer empty. A sentinel like `math.inf` does the opposite: it starts a turn that never ends and grows `state.stream` without bound.
- `warmup()` is only invoked by FastRTC's own Silero loader, so a supplied model must warm itself on first use.

## Prevention

- Assert the detector is wired, not just constructed: `isinstance(stream.event_handler.model, AICVadAdapter)`, and assert no VAD is built at import without secrets.
- Keep startup secret-free: build the ai-coustics VAD lazily so `import app` succeeds with no `AIC_SDK_LICENSE`.
- Test the adapter's real path (resample, padding, threshold) with a fake SDK, since the licensed path is a manual gate.

## Related Issues

- `docs/solutions/integration-issues/huggingface-spaces-zerogpu-deployment-failure.md`
- `docs/plans/2026-09-26-001-feat-aic-vad-pause-detection-plan.md`
- Fix opened as PR #2 on the `goedev/purr` Space; unmerged as of this writing.
