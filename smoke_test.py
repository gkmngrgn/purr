import os, sys, subprocess, textwrap

RESULTS = []

def run(label: str, code: str) -> int:
    r = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True)
    status = "OK " if r.returncode == 0 else "FAIL"
    print(f"[{status}] {label}")
    if r.returncode != 0:
        print(r.stderr[-2000:])
    RESULTS.append(r.returncode)
    return r.returncode

run("stream+UI construction (offline, no launch, no models loaded)", """
import os
os.environ.pop('HF_TOKEN', None)
os.environ.pop('AIC_SDK_LICENSE', None)
import app
assert app.stream.ui is not None
assert app.mode_radio is not None
assert callable(app.rtc_configuration)
handler = app.stream.event_handler
assert isinstance(handler, app.StreamingTranscriber), type(handler).__name__
assert app._asr == {}, "ASR must not be built at import"
assert app._faster_whisper == {}, "Whisper must not be loaded at import"
assert app._quail_model == {}, "Quail must not be built at import"
c = handler.copy()
assert isinstance(c, app.StreamingTranscriber)
assert c is not handler
assert c._utt.size == 0 and c._committed == "", "each connection needs fresh state"
assert app.transcript.interactive is False, "transcript must be read-only"
cfg = app.stream.ui.get_config_file()
ids = {c["type"]: c["id"] for c in cfg["components"]}
tx, radio, webrtc = ids["textbox"], ids["radio"], ids["webrtc"]
comps = cfg["components"]
types = [c["type"] for c in comps]
assert types.count("textbox") == 1, "layout must not add a second textbox"
assert types.count("radio") == 1, "layout must not add a second radio"
tx_props = next(c["props"] for c in comps if c["type"] == "textbox")
assert tx_props["autoscroll"] is True, "transcript must auto-follow"
assert tx_props["interactive"] is False, "transcript must stay read-only"
row_props = next(c["props"] for c in comps if c["type"] == "row")
assert "purr-layout" in (row_props.get("elem_classes") or []), "two-region shell missing"
w_props = next(c["props"] for c in comps if c["type"] == "webrtc")
assert "purr-webrtc" in (w_props.get("elem_classes") or []), "source-picker scoping hook missing"
deps = cfg["dependencies"]
assert any(set(d.get("inputs") or []) >= {webrtc, radio} for d in deps), "stream must take (webrtc, mode)"
assert any((d.get("outputs") or []) == [tx] for d in deps), "transcript must be the stream output"
print("constructed OK", type(app.stream).__name__)
""")

run("Space-mode binds the ZeroGPU probe to a Gradio event", """
import sys, types

class _FakeSpaces:
    @staticmethod
    def GPU(fn=None):
        return fn if fn is not None else (lambda f: f)

sys.modules["spaces"] = _FakeSpaces()

import app
names = [getattr(getattr(f, "fn", None), "__name__", "") for f in app.stream.ui.fns.values()]
assert any(n == "_zerogpu_startup_probe" for n in names), names
print("zerogpu probe bound OK")
""")

run("mode is read from the additional-input slot", """
import app
h = app.StreamingTranscriber()
assert h._mode() == "enhanced", "default must be enhanced"
h.latest_args = ["__webrtc_value__", "raw"]
assert h._mode() == "raw"
h.latest_args = ["__webrtc_value__", "enhanced"]
assert h._mode() == "enhanced"
print("mode parsing OK")
""")

run("live partials stream, then commit on silence", r"""
import numpy as np
import app

class FakeASR:
    def prepare(self):
        pass

    def transcribe(self, audio, language):
        return "hello world", "en"

sr = 16000
speech = (0.2 * np.sin(2 * np.pi * 220 * np.arange(sr) / sr)).astype(np.float32)
silence = np.zeros(sr, dtype=np.float32)

h = app.StreamingTranscriber(asr=FakeASR())
h.latest_args = ["__webrtc_value__", "raw"]

# 1s of speech -> a partial is emitted while the utterance is still open
h.receive((sr, speech))
h._last_decode_t = 0.0
out = h.emit()
assert isinstance(out, app.AdditionalOutputs), out
assert out.args[0] == "hello world", out.args

# a run of silence commits the utterance; the visible text is unchanged, so
# no emission is required, but the state must commit and reset
h.receive((sr, silence))
h._last_decode_t = 0.0
out = h.emit()
if out is not None:
    assert isinstance(out, app.AdditionalOutputs), out
    assert out.args[0] == "hello world", out.args
assert h._partial == "" and h._committed == "hello world", (h._partial, h._committed)

# the next utterance starts a new line
h.receive((sr, speech))
h._last_decode_t = 0.0
out = h.emit()
assert out.args[0] == "hello world\nhello world", out.args
print("streaming partials OK")
""")

