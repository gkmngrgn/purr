# AGENTS.md

## What this is
Single-file Hugging Face Gradio Space for streaming speech-to-text (`app.py`).
Pipeline: mic -> FastRTC WebRTC -> optional Quail enhancement (aic-sdk, CPU, per connection) -> local streaming ASR (faster-whisper or Moonshine, CPU) -> live transcript. `README.md` is the Space card and deployment notes.
Documented solutions live in `docs/solutions/` (organized by category with YAML frontmatter: `module`, `tags`, `problem_type`); shared domain vocabulary is in `CONCEPTS.md`. Relevant when implementing or debugging in documented areas.

## Deploy
- Live Space: https://huggingface.co/spaces/goedev/purr. It is the git remote `origin`; pushing `main` redeploys (requires your HF write token).
- `README.md` YAML frontmatter drives Space metadata. `short_description` is capped at 60 characters and is validated on build; keep it short or the Space build/README validation fails.

## Commands
- Dependencies: `pyproject.toml` is the source of truth. `requirements.txt` is **generated** from it — do not hand-edit it. Regenerate with:
  `uv pip compile pyproject.toml --python-version 3.12 --universal --output-file requirements.txt`
- Install: `uv pip install -r requirements.txt`. A `.venv/` exists; on Windows use `.venv\Scripts\python.exe` (README's `. .venv/bin/activate` is POSIX-only).
- Run: `python app.py` -> http://localhost:7860
- Verify: `python smoke_test.py` (from the repo root). This is the only test. Type checks run through pre-commit: `uvx pre-commit run --all-files` (see Typing). It spawns subprocesses with `sys.executable` and exits non-zero on failure, so run it with the interpreter you want tested.

## Typing
- Production code is statically typed. Annotate function signatures, module-level state, and non-obvious locals, and prefer precise types over `Any`. Reserve dynamic typing for throwaway scripts.
- Two hooks run on every commit (`.pre-commit-config.yaml`): `ruff` enforces the `ANN` annotation rules (`select = ["ANN"]` in `[tool.ruff.lint]`), and `ty` checks the types. Run `uvx pre-commit install` once to enable them, or `uvx pre-commit run --all-files` to check everything on demand.

## Environment / secrets
- `AIC_SDK_LICENSE` (Quail) is read from the environment at call time. There is **no `HF_TOKEN` requirement** — ASR runs locally.
- Optional Cloudflare TURN on Spaces: `CLOUDFLARE_TURN_KEY_ID` and `CLOUDFLARE_TURN_KEY_API_TOKEN` (see `rtc_configuration()`).
- Importing `app` and constructing the UI must succeed with no secrets set (the smoke test asserts this). Quail and ASR only fail when actually invoked.

## Invariants that are easy to break
- The `gradio` pin in `pyproject.toml` must equal `sdk_version` in `README.md` (currently `5.50.0`). The Gradio Space build injects `gradio[oauth,mcp]==<sdk_version>`, and `fastrtc==0.0.34` caps `gradio<6`; bumping `sdk_version` to Gradio 6 makes `pip` fail with `ResolutionImpossible`.
- The Space runs on ZeroGPU. ZeroGPU refuses to start unless the app has at least one Gradio event handler bound to a `@spaces.GPU` function. `app.py` binds the no-op `_zerogpu_startup_probe` to `stream.ui`; keep it bound to a Gradio event (defining it alone is not enough — the startup scan walks Gradio's registered handlers). The app itself is CPU-only.
- `StreamingTranscriber` is a continuous `StreamHandler`, not `ReplyOnPause`: `receive()` appends each 16 kHz mono frame (optionally Quail-enhanced) to the current utterance, and `emit()` re-decodes it on a timer and returns `AdditionalOutputs(full_text)`. There is no turn/utterance-boundary input; a run of silence (`COMMIT_SILENCE_S`) or `MAX_UTTERANCE_S` commits a line. A partial decode uses only the last `PARTIAL_WINDOW_S` of audio; a commit decodes the whole utterance. A decode failure must not mutate transcript state (it surfaces once and retries later).
- UI wiring in `build_ui()`: `webrtc.stream(inputs=[webrtc, mode_radio], outputs=[webrtc])` and `webrtc.on_additional_outputs(_replace_transcript, inputs=[transcript], outputs=[transcript])`. The handler finds the enhancement mode by scanning `latest_args` for a `MODE_CHOICES` value; FastRTC's base `set_args` prepends the `"__webrtc_value__"` placeholder. Keep the components and their order in sync.
- Yield `AdditionalOutputs(value)` (bare value), never `AdditionalOutputs((value,))`. With a single output component Gradio wraps the prediction without unpacking, so the wrapped form renders the Textbox as a Python tuple like `('text',)`.
- `AIC_SAMPLE_RATE` must stay 16000 (Quail native; incoming WebRTC frames are resampled there via the handler's `input_sample_rate`). `OUTPUT_SAMPLE_RATE = 24000` satisfies FastRTC's multiple-of-50 handler constraint; the app emits no audio, so it has no other effect.
- Each connection gets its own `QuailStream` (a `Processor` + context), but the native `Processor.process` calls are serialized with the process-wide `_proc_lock`. Do not process concurrently without that lock.
- ASR is local and loaded lazily (`get_transcriber`): `PURR_ASR_ENGINE` selects `faster-whisper` (default, multilingual, `PURR_ASR_MODEL`, default `base`) or `moonshine` (`PURR_MOONSHINE_MODEL`). `_transcribe_lock` serializes decodes across connections. Never load a model at import.
- There is no audio playback by design: continuously echoing the mic would feed back through the speakers, so enhancement is observed through the transcript instead.
