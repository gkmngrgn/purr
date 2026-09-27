---
title: Purr
emoji: 🎙️
colorFrom: green
colorTo: green
sdk: gradio
sdk_version: 5.50.0
python_version: '3.12'
app_file: app.py
pinned: false
license: apache-2.0
short_description: 'Streaming speech-to-text: Quail enhancement + local ASR'
---

# Purr

🔗 **[Live demo on HF Spaces](https://huggingface.co/spaces/goedev/purr)**

A streaming speech-to-text demo built on the Hugging Face stack:

- **FastRTC** — low-latency WebRTC audio streaming into a Gradio UI
- **ai-coustics Quail** (`quail-ms-l-16khz`) — CPU-first speech enhancement tuned to improve downstream STT accuracy
- **Local ASR** — Whisper via `faster-whisper` (multilingual, `base` by default), or Moonshine for lower latency. It runs on the Space's CPU, so there is no hosted-inference round trip, no API token, and no per-request cost.

Tap the mic and start speaking: the transcript updates **while you talk** and finalizes a line after a pause. Toggle **Quail speech enhancement** between *enhanced* and *raw* to compare the transcript Quail produces against what the raw microphone audio yields.

## Secrets

Configure one secret in **Space → Settings → Variables and secrets**:

| Secret | Purpose |
|---|---|
| `AIC_SDK_LICENSE` | ai-coustics SDK license key from https://developers.ai-coustics.com |

No `HF_TOKEN` is required: transcription runs locally. (Cloudflare TURN relay on Spaces is configured separately with `CLOUDFLARE_TURN_KEY_ID` / `CLOUDFLARE_TURN_KEY_API_TOKEN`; see `rtc_configuration()` in `app.py`.)

## Run locally

`pyproject.toml` is the source of truth for dependencies; `requirements.txt` is generated from it with [uv](https://docs.astral.sh/uv/). Do not hand-edit `requirements.txt`.

```bash
uv venv --python 3.12 && . .venv/bin/activate   # Windows: .venv\Scripts\activate
uv pip install -r requirements.txt
export AIC_SDK_LICENSE=...        # Windows: $env:AIC_SDK_LICENSE = "..."
python app.py
```

After changing dependencies in `pyproject.toml`, regenerate the lock:

```bash
uv pip compile pyproject.toml --python-version 3.12 --universal --output-file requirements.txt
```

Open http://localhost:7860. WebRTC in a browser works without a token (TURN credentials are only fetched on Spaces).

## Configuration

Both ASR backends run locally on CPU:

| Variable | Default | Meaning |
|---|---|---|
| `PURR_ASR_ENGINE` | `faster-whisper` | `faster-whisper` (multilingual) or `moonshine` (English-only, lower latency) |
| `PURR_ASR_MODEL` | `base` | faster-whisper checkpoint: `tiny`, `base`, `small`, … |
| `PURR_MOONSHINE_MODEL` | `moonshine/base` | `moonshine/base` or `moonshine/tiny` |

## How it works

1. FastRTC streams mic audio over WebRTC; frames are resampled to 16 kHz mono before reaching the handler.
2. In *enhanced* mode, each frame is processed block-by-block through a per-connection Quail processor (its own `Processor` and context), which runs CPU-only.
3. The enhanced (or raw) audio is appended to the current utterance buffer and **re-decoded on a timer** (~every 0.9 s). Each partial decodes only the most recent ~8 s; committing a line decodes the whole utterance, so no words are lost. Partial text streams into the transcript as you speak; a run of ~0.9 s of silence (or a 20 s cap) commits the line and starts a new one. A transient decode error is surfaced once and never discards buffered audio.
4. ASR runs locally in-process: `faster-whisper` (CTranslate2, `int8`) or Moonshine (ONNX). The transcript is pushed to the textbox via FastRTC `AdditionalOutputs`.

## Notes

- The Space runs on **ZeroGPU** (Hugging Face's free tier for Gradio Spaces). ZeroGPU requires at least one `@spaces.GPU` handler, so `app.py` registers a no-op startup probe bound to the UI; the app itself is CPU-only. Switching to `cpu-basic` requires a paid plan.
- **Models download on first use**: Quail (~35 MB) into `models/`, and the ASR checkpoint from the Hugging Face cache (`base` ~150 MB). The first partial after a cold start is therefore slower.
- Every connection gets its own Quail processor; the native SDK `process` calls are serialized with a process-wide lock, so concurrent sessions enhance sequentially (a demo-grade tradeoff). ASR decodes are serialized too.
- There is **no audio playback**. Continuously echoing the mic would feed back through the speakers, so the enhancement is observable through the transcript rather than audible.
- Privacy: audio is processed in the Space itself. It only leaves via the optional Cloudflare TURN relay; no external ASR provider is involved.

## License

This project's own code is released under the [MIT License](LICENSE).

Third-party components keep their own terms and are **not** covered by this license:

- **ai-coustics Quail** (`quail-ms-l-16khz`) — model and `AIC_SDK_LICENSE` are governed by ai-coustics (https://developers.ai-coustics.com).
- **Whisper** (`faster-whisper`) — model weights under OpenAI's terms.
- **Moonshine** (`fastrtc-moonshine-onnx`) and **ONNX Runtime** — governed by their respective licenses.
