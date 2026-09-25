"""Clean Transcribe: real-time speech-to-text on the HF Stack.

Pipeline:  mic -> FastRTC (WebRTC) -> Quail enhancement (optional) -> Whisper
via Inference Providers -> transcript.

Deploys as a Gradio Space. Requires two secrets:
    HF_TOKEN          token with "Make calls to Inference Providers" permission
    AIC_SDK_LICENSE   ai-coustics SDK license key (https://developers.ai-coustics.com)
"""

from __future__ import annotations

import io
import os
import threading
import wave

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import numpy as np

import gradio as gr
from gradio.utils import get_space

import librosa
from fastrtc import AdditionalOutputs, ReplyOnPause, Stream

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

QUAIL_MODEL_ID = "quail-ms-l-16khz"  # Quail Multi-Speaker: tuned for STT accuracy
MODELS_DIR = "models"
AIC_SAMPLE_RATE = 16000  # Quail native sample rate
OUTPUT_SAMPLE_RATE = 24000  # FastRTC output sample rate (multiple of 50)
WHISPER_MODEL = "openai/whisper-large-v3"
TEST_AUDIO_URL = (
    "https://storage.googleapis.com/media-session/sintel/snow-fight.mp3"
)


def is_space() -> bool:
    try:
        return bool(get_space())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Lazy singletons
# ---------------------------------------------------------------------------

_init_lock = threading.Lock()
_proc_lock = threading.Lock()
_quail: dict = {}
_client_cache: list = []


def get_hf_client():
    if not _client_cache:
        with _init_lock:
            if not _client_cache:
                from huggingface_hub import InferenceClient

                _client_cache.append(InferenceClient(provider="auto"))
    return _client_cache[0]


def load_quail() -> tuple:
    """Download (once) and initialize the Quail enhancement processor."""
    with _init_lock:
        if "processor" in _quail:
            return _quail["processor"], _quail["config"]
        import aic_sdk as aic

        license_key = os.environ.get("AIC_SDK_LICENSE")
        if not license_key:
            raise RuntimeError("AIC_SDK_LICENSE is not set.")
        model_path = aic.Model.download(QUAIL_MODEL_ID, MODELS_DIR)
        model = aic.Model.from_file(model_path)
        processor = aic.Processor(model, license_key)
        config = aic.ProcessorConfig.optimal(model, sample_rate=AIC_SAMPLE_RATE)
        processor.initialize(config)
        _quail["processor"] = processor
        _quail["config"] = config
        return processor, config


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------


def to_mono_float32(audio: tuple[int, np.ndarray]) -> tuple[int, np.ndarray]:
    """Normalize a FastRTC (sr, array) tuple to (sr, mono float32 in [-1, 1])."""
    sr, arr = audio
    arr = np.asarray(arr)
    if arr.ndim == 2:
        arr = arr.mean(axis=0) if arr.shape[0] > 1 and arr.shape[1] > 0 else arr.reshape(-1)
    arr = arr.reshape(-1)
    if arr.dtype.kind in "iu":
        arr = arr.astype(np.float32) / 32767.0
    else:
        arr = arr.astype(np.float32)
    return sr, np.clip(arr, -1.0, 1.0)


