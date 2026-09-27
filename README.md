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
short_description: 'Real-time speech-to-text: Quail enhancement + Whisper'
---

# Purr

🔗 **[Live demo on HF Spaces](https://huggingface.co/spaces/goedev/purr)**

A real-time speech-to-text demo built on the Hugging Face stack:

- **FastRTC** — low-latency WebRTC audio streaming into a Gradio UI
- **ai-coustics Quail** (`quail-ms-l-16khz`) — CPU-first speech enhancement tuned to improve downstream STT accuracy
- **Whisper (`whisper-large-v3-turbo`)** — transcribed locally on CPU via **faster-whisper**, so no hosted inference credits or token are needed

Hold the mic button, speak, release. You'll hear the (optionally enhanced) audio played back and the transcript appear. Toggle **Quail speech enhancement** between *enhanced* and *raw* to hear and read the difference — the audio you hear in *raw* mode is the fidelity Whisper is actually trying to decode.

## Secrets

Configure in **Space → Settings → Variables and secrets**:

| Secret | Purpose |
|---|---|
| `AIC_SDK_LICENSE` | ai-coustics SDK license key from https://developers.ai-coustics.com |

Transcription runs locally, so no Inference Providers token is required. Cloudflare TURN (optional, for relay) reads `CLOUDFLARE_TURN_KEY_ID` and `CLOUDFLARE_TURN_KEY_API_TOKEN` when set.

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

Open http://localhost:7860. No token is needed: transcription runs locally, and TURN credentials are only fetched on Spaces (via `CLOUDFLARE_TURN_KEY_ID` / `CLOUDFLARE_TURN_KEY_API_TOKEN`).

## How it works

1. `ReplyOnPause` accumulates the mic stream and fires once you stop speaking.
2. In *enhanced* mode, the utterance is resampled to 16 kHz and processed block-by-block through Quail (`ProcessorContext.reset()` per utterance), which runs CPU-only with sub-40 ms latency.
3. The processed audio is played back over WebRTC and transcribed in parallel: the 16 kHz buffer is fed to `faster-whisper` (`whisper-large-v3-turbo`, int8, CPU, one pass per utterance).
4. The transcript is appended to the textbox via FastRTC `AdditionalOutputs`.

## Notes

- The Space runs on **ZeroGPU** (Hugging Face's free tier for Gradio Spaces). ZeroGPU requires at least one `@spaces.GPU` handler, so `app.py` registers a no-op startup probe bound to the UI; the app itself is CPU-only. Switching to `cpu-basic` requires a paid plan.
- The Quail model (~35 MB) is downloaded to `models/` on first use; it is cached in the Space until the container restarts. The Whisper checkpoint (~1.6 GB) downloads to the Hugging Face cache on first transcription.
- Whisper runs on CPU, so transcription takes a few seconds per utterance depending on length; Quail enhancement shortens and cleans the audio first, which helps accuracy.
- The shared Quail processor is serialized with a lock, so concurrent sessions process sequentially (a demo-grade tradeoff).
- Privacy: audio leaves the Space only through (a) the optional Cloudflare TURN relay and (b) the model download from the Hugging Face Hub. Transcription itself stays on the Space.

## License

This project's own code is released under the [MIT License](LICENSE).

Third-party components keep their own terms and are **not** covered by this license:

- **ai-coustics Quail** (`quail-ms-l-16khz`) — model and `AIC_SDK_LICENSE` are governed by ai-coustics (https://developers.ai-coustics.com).
- **Whisper** (`whisper-large-v3-turbo`) — model weights under OpenAI's terms; run locally via [faster-whisper](https://github.com/SYSTRAN/faster-whisper).
