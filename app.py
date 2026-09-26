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
import time
import wave
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# ZeroGPU Spaces require `import spaces` before any CUDA-touching import.
# The package ships with the Space base image on every hardware tier; locally
# it may be absent, which is harmless (`@spaces.GPU` is a no-op off ZeroGPU).
try:
    import spaces  # ty: ignore[unresolved-import]
except ImportError:  # local dev
    spaces = None

import numpy as np

import gradio as gr
from gradio.utils import get_space

import librosa
from fastrtc import AdditionalOutputs, ReplyOnPause, Stream

if TYPE_CHECKING:
    import aic_sdk as aic
    from fastrtc.tracks import EmitType
    from fastrtc.utils import AudioChunk
    from huggingface_hub import InferenceClient

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

QUAIL_MODEL_ID = "quail-ms-l-16khz"  # Quail Multi-Speaker: tuned for STT accuracy
MODELS_DIR = "models"
AIC_SAMPLE_RATE = 16000  # Quail native sample rate
OUTPUT_SAMPLE_RATE = 24000  # FastRTC output sample rate (multiple of 50)
WHISPER_MODEL = "openai/whisper-large-v3"
VAD_MODEL_ID = "vad-2.1-xxs-16khz"  # ai-coustics dedicated VAD model
VAD_SPEECH_THRESHOLD = 0.5  # raw per-block probability treated as speech
SILENCE_GAP_S = 1.5  # no-input gap that marks the end of a silent press
NO_SPEECH_LINE = "[no speech detected]"
NO_WORDS_LINE = "[speech detected, but no words recognized]"
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
_quail: dict[str, Any] = {}
_client_cache: list[InferenceClient] = []


def get_hf_client() -> InferenceClient:
    if not _client_cache:
        with _init_lock:
            if not _client_cache:
                from huggingface_hub import InferenceClient

                _client_cache.append(InferenceClient(provider="auto"))
    return _client_cache[0]


def _load_aic_model(model_id: str) -> aic.Model:
    """Download (once) and load an ai-coustics model file."""
    import aic_sdk as aic

    model_path = aic.Model.download(model_id, MODELS_DIR)
    return aic.Model.from_file(model_path)


def load_quail() -> tuple[aic.Processor, aic.ProcessorConfig]:
    """Download (once) and initialize the Quail enhancement processor."""
    with _init_lock:
        if "processor" in _quail:
            return _quail["processor"], _quail["config"]
        import aic_sdk as aic

        license_key = os.environ.get("AIC_SDK_LICENSE")
        if not license_key:
            raise RuntimeError("AIC_SDK_LICENSE is not set.")
        model = _load_aic_model(QUAIL_MODEL_ID)
        processor = aic.Processor(model, license_key)
        config = aic.ProcessorConfig.optimal(model, sample_rate=AIC_SAMPLE_RATE)
        processor.initialize(config)
        _quail["processor"] = processor
        _quail["config"] = config
        return processor, config


# ---------------------------------------------------------------------------
# Pause detection (ai-coustics VAD)
# ---------------------------------------------------------------------------

_vad_model: dict[str, Any] = {}
_vad_lock = threading.Lock()


def load_vad_model() -> aic.Model:
    """Download (once) and load the ai-coustics VAD model (no license needed)."""
    with _vad_lock:
        if "model" in _vad_model:
            return _vad_model["model"]
        model = _load_aic_model(VAD_MODEL_ID)
        _vad_model["model"] = model
        return model


