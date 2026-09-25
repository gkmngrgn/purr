# AGENTS.md

## What this is
Single-file Hugging Face Gradio Space for real-time speech-to-text (`app.py`).
Pipeline: mic -> FastRTC `ReplyOnPause` (WebRTC) -> optional Quail enhancement (aic-sdk, CPU) -> Whisper via HF Inference Providers -> transcript. `README.md` is the Space card and deployment notes.

## Commands
- Install: `pip install -r requirements.txt`. A `.venv/` exists; on Windows use `.venv\Scripts\python.exe` (README's `. .venv/bin/activate` is POSIX-only).
- Run: `python app.py` -> http://localhost:7860
- Verify: `python smoke_test.py` (from the repo root). This is the only test; there is no pytest/lint/typecheck config. It spawns subprocesses with `sys.executable` and exits non-zero on failure, so run it with the interpreter you want tested.

## Environment / secrets
- `HF_TOKEN` (Inference Providers, and TURN on Spaces) and `AIC_SDK_LICENSE` (Quail) are read from the environment at call time.
- Importing `app` and constructing the UI must succeed with neither secret set (the smoke test asserts this). Quail and transcription only fail when actually invoked.

## Invariants that are easy to break
- `process(audio, mode, transcript)` is invoked positionally from `Stream(additional_inputs=[mode_radio, transcript])`; keep those components and that order in sync.
- Yield `AdditionalOutputs(value)` (bare value), never `AdditionalOutputs((value,))`. With a single output component Gradio wraps the prediction without unpacking, so the wrapped form renders the Textbox as a Python tuple like `('text',)`.
- FastRTC calls the outputs handler as `handler(current_component_value, *AdditionalOutputs.args)`; the first argument is the current transcript value, not the WebRTC id.
- `AIC_SAMPLE_RATE` must stay 16000 (Quail native). Playback uses `OUTPUT_SAMPLE_RATE = 24000`, which must match `ReplyOnPause`'s default `output_sample_rate`.
- Audio yielded for playback is `(24000, int16 ndarray of shape (1, N))`.
- The Quail `Processor` is a process-wide singleton guarded by `_proc_lock`; every utterance is serialized through it. Do not process concurrently without that lock.
- `is_space()` (driven by `SYSTEM=spaces`) gates `concurrency_limit`, `time_limit`, and TURN credential lookup; local runs intentionally get no TURN and unlimited concurrency.
