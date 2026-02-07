import asyncio
import sys

import click

from purr.audio import iter_audio_chunks, list_devices
from purr.transcriber import transcribe


@click.group()
def cli():
    """Real-time speech transcription using Voxtral via vLLM."""


@cli.command()
def devices():
    """List available audio input devices."""
    devs = list_devices()
    if not devs:
        click.echo("No input devices found.")
        return
    for d in devs:
        click.echo(
            f"  [{d['index']}] {d['name']}  "
            f"({d['channels']}ch, {d['sample_rate']}Hz)"
        )


@cli.command()
@click.option(
    "-d", "--device", type=int, default=None, help="Audio device index."
)
@click.option(
    "--host", default="localhost", show_default=True, help="vLLM server host."
)
@click.option(
    "--port", default=8000, show_default=True, help="vLLM server port."
)
@click.option(
    "-m",
    "--model",
    default="mistralai/Voxtral-Mini-4B-Realtime-2602",
    show_default=True,
    help="Model name.",
)
def listen(device, host, port, model):
    """Transcribe speech in real-time."""
    ws_url = f"ws://{host}:{port}/v1/realtime"

    click.echo(f"Connecting to {ws_url} ...")
    click.echo(f"Model: {model}")
    if device is not None:
        click.echo(f"Device: {device}")
    else:
        click.echo("Device: system default")
    click.echo("Listening... (Ctrl+C to stop)\n")

    def on_delta(text):
        sys.stdout.write(text)
        sys.stdout.flush()

    def on_done(text):
        sys.stdout.write("\n")
        sys.stdout.flush()

    async def run():
        audio = iter_audio_chunks(device_index=device)
        await transcribe(ws_url, model, audio, on_delta, on_done)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        click.echo("\nStopped.")
