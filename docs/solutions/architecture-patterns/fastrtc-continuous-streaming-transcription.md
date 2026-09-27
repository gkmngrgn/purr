---
title: Replacing turn-based transcription with a continuous streaming StreamHandler for real-time ASR
date: 2026-09-27
category: architecture-patterns
module: app
problem_type: architecture_pattern
component: service_object
severity: medium
applies_when:
  - building a live speech-to-text pipeline where per-utterance turn latency is the bottleneck
  - a FastRTC StreamHandler must emit partial transcripts continuously instead of replying on pause
  - replacing a hosted ASR round-trip with local CPU inference (faster-whisper or Moonshine)
tags: [fastrtc, streaming, speech-to-text, faster-whisper, moonshine, ai-coustics, additional-outputs, real-time]
---

# Replacing turn-based transcription with a continuous streaming StreamHandler for real-time ASR

## Context

Purr advertises streaming speech-to-text, but its original pipeline was turn-based. FastRTC's
`ReplyOnPause` accumulated mic audio, ran an ai-coustics VAD over a sliding window to detect when
the speaker had stopped, then sent the whole utterance in one request to a hosted model
(`openai/whisper-large-v3` via HF Inference Providers). Text therefore appeared only after a pause,
and only after the hosted round trip plus the model completed.

The friction that prompted the redesign:

- No partial text appeared while the speaker was talking; output was gated on a detected turn
  boundary.
- Latency stacked per utterance: VAD pause detection, then the network request, then the model.
- A continuously talking user produced no output for as long as they kept talking, because no pause
  ever committed a turn.
- The pipeline depended on hosted inference even though Quail enhancement already ran locally on CPU.

Approaches that were tried and rejected:

- **Keeping `ReplyOnPause` and speeding up the model.** `ReplyOnPause` is a turn state machine: it
  emits only when its detector declares a pause, so no faster backend can produce text
  mid-utterance. The VAD pause detector in
  `docs/solutions/integration-issues/fastrtc-custom-vad-pause-detector.md` reduced to a latency
  knob, not a fix. FastRTC's own "real-time" transcription example is still turn-based
  `ReplyOnPause`, so copying it re-creates the problem.
- **Local `faster-whisper large-v3-turbo` int8 on CPU.** Attempted and reverted: decoding the full
  checkpoint on the Space's CPU was too slow per utterance to be the sole engine at that size.
- **Streaming from hosted Whisper.** The Inference Providers API is request/response only; it
  cannot emit partial hypotheses for a growing buffer. Streaming had to move in-process.
- **Keeping audio playback.** Continuously echoing the mic through the speakers feeds back, so
  playback was removed rather than fixed (`README.md`, Notes).

## Guidance

