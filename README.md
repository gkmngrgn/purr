---
title: Purr
emoji: 🎙️
colorFrom: green
colorTo: green
sdk: gradio
sdk_version: 6.28.0
python_version: '3.12'
app_file: app.py
pinned: false
license: apache-2.0
short_description: 'Real-time speech-to-text: Quail enhancement + Whisper'
---

# Clean Transcribe

🔗 **[Live demo on HF Spaces](https://huggingface.co/spaces/goedev/purr)**

A real-time speech-to-text demo built on the Hugging Face stack:

- **FastRTC** — low-latency WebRTC audio streaming into a Gradio UI
- **ai-coustics Quail** (`quail-ms-l-16khz`) — CPU-first speech enhancement tuned to improve downstream STT accuracy
- **Whisper (`openai/whisper-large-v3`)** — transcribed via **HF Inference Providers**, so the whole app stays inside the HF ecosystem with a single token

Hold the mic button, speak, release. You'll hear the (optionally enhanced) audio played back and the transcript appear. Toggle **Quail speech enhancement** between *enhanced* and *raw* to hear and read the difference — the audio you hear in *raw* mode is the fidelity Whisper is actually trying to decode.

## Secrets

Configure both in **Space → Settings → Variables and secrets**:

| Secret | Purpose |
|---|---|
| `HF_TOKEN` | Inference Providers token with **Make calls to Inference Providers** permission (also used for TURN on Spaces) |
| `AIC_SDK_LICENSE` | ai-coustics SDK license key from https://developers.ai-coustics.com |

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export HF_TOKEN=hf_...
export AIC_SDK_LICENSE=...
python app.py
```

Open http://localhost:7860. WebRTC in a browser works without a token (TURN credentials are only fetched on Spaces).

## How it works

1. `ReplyOnPause` accumulates the mic stream and fires once you stop speaking.
2. In *enhanced* mode, the utterance is resampled to 16 kHz and processed block-by-block through Quail (`ProcessorContext.reset()` per utterance), which runs CPU-only with sub-40 ms latency.
3. The processed audio is played back over WebRTC and transcribed in parallel: it's WAV-encoded and sent to `InferenceClient.automatic_speech_recognition(provider="auto")`.
4. The transcript is appended to the textbox via FastRTC `AdditionalOutputs`.

## Notes

- The Quail model (~35 MB) is downloaded to `models/` on first use; it is cached in the Space until the container restarts.
- The shared Quail processor is serialized with a lock, so concurrent sessions process sequentially (a demo-grade tradeoff).
- Privacy: audio leaves the Space for (a) transcription via HF Inference Providers (serverless) and (b) optional Cloudflare TURN relay. Do not feed it sensitive data.

## License

This project's own code is released under the [MIT License](LICENSE).

Third-party components keep their own terms and are **not** covered by this license:

- **ai-coustics Quail** (`quail-ms-l-16khz`) — model and `AIC_SDK_LICENSE` are governed by ai-coustics (https://developers.ai-coustics.com).
- **Whisper** (`openai/whisper-large-v3`) — model weights under OpenAI's terms; used via HF Inference Providers.
