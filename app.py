"""Purr: real-time streaming speech-to-text on the HF Stack.

Pipeline:  mic -> FastRTC (WebRTC) -> optional Quail enhancement -> local
streaming ASR -> live transcript.

The transcript streams while you speak: incoming audio is enhanced block by
block, buffered into the current utterance, and re-decoded every fraction of a
second, so partial text appears immediately instead of after a pause. A run of
silence commits the utterance and starts a new line.

Deploys as a Gradio Space. Requires one secret:
    AIC_SDK_LICENSE   ai-coustics SDK license key (https://developers.ai-coustics.com)

ASR runs locally on CPU (faster-whisper by default, Moonshine optional), so no
Inference Providers token or hosted credits are needed.
"""

from __future__ import annotations

import os
import threading
import time
import warnings
from typing import TYPE_CHECKING, Any, Protocol

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# ZeroGPU Spaces require `import spaces` before any CUDA-touching import.
# The package ships with the Space base image on every hardware tier; locally
# it may be absent, which is harmless (`@spaces.GPU` is a no-op off ZeroGPU).
try:
    import spaces  # ty: ignore[unresolved-import]
except ImportError:  # local dev
    spaces = None

import numpy as np
from dotenv import load_dotenv

import gradio as gr
from gradio.utils import get_space

import librosa
from fastrtc import AdditionalOutputs, Stream, StreamHandler, WebRTC

if TYPE_CHECKING:
    import aic_sdk as aic
    import numpy.typing as npt
    from faster_whisper import WhisperModel
    from fastrtc.tracks import EmitType