Rebuild the audio path around a continuous `StreamHandler`. The Space's `main` branch is the
push-to-deploy remote, so this landed directly on it; there is no GitHub PR for this change (the
predecessor VAD work was PR #2).

### 1. `StreamingTranscriber(StreamHandler)` replaces `ReplyOnPause`

`receive()` consumes every incoming frame; `emit()` re-decodes on a timer. There is no
turn-boundary input at all (`app.py:319-352`).

```python
class StreamingTranscriber(StreamHandler):
    def __init__(self, asr: Transcriber | None = None) -> None:
        super().__init__(
            expected_layout="mono",
            output_sample_rate=OUTPUT_SAMPLE_RATE,
            input_sample_rate=AIC_SAMPLE_RATE,
        )
        ...
        self._utt = np.zeros(0, dtype=np.float32)  # current utterance (possibly enhanced)
        self._utt_has_speech = False
        self._committed = ""  # committed transcript across utterances
        self._partial = ""  # live hypothesis for the current utterance
```

`receive()` normalizes each frame to 16 kHz mono, runs it through per-connection Quail, appends it
to the utterance buffer, and tracks trailing silence. It bounds pre-speech silence to a short
pre-roll and caps the buffer at `MAX_UTTERANCE_S` even before the ASR is ready, so a slow model load
cannot grow memory without limit (`app.py:414-438`).

`emit()` decides, under the lock, whether this tick is a partial, a commit, or nothing. A partial
decodes only the recent `PARTIAL_WINDOW_S` (8 s) so cost stays bounded; a commit decodes the whole
utterance so no words are lost (`app.py:440-472`):

```python
commit = self._silence_s >= COMMIT_SILENCE_S and self._utt_has_speech
due = (
    self._utt_has_speech
    and new_audio >= int(MIN_DECODE_AUDIO_S * AIC_SAMPLE_RATE)
    and (now - self._last_decode_t) >= DECODE_INTERVAL_S
)
...
window = utt if should_commit else utt[-int(PARTIAL_WINDOW_S * AIC_SAMPLE_RATE):]
```

The result is emitted as **bare** `AdditionalOutputs(full_text)` - never a wrapped tuple
(`app.py:512`). Streaming tunables live at `app.py:83-89`: `DECODE_INTERVAL_S = 0.9`,
`MIN_DECODE_AUDIO_S = 0.5`, `PARTIAL_WINDOW_S = 8.0`, `COMMIT_SILENCE_S = 0.9`,
`MAX_UTTERANCE_S = 20.0`, `PRE_ROLL_S = 0.4`, `SPEECH_RMS = 0.008`.

### 2. Per-connection streaming Quail with a process-wide lock

`QuailStream` owns one `Processor` per connection and processes audio continuously, re-blocking to
the model's native block size across frames (`app.py:133-167`). The native `Processor.process`
calls are serialized process-wide, because the SDK is not safe to touch concurrently
(`app.py:161`):

```python
with _proc_lock:
    out = [
        self._processor.process(np.ascontiguousarray(buf[i : i + self._block]))
        for i in range(0, full, self._block)
    ]
```

The processor is built lazily inside `_enhance()`; a mid-stream enhancement failure disables
enhancement for that connection and falls back to raw audio rather than dropping the session
(`app.py:366-383`).

### 3. Local CPU ASR behind a small backend protocol

A `Transcriber` protocol (`prepare()` + `transcribe()`) abstracts two local backends
(`app.py:175-221`): `FasterWhisperTranscriber` (default, multilingual, CTranslate2 int8) and
`MoonshineTranscriber` (English-only, lower latency, ONNX). `get_transcriber()` resolves it once
lazily, selected by `PURR_ASR_ENGINE` / `PURR_ASR_MODEL` / `PURR_MOONSHINE_MODEL` (`app.py:78-80`).

Partial decodes use `beam_size=1`, `vad_filter=False`, `condition_on_previous_text=False`, and
`without_timestamps=True` to keep each re-decode cheap. Decodes are serialized by
`_transcribe_lock` (`app.py:409`).

`warm_asr()` starts on a daemon thread from `start_up()` and calls the backend's `prepare()`, so the
first utterance does not block on a cold model download (`app.py:264-278`). A load failure is
recorded once; later decodes short-circuit on the message instead of retrying the download
(`app.py:231-246`).

### 4. A decode failure must not corrupt transcript state

If `transcribe` raises, the handler keeps the buffered utterance and the live partial, records the
error, and surfaces it once as a note - never fabricating a partial or committing empty text
(`app.py:475-512`):

```python
try:
    text, detected = self._transcribe(audio, language)
except Exception as exc:
    # A decode failure must not disturb the transcript: keep the live
    # partial and the buffered utterance, and surface the error once.
    print(f"[asr] transcription failed: {exc}")
    self._decode_error = str(exc)
    failed = True
    text, detected = "", language
```

`_note_error_once()` de-duplicates the note across decode, enhancement, and ASR-load errors
(`app.py:390-399`).

### 5. Playback removed

No audio is emitted; enhancement is observed through the transcript instead (`README.md`, Notes).

## Why This Matters

- **Decoupling capture from output.** `ReplyOnPause` made output conditional on a detected turn
  boundary, coupling latency and speaking style by design. With a continuous `StreamHandler`, the
  browser streams frames in and Gradio pushes `AdditionalOutputs` back on its own cadence, so text
  can appear mid-utterance. No pause is required to produce output (`app.py:440-512`).
- **Bounded work per tick.** Partial decodes read only the last 8 s and are throttled by
  `DECODE_INTERVAL_S` / `MIN_DECODE_AUDIO_S`, so CPU cost per tick is roughly constant regardless of
  utterance length. The full-utterance decode is reserved for commits, which are rarer and
  semantically final; `MAX_UTTERANCE_S` forces a commit so one unbroken utterance cannot drive cost
  or memory without bound (`app.py:449`, `app.py:434-437`).
- **Local inference removes the network term.** The hosted round trip was a fixed per-utterance
  latency add-on; running faster-whisper or Moonshine in-process removes it and also removes the
  Inference Providers token requirement.
- **`int8` + `beam_size=1`** keep CPU decode fast enough to re-run every ~0.9 s, which is what makes
  partials feel live.
- **State isolation.** Each connection gets a fresh handler (`copy()` at `app.py:351`) and its own
  Quail processor; shared singletons are guarded by `_init_lock` / `_asr_lock`, and the two truly
  unsafe shared operations - native Quail `process` and ASR decode - are serialized with
  `_proc_lock` and `_transcribe_lock` (`app.py:161`, `app.py:409`).
- **Error containment.** A transient decode failure leaves `_utt`, `_committed`, and `_partial`
  untouched, so the next tick retries the same audio and the user sees one note instead of a wiped
  transcript (`app.py:477-508`).

## When to Apply

- Building a live speech-to-text pipeline where per-utterance turn latency is the bottleneck.
- A FastRTC `StreamHandler` must emit partial transcripts continuously instead of replying on pause.
- Replacing a hosted ASR round-trip with local CPU inference (faster-whisper or Moonshine).

## Examples

Before - output waits for a turn boundary, so nothing appears while speaking:

```python
stream = Stream(SpeechAwarePauseHandler(process, model=AICVadAdapter()), ...)  # ReplyOnPause
```

After - a timer drives output and partials stream continuously:

```python
stream = Stream(StreamingTranscriber(), modality="audio", mode="send-receive", ...)
# emit() returns a bare value; a wrapped tuple renders the Textbox as a Python tuple:
return AdditionalOutputs(full)
```

The emit contract has one easy footgun: `AdditionalOutputs(value)`, never
`AdditionalOutputs((value,))`. With a single output component, Gradio wraps the prediction without
unpacking, so the wrapped form renders the Textbox literally as `('text',)`.

## Related

- `docs/solutions/integration-issues/fastrtc-custom-vad-pause-detector.md` - superseded predecessor:
  its `ReplyOnPause` / `AICVadAdapter` path no longer exists; kept for the reusable FastRTC
  `PauseDetectionModel` lessons. That work was PR #2.
- `docs/solutions/integration-issues/fastrtc-dead-turn-endpoint.md` - the WebRTC connection layer
  that delivers frames into this handler.
- `docs/solutions/integration-issues/huggingface-spaces-zerogpu-deployment-failure.md` - the
  ZeroGPU probe invariant and the `gradio==5.50.0` / `sdk_version` pin that must survive any
  pipeline change.
- `docs/plans/2026-09-26-001-feat-aic-vad-pause-detection-plan.md` - the turn-based design this
  replaced.
- `CONCEPTS.md` - shared vocabulary (Continuous streaming, Partial transcript, Utterance, Local ASR,
  Quail enhancement).
