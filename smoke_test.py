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

run("stream+UI construction (offline, no launch)", """
import os
os.environ.pop('HF_TOKEN', None)
os.environ.pop('AIC_SDK_LICENSE', None)
import app
assert app.stream.ui is not None
assert app.mode_radio is not None
assert callable(app.rtc_configuration)
handler = app.stream.event_handler
assert isinstance(handler, app.SpeechAwarePauseHandler), type(handler).__name__
assert isinstance(handler.model, app.AICVadAdapter), type(handler.model).__name__
assert not app._vad_model, "VAD model must not be built at import"
assert app.NO_SPEECH_LINE != app.NO_WORDS_LINE
c = handler.copy()
assert isinstance(c, app.SpeechAwarePauseHandler)
assert isinstance(c.model, app.AICVadAdapter)
assert c.model is not handler.model, "each connection must get its own detector"
print("constructed OK", type(app.stream).__name__)
""")

run("aic VAD adapter satisfies the detector contract (offline, no license)", """
import os
os.environ.pop('HF_TOKEN', None)
os.environ.pop('AIC_SDK_LICENSE', None)
import numpy as np
import app
adapter = app.AICVadAdapter()
assert callable(adapter.vad) and callable(adapter.warmup)
assert app._vad_model == {}, "adapter construction must not download the model"
dur, chunks = adapter.vad((48000, np.zeros(4800, dtype=np.float32)))
assert dur == 0.0 and chunks == [], (dur, chunks)
print("adapter contract OK (fail-closed without license)")
""")

run("empty transcription reports speech-without-words, not no-speech", r"""
import numpy as np
import app
app.transcribe_16k = lambda y16: ""
y = (np.sin(2*np.pi*440*np.linspace(0, 0.3, 4800)) * 0.2).astype(np.float32)
outs = list(app.process((48000, y), "raw", "prev"))
assert isinstance(outs[-1], app.AdditionalOutputs), outs[-1]
text = outs[-1].args[0]
assert text == "prev\n" + app.NO_WORDS_LINE, text
assert app.NO_SPEECH_LINE not in text
print("no-words label OK")
""")

run("a spoken turn does not also emit a no-speech line", """
import time
import numpy as np
import app
h = app.SpeechAwarePauseHandler(lambda audio, mode, transcript: iter(()), model=app.AICVadAdapter())
h.latest_args = [None, "raw", "prev"]
h.receive((48000, np.zeros(4800, dtype=np.float32)))
h._turn_fired = True
h._last_audio_time = time.monotonic() - (h._silence_gap_s + 1)
assert h.emit() is None
print("no double append OK")
""")

run("speechless press emits no-speech line without transcription", r"""
import time
import numpy as np
import app
h = app.SpeechAwarePauseHandler(lambda audio, mode, transcript: iter(()), model=app.AICVadAdapter())
h.latest_args = [None, "enhanced", "existing"]
h.receive((48000, np.zeros(4800, dtype=np.float32)))
h._last_audio_time = time.monotonic() - (h._silence_gap_s + 1)
out = h.emit()
assert isinstance(out, app.AdditionalOutputs), out
assert out.args[0] == "existing\n" + app.NO_SPEECH_LINE, out.args
# a second silent press must emit again
h.receive((48000, np.zeros(4800, dtype=np.float32)))
h._last_audio_time = time.monotonic() - (h._silence_gap_s + 1)
out2 = h.emit()
assert isinstance(out2, app.AdditionalOutputs), out2
assert out2.args[0] == "existing\n" + app.NO_SPEECH_LINE, out2.args
print("speechless press OK")
""")

run("audio helpers with synthetic mono/stereo int16", """
import numpy as np
import app
t = np.linspace(0, 0.5, 8000)
chirp = (np.sin(2*np.pi*(600+2000*t)) * 3000).astype(np.int16)
sr, mono = app.to_mono_float32((16000, chirp[None, :]))
assert mono.ndim == 1 and mono.dtype == np.float32
assert mono.max() <= 1.0 and mono.min() >= -1.0
y16 = app.resample(mono, 16000, 16000)
assert np.allclose(y16, mono)
y24 = app.resample(mono, 16000, 24000)
assert len(y24) == 12000
i16 = app.to_int16(mono)
assert i16.dtype == np.int16 and i16.max() <= 32767
frames = list(app.playback_chunks(mono, chunk_s=0.1))
assert frames and frames[0][0] == 24000 and frames[0][1].ndim == 2
print("audio helpers OK", len(frames), "frames")
""")

run("WAV bytes via own encoder on int16 mono", """
import numpy as np
import app
y = app.to_int16(np.random.uniform(-0.3, 0.3, 16000))
b = app.wav_bytes(y)
assert b.startswith(b'RIFF') and b'fmt ' in b and b'data' in b
assert len(b) == 44 + len(y) * 2
print("wav bytes OK", len(b))
""")

run("turn creds helper graceful without token", """
import app
rc = app.rtc_configuration()
print("rtc_configuration ->", rc)
""")

sys.exit(1 if any(RESULTS) else 0)