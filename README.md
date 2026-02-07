# purr

Real-time speech transcription CLI using [Voxtral](https://huggingface.co/mistralai/Voxtral-Mini-4B-Realtime-2602) via vLLM.

Captures audio from mic or system sources and streams it over WebSocket to a vLLM realtime API server. Transcribed text prints to stdout as it arrives. No cloud API key needed — the model runs on your own GPU.

## Prerequisites

- Python 3.14+
- [uv](https://docs.astral.sh/uv/)
- vLLM serving Voxtral on a machine with a GPU:
  ```
  vllm serve mistralai/Voxtral-Mini-4B-Realtime-2602
  ```
- PulseAudio/PipeWire for audio capture (works under WSL2 via WSLg)

## Setup

```
uv sync
```

## Usage

List available audio input devices:

```
uv run purr devices
```

Transcribe from default mic:

```
uv run purr listen
```

Transcribe from a specific device:

```
uv run purr listen -d <device-index>
```

Connect to a remote vLLM server (e.g. your GPU machine):

```
uv run purr listen --host 192.168.1.100
```

All options:

```
uv run purr listen --help
```

Press `Ctrl+C` to stop.

## Architecture

```
┌─────────────────────────────┐      WebSocket       ┌──────────────────────┐
│  CLI Client (any machine)   │ ──────────────────▶   │  vLLM Server (GPU)   │
│                             │  ws://host:8000       │                      │
│  audio.py: capture PCM      │  /v1/realtime         │  Voxtral-Mini-4B     │
│  transcriber.py: WS client  │ ◀──────────────────── │  Realtime-2602       │
│  cli.py: display text       │  transcription.delta  │                      │
└─────────────────────────────┘                       └──────────────────────┘
```

The client captures audio, encodes it as PCM 16-bit 16kHz mono, and streams base64 chunks over WebSocket. The server returns incremental transcription deltas. This split naturally supports multi-machine setups — run vLLM on the strong machine, connect from anywhere.
