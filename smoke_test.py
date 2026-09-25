import os, sys, subprocess, textwrap

RESULTS = []

def run(label, code):
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
print("constructed OK", type(app.stream).__name__)
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