run("silence with no speech never decodes", r"""
import numpy as np
import app

class Boom:
    def prepare(self):
        pass

    def transcribe(self, audio, language):
        raise AssertionError("must not transcribe silence")

h = app.StreamingTranscriber(asr=Boom())
h.latest_args = ["__webrtc_value__", "raw"]
for _ in range(3):
    h.receive((16000, np.zeros(16000, dtype=np.float32)))
    h._last_decode_t = 0.0
    assert h.emit() is None
assert h._committed == "" and h._partial == ""
print("silence gate OK")
""")

run("too little audio does not trigger a decode", r"""
import numpy as np
import app

class Boom:
    def prepare(self):
        pass

    def transcribe(self, audio, language):
        raise AssertionError("must not decode tiny chunks")

h = app.StreamingTranscriber(asr=Boom())
h.latest_args = ["__webrtc_value__", "raw"]
tiny = (0.2 * np.sin(2 * np.pi * 220 * np.arange(400) / 16000)).astype(np.float32)
h.receive((16000, tiny))
h._last_decode_t = 0.0
assert h.emit() is None
print("min-chunk gate OK")
""")

run("decode failure preserves the partial and surfaces once", r"""
import numpy as np
import app

class FlakyASR:
    def __init__(self):
        self.calls = 0
    def prepare(self):
        pass
    def transcribe(self, audio, language):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("boom")
        return "recovered", "en"

sr = 16000
speech = (0.2 * np.sin(2 * np.pi * 220 * np.arange(sr) / sr)).astype(np.float32)

h = app.StreamingTranscriber(asr=FlakyASR())
h.latest_args = ["__webrtc_value__", "raw"]
h.receive((sr, speech))
h._last_decode_t = 0.0
out = h.emit()
assert isinstance(out, app.AdditionalOutputs), out
assert out.args[0] == "[unavailable: boom]", out.args
assert h._partial == "", "a failed decode must not fabricate a partial"

h._last_decode_t = 0.0
out2 = h.emit()
assert out2.args[0] == "recovered", out2.args
assert h._partial == "recovered"
print("decode-failure handling OK")
""")

run("partial decode is windowed; commit decodes the whole utterance", r"""
import numpy as np
import app

class LengthASR:
    def __init__(self):
        self.lengths = []
    def prepare(self):
        pass
    def transcribe(self, audio, language):
        self.lengths.append(len(audio))
        return "x", "en"

sr = 16000
a = LengthASR()
h = app.StreamingTranscriber(asr=a)
h.latest_args = ["__webrtc_value__", "raw"]

n = int((app.PARTIAL_WINDOW_S + 4) * sr)
speech = (0.2 * np.sin(2 * np.pi * 220 * np.arange(n) / sr)).astype(np.float32)
h.receive((sr, speech))
h._last_decode_t = 0.0
assert isinstance(h.emit(), app.AdditionalOutputs)
assert a.lengths[-1] == int(app.PARTIAL_WINDOW_S * sr), a.lengths

h.receive((sr, np.zeros(2 * sr, dtype=np.float32)))
h._last_decode_t = 0.0
h.emit()
assert a.lengths[-1] == n + 2 * sr, a.lengths
print("partial window OK", a.lengths)
""")

run("utterance buffer is capped even before ASR is ready", r"""
import numpy as np
import app

h = app.StreamingTranscriber()
h.latest_args = ["__webrtc_value__", "raw"]
cap = int(app.MAX_UTTERANCE_S * 16000)
chunk = (0.2 * np.ones(16000, dtype=np.float32))
for _ in range(int(app.MAX_UTTERANCE_S) + 3):
    h.receive((16000, chunk))
assert h._utt.size <= cap, h._utt.size
print("buffer cap OK", h._utt.size)
""")

run("audio helpers with synthetic mono/stereo int16", """
import numpy as np
import app
t = np.linspace(0, 0.5, 8000)
chirp = (np.sin(2*np.pi*(600+2000*t)) * 3000).astype(np.int16)
sr, mono = app.to_mono_float32((16000, chirp[None, :]))
assert mono.ndim == 1 and mono.dtype == np.float32
assert mono.max() <= 1.0 and mono.min() >= -1.0
assert np.allclose(app.resample(mono, 16000, 16000), mono)
assert len(app.resample(mono, 16000, 24000)) == 12000
assert app.rms(mono) > 0.0 and app.rms(np.zeros(10, dtype=np.float32)) == 0.0
print("audio helpers OK")
""")

run("transcript line append keeps a single string", """
import app
assert app.append_transcript_line("", "a") == "a"
assert app.append_transcript_line("a", "b") == "a\\nb"
print("append helper OK")
""")

run("turn creds helper graceful without token", """
import app
rc = app.rtc_configuration()
print("rtc_configuration ->", rc)
""")

sys.exit(1 if any(RESULTS) else 0)