def resample(x: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    if x is None or len(x) == 0 or orig_sr == target_sr:
        return x
    return librosa.resample(x, orig_sr=orig_sr, target_sr=target_sr, res_type="soxr_hq")


def enhance_16k(y16: np.ndarray) -> np.ndarray:
    """Run Quail over a 16 kHz mono float32 buffer, resetting per utterance."""
    if len(y16) == 0:
        return y16
    processor, config = load_quail()
    block_size = int(config.block_size)
    with _proc_lock:
        ctx = processor.get_context()
        ctx.reset()
        out = []
        for i in range(0, len(y16), block_size):
            block = np.ascontiguousarray(y16[i : i + block_size], dtype=np.float32)
            if len(block) < block_size:
                block = np.pad(block, (0, block_size - len(block)))
            out.append(processor.process(block))
        return np.concatenate(out)[: len(y16)]


def to_int16(y: np.ndarray) -> np.ndarray:
    return (np.clip(y, -1.0, 1.0) * 32767.0).astype(np.int16)


def wav_bytes(y16: np.ndarray) -> bytes:
    """Encode 16 kHz mono int16 samples as RIFF/WAV bytes (no ffmpeg needed)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(AIC_SAMPLE_RATE)
        w.writeframes(np.ascontiguousarray(y16, dtype=np.int16).tobytes())
    return buf.getvalue()


def transcribe_16k(y16: np.ndarray) -> str:
    """Transcribe a 16 kHz mono float32 buffer with Whisper via Inference Providers."""
    client = get_hf_client()
    result = client.automatic_speech_recognition(
        audio=wav_bytes(to_int16(y16)), model=WHISPER_MODEL
    )
    return (result.text or "").strip()


def playback_chunks(y16: np.ndarray, chunk_s: float = 0.3):
    """Yield (OUTPUT_SAMPLE_RATE, int16 mono) chunks for WebRTC playback."""
    y24 = resample(y16, AIC_SAMPLE_RATE, OUTPUT_SAMPLE_RATE)
    if len(y24) == 0:
        return
    y24i = to_int16(y24).reshape(1, -1)
    step = int(OUTPUT_SAMPLE_RATE * chunk_s)
    for i in range(0, len(y24i[0]), step):
        yield (OUTPUT_SAMPLE_RATE, y24i[:, i : i + step])


# ---------------------------------------------------------------------------
# Stream handler
# ---------------------------------------------------------------------------


def process(audio: tuple[int, np.ndarray], mode: str | None, transcript: str):
    """Handle one spoken utterance: enhance (optionally), play back, transcribe."""
    sr, mono = to_mono_float32(audio)
    y16 = resample(mono, sr, AIC_SAMPLE_RATE)

    use_enhance = mode is None or mode == "enhanced"
    note = ""
    if use_enhance:
        try:
            enhanced = enhance_16k(y16)
        except Exception as exc:
            note = f"\n[Quail unavailable: {exc}]"
            enhanced = y16
    else:
        enhanced = y16

    # Stream the (possibly enhanced) audio back so the difference is audible.
    yield from playback_chunks(enhanced)

    try:
        text = transcribe_16k(enhanced)
    except Exception as exc:
        text = f"[transcription error: {exc}]"

    if not text:
        text = "[no speech detected]"

    updated = f"{transcript}\n{text}{note}" if transcript else f"{text}{note}"
    yield AdditionalOutputs(updated)


# ---------------------------------------------------------------------------
# Gradio UI + Stream
# ---------------------------------------------------------------------------


def rtc_configuration():
    """TURN credentials on Spaces (requires HF_TOKEN); None locally."""
    if not is_space():
        return None
    try:
        from fastrtc.credentials import get_cloudflare_turn_credentials

        return get_cloudflare_turn_credentials(hf_token=os.environ.get("HF_TOKEN"))
    except Exception as exc:
        print(f"[turn] falling back to default ICE servers: {exc}")
        return None


mode_radio = gr.Radio(
    ["enhanced", "raw"],
    value="enhanced",
    label="Quail speech enhancement",
    info="enhanced: clean the mic audio with ai-coustics Quail before transcription.",
)
transcript = gr.Textbox(label="Transcript", lines=10, placeholder="Press and hold the mic button, speak, then release.")

stream = Stream(
    ReplyOnPause(process),
    modality="audio",
    mode="send-receive",
    additional_inputs=[mode_radio, transcript],
    additional_outputs=[transcript],
    additional_outputs_handler=lambda _webrtc_id, outputs: outputs,
    rtc_configuration=rtc_configuration,
    concurrency_limit=5 if is_space() else None,
    time_limit=90 if is_space() else None,
    ui_args={
        "title": "Clean Transcribe",
        "subtitle": "Real-time speech-to-text: ai-coustics Quail enhancement + Whisper via HF Inference Providers",
        "icon_button_color": "var(--color-green)",
        "pulse_color": "var(--color-green)",
    },
)

if __name__ == "__main__":
    stream.ui.launch()