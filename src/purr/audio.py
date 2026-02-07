import asyncio
import base64

import numpy as np
import sounddevice as sd


def list_devices():
    """List all available input audio devices."""
    devices = sd.query_devices()
    inputs = []
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            inputs.append(
                {
                    "index": i,
                    "name": dev["name"],
                    "channels": dev["max_input_channels"],
                    "sample_rate": int(dev["default_samplerate"]),
                }
            )
    return inputs


async def iter_audio_chunks(
    device_index=None, sample_rate=16000, chunk_duration_ms=480
):
    """Async generator yielding base64-encoded PCM s16le mono chunks.

    Args:
        device_index: Audio device index (None for system default).
        sample_rate: Target sample rate in Hz.
        chunk_duration_ms: Duration of each chunk in milliseconds.

    Yields:
        Base64-encoded PCM 16-bit signed LE mono audio chunks.
    """
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    chunk_frames = int(sample_rate * chunk_duration_ms / 1000)

    # Query device to get its native sample rate and channels
    if device_index is not None:
        dev_info = sd.query_devices(device_index)
    else:
        dev_info = sd.query_devices(kind="input")

    dev_rate = int(dev_info["default_samplerate"])
    dev_channels = min(dev_info["max_input_channels"], 2)

    # If device rate differs, capture at native rate and compute matching blocksize
    if dev_rate != sample_rate:
        dev_blocksize = int(dev_rate * chunk_duration_ms / 1000)
    else:
        dev_blocksize = chunk_frames

    def callback(indata, frames, time_info, status):
        if status:
            loop.call_soon_threadsafe(
                lambda s=str(status): None  # silently ignore xruns
            )
        loop.call_soon_threadsafe(queue.put_nowait, indata.copy())

    stream = sd.InputStream(
        device=device_index,
        samplerate=dev_rate,
        channels=dev_channels,
        dtype="float32",
        blocksize=dev_blocksize,
        callback=callback,
    )

    with stream:
        while True:
            data = await queue.get()

            # Convert to mono if stereo
            if data.ndim > 1 and data.shape[1] > 1:
                data = data.mean(axis=1)
            else:
                data = data.flatten()

            # Resample if device rate differs from target
            if dev_rate != sample_rate:
                num_target = int(len(data) * sample_rate / dev_rate)
                indices = np.linspace(0, len(data) - 1, num_target)
                data = np.interp(indices, np.arange(len(data)), data)

            # Convert float32 [-1, 1] to int16 PCM
            pcm = np.clip(data * 32767, -32768, 32767).astype(np.int16)

            yield base64.b64encode(pcm.tobytes()).decode("ascii")