class AICVadAdapter:
    """FastRTC pause detector backed by ai-coustics' voice activity detector.

    One instance per WebRTC connection; the downloaded model is cached
    process-wide. Each FastRTC window is treated independently: the detector
    context is reset, the window is resampled to the model's native rate, and
    speech-seconds are derived from the raw per-block probability (the
    post-processed ``is_speech_detected`` flag carries hold hysteresis that
    would mask pauses).
    """

    def __init__(self) -> None:
        self._vad = None
        self._config = None
        self._lock = threading.Lock()
        self.windows = 0  # completed VAD window evaluations
        self.unavailable: str | None = None  # first detector failure, if any

    def _ensure(self) -> tuple[aic.Vad, aic.ProcessorConfig]:
        if self.unavailable is not None:
            raise RuntimeError(self.unavailable)
        if self._vad is not None and self._config is not None:
            return self._vad, self._config
        with self._lock:
            if self._vad is None:
                try:
                    import aic_sdk as aic

                    license_key = os.environ.get("AIC_SDK_LICENSE")
                    if not license_key:
                        raise RuntimeError("AIC_SDK_LICENSE is not set.")
                    model = load_vad_model()
                    config = aic.ProcessorConfig.optimal(model)
                    self._vad = aic.Vad(model, license_key, config)
                    self._config = config
                except Exception as exc:
                    self.unavailable = str(exc)
                    raise
        assert self._vad is not None and self._config is not None
        return self._vad, self._config

    def warmup(self) -> None:
        """Best-effort: build the detector before the first window."""
        try:
            self._ensure()
        except Exception:
            pass

    def vad(self, audio: tuple[int, np.ndarray], options: object | None = None) -> tuple[float, list[AudioChunk]]:
        """Return ``(speech_seconds, [])`` for one FastRTC audio window."""
        try:
            sr, arr = audio
            vad, config = self._ensure()
            block_size = int(config.block_size)
            sample_rate = int(config.sample_rate)
            mono = to_mono_float32((sr, arr))[1]
            mono = resample(mono, sr, sample_rate)
            ctx = vad.get_context()
            ctx.reset()
            speech_blocks = 0
            for block in iter_blocks(mono, block_size):
                vad.process(block)
                if float(ctx.raw_vad_probability()) >= VAD_SPEECH_THRESHOLD:
                    speech_blocks += 1
            self.windows += 1
            return speech_blocks * block_size / sample_rate, []
        except Exception as exc:
            # Fail closed (no turn starts, no audio accumulates) and record the
            # failure once so later windows do not retry or re-log it.
            if self.unavailable is None:
                self.unavailable = str(exc)
                print(f"[vad] detector unavailable: {exc}")
            return 0.0, []


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


def iter_blocks(x: np.ndarray, block_size: int) -> Iterator[np.ndarray]:
    """Yield float32 blocks of ``block_size``, zero-padding the final block."""
    for i in range(0, len(x), block_size):
        block = np.ascontiguousarray(x[i : i + block_size], dtype=np.float32)
        if len(block) < block_size:
            block = np.pad(block, (0, block_size - len(block)))
        yield block


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
        out = [processor.process(block) for block in iter_blocks(y16, block_size)]
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


def playback_chunks(y16: np.ndarray, chunk_s: float = 0.3) -> Iterator[tuple[int, np.ndarray]]:
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


def append_transcript_line(transcript: str, line: str) -> str:
    """Append ``line`` to the accumulated transcript, newline-separated."""
    return f"{transcript}\n{line}" if transcript else line


def process(audio: tuple[int, np.ndarray], mode: str | None, transcript: str) -> Iterator[tuple[int, np.ndarray] | AdditionalOutputs]:
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
        # A turn only fires once the pause detector has confirmed speech, so an
        # empty transcript here is a recognition miss, not silence.
        text = NO_WORDS_LINE

    yield AdditionalOutputs(append_transcript_line(transcript, f"{text}{note}"))


# ---------------------------------------------------------------------------
# Speechless-press handling
# ---------------------------------------------------------------------------


