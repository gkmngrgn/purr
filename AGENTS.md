# AGENTS.md

## What this is
Single-file Hugging Face Gradio Space for real-time speech-to-text (`app.py`).
Pipeline: mic -> FastRTC `ReplyOnPause` (WebRTC) -> optional Quail enhancement (aic-sdk, CPU) -> Whisper via HF Inference Providers -> transcript. `README.md` is the Space card and deployment notes.
Documented solutions live in `docs/solutions/` (organized by category with YAML frontmatter: `module`, `tags`, `problem_type`); shared domain vocabulary is in `CONCEPTS.md`. Relevant when implementing or debugging in documented areas.

## Deploy
- Live Space: https://huggingface.co/spaces/goedev/purr. It is the git remote `origin`; pushing `main` redeploys (requires your HF write token).
- `README.md` YAML frontmatter drives Space metadata. `short_description` is capped at 60 characters and is validated on build; keep it short or the Space build/README validation fails.

## Commands
- Dependencies: `pyproject.toml` is the source of truth. `requirements.txt` is **generated** from it — do not hand-edit it. Regenerate with:
  `uv pip compile pyproject.toml --python-version 3.12 --universal --output-file requirements.txt`
- Install: `uv pip install -r requirements.txt`. A `.venv/` exists; on Windows use `.venv\Scripts\python.exe` (README's `. .venv/bin/activate` is POSIX-only).
- Run: `python app.py` -> http://localhost:7860
- Verify: `python smoke_test.py` (from the repo root). This is the only test; there is no pytest/lint/typecheck config. It spawns subprocesses with `sys.executable` and exits non-zero on failure, so run it with the interpreter you want tested.

## Environment / secrets
- `HF_TOKEN` (Inference Providers, and TURN on Spaces) and `AIC_SDK_LICENSE` (Quail) are read from the environment at call time.
- Importing `app` and constructing the UI must succeed with neither secret set (the smoke test asserts this). Quail and transcription only fail when actually invoked.

## Invariants that are easy to break
- The `gradio` pin in `pyproject.toml` must equal `sdk_version` in `README.md` (currently `5.50.0`). The Gradio Space build injects `gradio[oauth,mcp]==<sdk_version>`, and `fastrtc==0.0.34` caps `gradio<6`; bumping `sdk_version` to Gradio 6 makes `pip` fail with `ResolutionImpossible`.
- The Space runs on ZeroGPU (Hugging Face's free Gradio tier). ZeroGPU refuses to start unless the app has at least one Gradio event handler bound to a `@spaces.GPU` function. `app.py` binds the no-op `_zerogpu_startup_probe` to `stream.ui`; keep it bound to a Gradio event (defining it alone is not enough — the startup scan walks Gradio's registered handlers). The app itself is CPU-only.
- `process(audio, mode, transcript)` is invoked positionally from `Stream(additional_inputs=[mode_radio, transcript])`; keep those components and that order in sync.
- Yield `AdditionalOutputs(value)` (bare value), never `AdditionalOutputs((value,))`. With a single output component Gradio wraps the prediction without unpacking, so the wrapped form renders the Textbox as a Python tuple like `('text',)`.
- FastRTC calls the outputs handler as `handler(current_component_value, *AdditionalOutputs.args)`; the first argument is the current transcript value, not the WebRTC id.
- `AIC_SAMPLE_RATE` must stay 16000 (Quail native). Playback uses `OUTPUT_SAMPLE_RATE = 24000`, which must match `ReplyOnPause`'s default `output_sample_rate`.
- Audio yielded for playback is `(24000, int16 ndarray of shape (1, N))`.
- The Quail `Processor` is a process-wide singleton guarded by `_proc_lock`; every utterance is serialized through it. Do not process concurrently without that lock.
- `is_space()` (driven by `SYSTEM=spaces`) gates `concurrency_limit`, `time_limit`, and TURN credential lookup; local runs intentionally get no TURN and unlimited concurrency.