# Gradio 5.50 warns that the `gr.Blocks(css=..., head=...)` parameters will move
# to `Blocks.launch(...)` in Gradio 6, but Gradio 6 is unreachable while fastrtc
# caps `gradio<6`, and the replacement does not exist yet. Silence the
# known-benign warnings for our Blocks and for FastRTC's discarded default UI,
# which also passes `css`.
warnings.filterwarnings(
    "ignore",
    message=r"The '(css|js|head)' parameter in the Blocks constructor will be removed",
    category=DeprecationWarning,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

QUAIL_MODEL_ID = "quail-ms-l-16khz"  # Quail Multi-Speaker: tuned for STT accuracy
MODELS_DIR = "models"
AIC_SAMPLE_RATE = 16000  # Quail native sample rate; input frames are resampled here
OUTPUT_SAMPLE_RATE = 24000  # FastRTC output sample rate (multiple of 50)

# Enhancement modes, shared by the streaming handler and the UI radio.
MODE_ENHANCED = "enhanced"
MODE_RAW = "raw"
MODE_CHOICES = (MODE_ENHANCED, MODE_RAW)

# ASR backend. Both run locally on CPU; no hosted inference is involved.
#   engine "faster-whisper" -> multilingual Whisper (PURR_ASR_MODEL, default base)
#   engine "moonshine"      -> English-only, lower latency (PURR_MOONSHINE_MODEL)
ASR_ENGINE = os.environ.get("PURR_ASR_ENGINE", "faster-whisper").strip().lower()
ASR_MODEL = os.environ.get("PURR_ASR_MODEL", "base")
MOONSHINE_MODEL = os.environ.get("PURR_MOONSHINE_MODEL", "moonshine/base")

# Streaming tunables.
DECODE_INTERVAL_S = 0.9  # minimum gap between partial decodes
MIN_DECODE_AUDIO_S = 0.5  # minimum new audio before a partial decode
PARTIAL_WINDOW_S = 8.0  # recent audio decoded for a live partial (commit uses all)
COMMIT_SILENCE_S = 0.9  # silence that commits the current utterance
MAX_UTTERANCE_S = 20.0  # force-commit a very long utterance, bounding decode cost
PRE_ROLL_S = 0.4  # pre-speech audio kept for ASR context
SPEECH_RMS = 0.008  # per-chunk RMS treated as speech
EMIT_IDLE_SLEEP_S = 0.02  # back-off when there is nothing to emit


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
_quail_model: dict[str, Any] = {}


def _load_aic_model(model_id: str) -> aic.Model:
    """Download (once) and load an ai-coustics model file."""
    import aic_sdk as aic

    model_path = aic.Model.download(model_id, MODELS_DIR)
    return aic.Model.from_file(model_path)


def load_quail_model() -> tuple[aic.Model, str]:
    """Download (once) and cache the Quail model plus its license key.

    The processor is built per connection (see ``QuailStream``) because its
    enhancement state is stateful; only the model download is shared.
    """
    with _init_lock:
        if "model" not in _quail_model:
            license_key = os.environ.get("AIC_SDK_LICENSE")
            if not license_key:
                raise RuntimeError("AIC_SDK_LICENSE is not set.")
            _quail_model["model"] = _load_aic_model(QUAIL_MODEL_ID)
            _quail_model["license"] = license_key
        return _quail_model["model"], _quail_model["license"]


class QuailStream:
    """Per-connection streaming Quail enhancement.

    Audio is re-blocked to the model's block size and processed continuously,
    with one processor state across the whole session. A process-wide lock
    serializes the native ``process`` calls so concurrent sessions never touch
    the SDK at the same time.
    """

    def __init__(self, sample_rate: int = AIC_SAMPLE_RATE) -> None:
        import aic_sdk as aic

        model, license_key = load_quail_model()
        self._processor = aic.Processor(model, license_key)
        config = aic.ProcessorConfig.optimal(model, sample_rate=sample_rate)
        self._processor.initialize(config)
        self._block = int(config.block_size)
        self._residual = np.zeros(0, dtype=np.float32)

    def process(self, mono: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
        """Enhance a mono float32 buffer, returning however many full blocks complete."""
        if mono.size == 0:
            return np.zeros(0, dtype=np.float32)
        buf = np.concatenate([self._residual, mono.astype(np.float32)])
        full = (len(buf) // self._block) * self._block
        if full == 0:
            self._residual = buf
            return np.zeros(0, dtype=np.float32)
        with _proc_lock:
            out = [
                self._processor.process(np.ascontiguousarray(buf[i : i + self._block]))
                for i in range(0, full, self._block)
            ]
        self._residual = buf[full:]
        return np.concatenate(out)


# ---------------------------------------------------------------------------
# ASR backends (local, CPU)
# ---------------------------------------------------------------------------


class Transcriber(Protocol):
    def prepare(self) -> None: ...

    def transcribe(
        self, audio: npt.NDArray[np.float32], language: str | None
    ) -> tuple[str, str | None]: ...


class FasterWhisperTranscriber:
    """Multilingual Whisper via faster-whisper (CTranslate2, int8 CPU)."""

    def prepare(self) -> None:
        _load_faster_whisper()

    def transcribe(
        self, audio: npt.NDArray[np.float32], language: str | None
    ) -> tuple[str, str | None]:
        segments, info = _load_faster_whisper().transcribe(
            audio,
            language=language,
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        return "".join(segment.text for segment in segments).strip(), (
            info.language or language
        )


class MoonshineTranscriber:
    """English-only Moonshine via the ONNX runtime bundled with FastRTC."""

    def prepare(self) -> None:
        from fastrtc import get_stt_model

        with _asr_lock:
            get_stt_model(MOONSHINE_MODEL)

    def transcribe(
        self, audio: npt.NDArray[np.float32], language: str | None
    ) -> tuple[str, str | None]:
        from fastrtc import get_stt_model

        model = get_stt_model(MOONSHINE_MODEL)
        text = model.stt((AIC_SAMPLE_RATE, audio))
        return (text or "").strip(), "en"


_asr_lock = threading.Lock()
_asr: dict[str, Any] = {}
_asr_ready = threading.Event()
_asr_error: dict[str, str] = {}
_faster_whisper: dict[str, Any] = {}


def _load_faster_whisper() -> WhisperModel:
    with _asr_lock:
        if "error" in _faster_whisper:
            # A failed load stays failed: do not re-download on every decode.
            raise RuntimeError(_faster_whisper["error"])
        if "model" not in _faster_whisper:
            from faster_whisper import WhisperModel

            try:
                _faster_whisper["model"] = WhisperModel(
                    ASR_MODEL, device="cpu", compute_type="int8"
                )
            except Exception as exc:
                _faster_whisper["error"] = str(exc)
                raise
        return _faster_whisper["model"]


def get_transcriber() -> Transcriber:
    """Resolve (once) and cache the configured ASR backend."""
    with _asr_lock:
        if "impl" not in _asr:
            _asr["impl"] = (
                MoonshineTranscriber()
                if ASR_ENGINE == "moonshine"
                else FasterWhisperTranscriber()
            )
        return _asr["impl"]


_transcribe_lock = threading.Lock()


def warm_asr() -> None:
    """Best-effort: load the ASR model off the request path.

    The checkpoint is loaded lazily inside ``transcribe``; loading it here means
    the first utterance does not block on the download, and a load failure is
    recorded once (later decodes short-circuit on it instead of retrying).
    """
    try:
        get_transcriber().prepare()
    except Exception as exc:  # pragma: no cover - depends on the environment
        _asr_error["message"] = str(exc)
        print(f"[asr] unavailable: {exc}")
    finally:
        _asr_ready.set()


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------


def to_mono_float32(audio: tuple[int, npt.NDArray[Any]]) -> tuple[int, npt.NDArray[np.float32]]:
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


def resample(x: npt.NDArray[np.float32], orig_sr: int, target_sr: int) -> npt.NDArray[np.float32]:
    if x is None or len(x) == 0 or orig_sr == target_sr:
        return x
    return librosa.resample(x, orig_sr=orig_sr, target_sr=target_sr, res_type="soxr_hq")


def rms(x: npt.NDArray[np.float32]) -> float:
    return float(np.sqrt(np.mean(np.square(x)))) if x.size else 0.0


def append_transcript_line(transcript: str, line: str) -> str:
    """Append ``line`` to the accumulated transcript, newline-separated."""
    return f"{transcript}\n{line}" if transcript else line


# ---------------------------------------------------------------------------
# Stream handler: continuous streaming transcription
# ---------------------------------------------------------------------------


class StreamingTranscriber(StreamHandler):
    """Transcribe continuously while the user speaks.

    ``receive`` optionally enhances each incoming 16 kHz mono frame with a
    per-connection Quail processor, appends it to the current utterance, and
    tracks trailing silence. ``emit`` re-decodes the growing utterance on a
    timer, emits the live transcript, and commits it after a run of silence or
    once the utterance grows too long.
    """

    def __init__(self, asr: Transcriber | None = None) -> None:
        super().__init__(
            expected_layout="mono",
            output_sample_rate=OUTPUT_SAMPLE_RATE,
            input_sample_rate=AIC_SAMPLE_RATE,
        )
        self._asr = asr  # injected for tests; otherwise the shared backend is used
        self._lock = threading.Lock()
        self._utt = np.zeros(0, dtype=np.float32)  # current utterance (possibly enhanced)
        self._utt_has_speech = False
        self._committed = ""  # committed transcript across utterances
        self._partial = ""  # live hypothesis for the current utterance
        self._language: str | None = None
        self._silence_s = 0.0
        self._last_decode_len = 0
        self._last_decode_t = 0.0
        self._last_emit = ""
        self._enhancer: QuailStream | None = None
        self._enhance_error: str | None = None
        self._decode_error: str | None = None
        self._noted_errors: set[str] = set()

    def copy(self) -> StreamingTranscriber:
        return StreamingTranscriber(asr=self._asr)

    def start_up(self) -> None:
        threading.Thread(target=warm_asr, daemon=True).start()

    # -- helpers ------------------------------------------------------------

    def _mode(self) -> str:
        """Read the enhancement mode from the FastRTC additional inputs."""
        for arg in reversed(self.latest_args):
            if isinstance(arg, str) and arg in MODE_CHOICES:
                return arg
        return MODE_ENHANCED

    def _enhance(self, mono: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
        if self._mode() != MODE_ENHANCED:
            return mono
        if self._enhancer is None and self._enhance_error is None:
            try:
                self._enhancer = QuailStream()
            except Exception as exc:
                self._enhance_error = str(exc)
                print(f"[quail] enhancement unavailable: {exc}")
        if self._enhancer is None:
            return mono
        try:
            return self._enhancer.process(mono)
        except Exception as exc:
            self._enhance_error = str(exc)
            self._enhancer = None
            print(f"[quail] enhancement failed mid-stream: {exc}")
            return mono

    def _full_text(self) -> str:
        if self._partial:
            return append_transcript_line(self._committed, self._partial)
        return self._committed

    def _note_error_once(self) -> str | None:
        reason = (
            self._decode_error
            or self._enhance_error
            or _asr_error.get("message")
        )
        if reason is None or reason in self._noted_errors:
            return None
        self._noted_errors.add(reason)
        return f"[unavailable: {reason}]"

    def _transcribe(
        self, audio: npt.NDArray[np.float32], language: str | None
    ) -> tuple[str, str | None]:
        if self._asr is None:
            message = _asr_error.get("message")
            if message is not None:
                raise RuntimeError(message)
            self._asr = get_transcriber()
        with _transcribe_lock:
            return self._asr.transcribe(audio, language)

    # -- StreamHandler interface -------------------------------------------

    def receive(self, frame: tuple[int, npt.NDArray[np.int16]]) -> None:
        sr, arr = to_mono_float32(frame)
        if sr != AIC_SAMPLE_RATE:
            arr = resample(arr, sr, AIC_SAMPLE_RATE)
        enhanced = self._enhance(arr)
        if enhanced.size == 0:
            return
        speech = rms(enhanced) >= SPEECH_RMS
        with self._lock:
            self._utt = np.concatenate([self._utt, enhanced])
            if speech:
                self._utt_has_speech = True
            elif not self._utt_has_speech:
                # Bound pre-speech silence to a short pre-roll for ASR context.
                keep = int(PRE_ROLL_S * AIC_SAMPLE_RATE)
                if len(self._utt) > keep:
                    self._utt = self._utt[-keep:]
                    self._last_decode_len = min(self._last_decode_len, len(self._utt))
            # Bound the utterance even before the ASR is ready, so a slow model
            # load cannot grow the buffer without limit.
            cap = int(MAX_UTTERANCE_S * AIC_SAMPLE_RATE)
            if len(self._utt) > cap:
                self._utt = self._utt[-cap:]
                self._last_decode_len = min(self._last_decode_len, len(self._utt))
            self._silence_s = 0.0 if speech else self._silence_s + len(enhanced) / AIC_SAMPLE_RATE

    def emit(self) -> EmitType:
        if self._asr is None and not _asr_ready.is_set():
            time.sleep(EMIT_IDLE_SLEEP_S)
            return None

        now = time.monotonic()
        with self._lock:
            utt = self._utt
            new_audio = len(utt) - self._last_decode_len
            too_long = len(utt) >= int(MAX_UTTERANCE_S * AIC_SAMPLE_RATE)
            commit = self._silence_s >= COMMIT_SILENCE_S and self._utt_has_speech
            due = (
                self._utt_has_speech
                and new_audio >= int(MIN_DECODE_AUDIO_S * AIC_SAMPLE_RATE)
                and (now - self._last_decode_t) >= DECODE_INTERVAL_S
            )
            if not (commit or due or (too_long and self._utt_has_speech)):
                snapshot: tuple[npt.NDArray[np.float32], str | None, bool] | None = None
            else:
                should_commit = commit or too_long
                # A partial only needs recent audio; the commit decode covers the
                # whole utterance so no words are lost.
                window = (
                    utt
                    if should_commit
                    else utt[-int(PARTIAL_WINDOW_S * AIC_SAMPLE_RATE) :]
                )
                snapshot = (window.copy(), self._language, should_commit)

        if snapshot is None:
            time.sleep(EMIT_IDLE_SLEEP_S)
            return None

        audio, language, should_commit = snapshot
        failed = False
        try:
            text, detected = self._transcribe(audio, language)
        except Exception as exc:
            # A decode failure must not disturb the transcript: keep the live
            # partial and the buffered utterance, and surface the error once.
            print(f"[asr] transcription failed: {exc}")
            self._decode_error = str(exc)
            failed = True
            text, detected = "", language

        now = time.monotonic()
        with self._lock:
            # Throttle retries after a failure too.
            self._last_decode_t = now
            if not failed:
                if should_commit:
                    if text:
                        self._committed = append_transcript_line(self._committed, text)
                    tail = self._utt[len(audio) :]
                    self._utt = tail.copy()
                    self._last_decode_len = 0
                    self._partial = ""
                    self._language = None
                    self._silence_s = 0.0
                    self._utt_has_speech = tail.size > 0 and rms(tail) >= SPEECH_RMS
                else:
                    self._partial = text
                    self._last_decode_len = len(self._utt)
                    if language is None and detected:
                        self._language = detected
            note = self._note_error_once()
            full = self._full_text()
            if note:
                full = append_transcript_line(full, note)
            if full == self._last_emit:
                return None
            self._last_emit = full
            return AdditionalOutputs(full)


# ---------------------------------------------------------------------------
# Gradio UI + Stream
# ---------------------------------------------------------------------------


def rtc_configuration() -> dict[str, Any]:
    """ICE servers for WebRTC: public STUN, plus Cloudflare TURN when configured.

    Must never return ``None``: FastRTC substitutes no default when this callable
    returns ``None``, which leaves the browser with zero ICE servers and a
    WebRTC connection stuck at "connecting".

    ``HF_TOKEN`` is deliberately not used here. fastrtc's ``hf_token`` path posts
    the token to ``turn.fastrtc.org``, which is dead upstream, and that dangling
    DNS delegation is a takeover risk for the token.
    """
    turn_key_id = os.environ.get("CLOUDFLARE_TURN_KEY_ID")
    turn_key_api_token = os.environ.get("CLOUDFLARE_TURN_KEY_API_TOKEN")
    if is_space() and turn_key_id and turn_key_api_token:
        try:
            from fastrtc.credentials import get_cloudflare_turn_credentials

            # hf_token="" forces the Cloudflare-keys branch; the default reads
            # HF_TOKEN and would hit the dead fastrtc endpoint instead.
            return get_cloudflare_turn_credentials(
                turn_key_id=turn_key_id,
                turn_key_api_token=turn_key_api_token,
                hf_token="",
            )
        except Exception as exc:
            print(f"[turn] Cloudflare TURN unavailable, using STUN only: {exc}")
    return {
        "iceServers": [
            {"urls": "stun:stun.l.google.com:19302"},
            {"urls": "stun:stun.cloudflare.com:3478"},
        ]
    }


def _replace_transcript(_current: str, new_transcript: str) -> str:
    """Replace the accumulated transcript with the newest value from the stream."""
    return new_transcript


mode_radio = gr.Radio(
    list(MODE_CHOICES),
    value=MODE_ENHANCED,
    label="Quail speech enhancement",
    info="enhanced: clean the mic audio with ai-coustics Quail before transcription.",
)
transcript = gr.Textbox(
    label="Transcript",
    placeholder="Tap the mic and start speaking — text appears as you talk.",
    interactive=False,
    autoscroll=True,
    lines=14,
    max_lines=14,
    elem_classes=["transcript-box"],
)

# The handler owns streaming and config; the UI is built by hand below, so no
# additional components are attached to this Stream (its generated layout is
# discarded in favor of `build_ui`).
stream = Stream(
    StreamingTranscriber(),
    modality="audio",
    mode="send-receive",
    rtc_configuration=rtc_configuration,
    concurrency_limit=5 if is_space() else None,
    time_limit=90 if is_space() else None,
)

# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
# FastRTC's generated layout buries the primary output between two sidebars.
# Build the Gradio Blocks by hand instead: a full-height transcript pane owns the
# page, with the record control, the microphone-source picker, and the
# enhancement setting grouped in a rail beside it.
APP_CSS = """
.gradio-container { max-width: 1200px !important; margin: 0 auto !important; }
.purr-header { text-align: center; margin: 0.25rem 0 1rem; }
.purr-header h1 {
    margin: 0;
    font-size: 3.25rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    line-height: 1.1;
}
.purr-header p {
    margin: 0.5rem 0 0;
    font-size: 1.05rem;
    color: var(--body-text-color-subdued);
}
/* Two-region shell: the transcript pane owns the space, the rail holds controls. */
.purr-layout {
    gap: 1.25rem;
    align-items: stretch !important;
}
.transcript-pane {
    display: flex;
    flex-direction: column;
    min-height: 0;
}
/* Gradio auto-wraps each component in a form whose scale sets flex-grow: 0.
   Include it in the height chain and clear its chrome so the textarea fills. */
.transcript-pane > .form {
    flex: 1 1 auto;
    min-height: 0;
    display: flex;
    flex-direction: column;
    overflow: visible;
    background: transparent;
    border: none;
    box-shadow: none;
}
.control-rail {
    justify-content: flex-start;
    gap: 0.75rem;
    padding-left: 1.25rem;
    border-left: 1px solid var(--border-color-primary);
}
/* The transcript fills the pane; the textarea scrolls internally, the page does not. */
.transcript-box {
    flex: 1 1 auto;
    min-height: 0;
    display: flex;
    flex-direction: column;
}
.transcript-box label,
.transcript-box .input-container {
    flex: 1 1 auto;
    min-height: 0;
    display: flex;
    flex-direction: column;
}
.transcript-box .input-container {
    align-items: stretch;
}
.transcript-box textarea {
    flex: 1 1 auto;
    height: 100% !important;
    min-height: 0 !important;
    font-size: 1.6rem !important;
    line-height: 1.55 !important;
    font-weight: 500 !important;
    color: var(--body-text-color) !important;
    -webkit-text-fill-color: var(--body-text-color) !important;
    opacity: 1 !important;
}
/* FastRTC's own source control is hidden: a trigger button plus a dropdown that
   only mounts while open. The rail's purr-source control replaces it. Scoped to
   the WebRTC component's class, pinned to fastrtc==0.0.34. */
.purr-webrtc button[aria-label="select input source"],
.purr-webrtc .select-wrap {
    display: none !important;
}
/* Microphone-source control. Static markup, filled by the page script. */
.purr-source-wrap {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    margin-top: 0.75rem;
}
.purr-source-wrap label {
    font-size: var(--block-label-text-size);
    font-weight: var(--block-label-text-weight);
    color: var(--block-label-text-color);
}
.purr-source-wrap select {
    width: 100%;
    padding: 0.3rem 0.5rem;
    border: 1px solid var(--input-border-color);
    border-radius: var(--input-radius);
    background: var(--input-background-fill);
    color: var(--body-text-color);
    font-size: var(--input-text-size);
}
/* Decorative orange wave background. Pure CSS, non-interactive, and separate
   from the mic control so it can never render as a static circle. */
.purr-wave {
    position: fixed;
    inset: 0;
    z-index: 0;
    overflow: hidden;
    pointer-events: none;
}
.purr-wave::before {
    content: "";
    position: absolute;
    inset: 0;
    background: radial-gradient(55% 45% at 50% 0%, rgba(249, 115, 22, 0.12), transparent 70%);
}
.purr-wave span {
    position: absolute;
    top: 50%;
    left: 50%;
    width: 42vmax;
    height: 42vmax;
    margin: -21vmax 0 0 -21vmax;
    border-radius: 50%;
    background: radial-gradient(circle, rgba(249, 115, 22, 0.22), rgba(249, 115, 22, 0) 62%);
    opacity: 0;
    animation: purr-wave 9s ease-out infinite;
}
.purr-wave span:nth-child(2) { animation-delay: 3s; }
.purr-wave span:nth-child(3) { animation-delay: 6s; }
@keyframes purr-wave {
    0% { transform: scale(0.3); opacity: 0; }
    18% { opacity: 1; }
    100% { transform: scale(1.75); opacity: 0; }
}
@media (prefers-reduced-motion: reduce) {
    .purr-wave span { animation: none; opacity: 0; }
}
/* Narrow screens: the rail reflows below the transcript and the transcript is
   height-capped so the record control stays reachable. Page scroll is fine here. */
@media (max-width: 768px) {
    .purr-layout { flex-direction: column !important; flex-wrap: nowrap !important; }
    .purr-layout > .transcript-pane,
    .purr-layout > .control-rail { width: 100% !important; flex: none !important; }
    .transcript-pane { min-height: 0; }
    .transcript-pane > .form { flex: none; }
    .transcript-box,
    .transcript-box label,
    .transcript-box .input-container { flex: none; }
    .transcript-box textarea { height: 58vh !important; }
    .control-rail {
        padding-left: 0;
        padding-top: 1rem;
        border-left: none;
        border-top: 1px solid var(--border-color-primary);
    }
}
.purr-header,
.purr-layout {
    position: relative;
    z-index: 1;
}
"""


# Browser-side microphone-source control. Gradio and FastRTC expose no Python
# device API, so the control is static markup filled by this page script, and the
# chosen device is bound to the next recording by wrapping getUserMedia. Pinned to
# fastrtc==0.0.34: FastRTC acquires the mic per recording start from
# `navigator.mediaDevices.getUserMedia` and never re-applies constraints.
SOURCE_SCRIPT = """
<script>
(function () {
    var HINT = "<option value=''>Grant microphone access to choose a device</option>";
    var DEFAULT_OPTION = "<option value=''>Browser default</option>";
    var chosen = "";
    var unlocked = false;
    var select = null;

    function render(devices) {
        if (!select) return;
        var mics = devices.filter(function (d) { return d.kind === "audioinput"; });
        var labelled = mics.some(function (d) { return d.label; });
        if (!mics.length || !labelled) {
            select.disabled = true;
            select.innerHTML = HINT;
            return;
        }
        var prior = chosen;
        select.innerHTML = DEFAULT_OPTION;
        mics.forEach(function (d, i) {
            var option = document.createElement("option");
            option.value = d.deviceId;
            option.textContent = d.label || ("Microphone " + (i + 1));
            select.appendChild(option);
        });
        select.disabled = false;
        select.value = prior && mics.some(function (d) { return d.deviceId === prior; }) ? prior : "";
    }

    function refresh() {
        if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
        navigator.mediaDevices.enumerateDevices().then(render).catch(function () {});
    }

    function maybeUnlock() {
        if (!unlocked) { unlocked = true; refresh(); }
    }

    function bind() {
        if (!select || select.__purrBound) return;
        select.__purrBound = true;
        select.addEventListener("change", function () { chosen = select.value; });
        refresh();
    }

    function attach(attempt) {
        select = document.getElementById("purr-source");
        if (select) { bind(); return; }
        if (attempt < 400) setTimeout(function () { attach(attempt + 1); }, 250);
    }

    var media = navigator.mediaDevices;
    var realGUM = media && media.getUserMedia;
    if (realGUM && !window.__purrSourceHook) {
        window.__purrSourceHook = true;
        media.getUserMedia = function (constraints) {
            var audioOnly = constraints && constraints.audio && !constraints.video;
            if (!chosen || !audioOnly) {
                return realGUM.call(media, constraints).then(function (stream) {
                    maybeUnlock();
                    return stream;
                });
            }
            var base = typeof constraints.audio === "object" ? constraints.audio : {};
            var pinned = Object.assign({}, constraints, {
                audio: Object.assign({}, base, { deviceId: { exact: chosen } })
            });
            return realGUM.call(media, pinned).then(function (stream) {
                maybeUnlock();
                return stream;
            }).catch(function (error) {
                if (!error || (error.name !== "OverconstrainedError" && error.name !== "NotFoundError")) {
                    throw error;
                }
                chosen = "";
                if (select) select.value = "";
                var fallback = Object.assign({}, constraints, { audio: Object.assign({}, base) });
                delete fallback.audio.deviceId;
                return realGUM.call(media, fallback);
            });
        };
        if (media.addEventListener) media.addEventListener("devicechange", refresh);
    }
    attach(0);
    refresh();
})();
</script>
"""


def build_ui() -> gr.Blocks:
    """Assemble the app UI: a control rail beside a full-height transcript pane."""
    with gr.Blocks(css=APP_CSS, head=SOURCE_SCRIPT, title="PURR", fill_height=True) as demo:
        gr.HTML(
            "<div class='purr-wave'><span></span><span></span><span></span></div>",
            container=False,
        )
        gr.HTML(
            "<div class='purr-header'>"
            "<h1>PURR</h1>"
            "<p>Streaming speech-to-text &middot; ai-coustics Quail enhancement "
            "&middot; local Whisper/Moonshine</p>"
            "</div>"
        )
        with gr.Row(scale=1, equal_height=False, elem_classes=["purr-layout"]):
            with gr.Column(scale=4, elem_classes=["transcript-pane"]):
                transcript.render()
            with gr.Column(scale=1, min_width=220, elem_classes=["control-rail"]):
                webrtc = WebRTC(
                    rtc_configuration=rtc_configuration,
                    mode="send-receive",
                    modality="audio",
                    icon_button_color="var(--color-orange)",
                    pulse_color="var(--color-orange)",
                    full_screen=False,
                    show_label=False,
                    elem_classes=["purr-webrtc"],
                )
                mode_radio.render()
                gr.HTML(
                    "<div class='purr-source-wrap'>"
                    "<label for='purr-source'>Microphone source</label>"
                    "<select id='purr-source' aria-label='Microphone source' disabled>"
                    "<option value=''>Grant microphone access to choose a device</option>"
                    "</select>"
                    "</div>",
                    container=False,
                )

        webrtc.stream(
            fn=stream.event_handler,
            inputs=[webrtc, mode_radio],
            outputs=[webrtc],
            time_limit=stream.time_limit,
            concurrency_limit=stream.concurrency_limit,
        )
        webrtc.on_additional_outputs(
            _replace_transcript,
            inputs=[transcript],
            outputs=[transcript],
            concurrency_limit=stream.concurrency_limit_gradio,
        )

        # ZeroGPU startup probe (Space-only). Free-tier Gradio Spaces run on
        # ZeroGPU, whose startup scan requires at least one Gradio event handler
        # bound to a @spaces.GPU function. This app is CPU-only, so the probe is
        # a no-op that is never invoked; it only satisfies the scan.
        if spaces is not None:

            @spaces.GPU
            def _zerogpu_startup_probe() -> None:
                return None

            probe = gr.Button(visible=False)
            probe.click(fn=_zerogpu_startup_probe, inputs=None, outputs=None)
    return demo


stream.ui = build_ui()


if __name__ == "__main__":
    # Local runs read .env (gitignored); on the Space it is absent and this is a
    # no-op, so the injected secrets remain the source of truth.
    load_dotenv()
    stream.ui.launch()