class SpeechAwarePauseHandler(ReplyOnPause):
    """ReplyOnPause that also reports a press ending with no speech.

    A turn only fires after speech is detected, so a silent hold-and-release
    would otherwise produce nothing. A per-connection no-input gap marks the
    release and emits a no-speech line instead.
    """

    def __init__(self, *args: Any, silence_gap_s: float = SILENCE_GAP_S, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._silence_gap_s = silence_gap_s
        self._last_audio_time = 0.0
        self._press_active = False
        self._no_speech_sent = False
        self._turn_fired = False
        self._unavailable_notified = False
        self._windows_at_press_start = 0

    def start_up(self) -> None:
        super().start_up()
        warmup = getattr(self.model, "warmup", None)
        if callable(warmup):
            warmup()

    def copy(self) -> SpeechAwarePauseHandler:
        return SpeechAwarePauseHandler(
            fn=self.fn,
            startup_fn=self.startup_fn,
            algo_options=self.algo_options,
            model_options=self.model_options,
            can_interrupt=self.can_interrupt,
            expected_layout=self.expected_layout,
            output_sample_rate=self.output_sample_rate,
            output_frame_size=self.output_frame_size,
            input_sample_rate=self.input_sample_rate,
            model=AICVadAdapter(),
            needs_args=self.needs_args,
            silence_gap_s=self._silence_gap_s,
        )

    def receive(self, frame: tuple[int, np.ndarray]) -> None:
        now = time.monotonic()
        if (now - self._last_audio_time) >= self._silence_gap_s:
            # A new press begins: clear state from the previous one. Cleared here,
            # not on turn completion, so a completed turn still suppresses the
            # no-speech line until the user actually starts a new press.
            self._no_speech_sent = False
            self._turn_fired = False
            self._unavailable_notified = False
            self._windows_at_press_start = getattr(self.model, "windows", 0)
        self._last_audio_time = now
        self._press_active = True
        super().receive(frame)

    def emit(self) -> EmitType:
        out = super().emit()
        if out is not None:
            self._turn_fired = True
            return out
        if not (
            self._press_active
            and not self._turn_fired
            and not self._no_speech_sent
            and not self.state.started_talking
            and not self.state.responding
            and self.generator is None
            and (time.monotonic() - self._last_audio_time) >= self._silence_gap_s
        ):
            return None
        self._no_speech_sent = True
        self._press_active = False
        transcript = self.latest_args[-1] if self.latest_args else ""
        if not isinstance(transcript, str):
            transcript = ""
        reason = getattr(self.model, "unavailable", None)
        if reason is not None:
            if self._unavailable_notified:
                return None
            self._unavailable_notified = True
            line = f"[turn detection unavailable: {reason}]"
        elif getattr(self.model, "windows", 0) <= self._windows_at_press_start:
            # Too short for the detector to have evaluated a window: stay silent.
            return None
        else:
            line = NO_SPEECH_LINE
        return AdditionalOutputs(append_transcript_line(transcript, line))


# ---------------------------------------------------------------------------
# Gradio UI + Stream
# ---------------------------------------------------------------------------


def rtc_configuration() -> dict[str, Any] | None:
    """TURN credentials on Spaces (requires HF_TOKEN); None locally.

    FastRTC accepts a ``None`` return at runtime for this callable, though its
    callable type does not model it.
    """
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
    SpeechAwarePauseHandler(process, model=AICVadAdapter()),
    modality="audio",
    mode="send-receive",
    additional_inputs=[mode_radio, transcript],
    additional_outputs=[transcript],
    additional_outputs_handler=lambda _webrtc_id, outputs: outputs,
    rtc_configuration=rtc_configuration,  # ty: ignore[invalid-argument-type]
    concurrency_limit=5 if is_space() else None,
    time_limit=90 if is_space() else None,
    ui_args={
        "title": "Clean Transcribe",
        "subtitle": "Real-time speech-to-text: ai-coustics Quail enhancement + Whisper via HF Inference Providers",
        "icon_button_color": "var(--color-green)",
        "pulse_color": "var(--color-green)",
    },
)

# ---------------------------------------------------------------------------
# ZeroGPU startup probe (Space-only)
# ---------------------------------------------------------------------------
# Free-tier Gradio Spaces run on ZeroGPU, whose startup scan requires at least
# one Gradio event handler bound to a @spaces.GPU function. This app is
# CPU-only (Quail runs on CPU, Whisper goes through HF Inference Providers),
# so the probe is a no-op that is never invoked; it only satisfies the scan.
if spaces is not None:

    @spaces.GPU
    def _zerogpu_startup_probe() -> None:
        return None

    with stream.ui:
        _zerogpu_probe = gr.Button(visible=False)
        _zerogpu_probe.click(fn=_zerogpu_startup_probe, inputs=None, outputs=None)


if __name__ == "__main__":
    stream.ui.launch()