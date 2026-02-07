import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable

import websockets


async def transcribe(
    ws_url: str,
    model: str,
    audio_stream: AsyncIterator[str],
    on_delta: Callable[[str], None],
    on_done: Callable[[str], None] | None = None,
):
    """Stream audio to vLLM realtime API and receive transcription.

    Args:
        ws_url: WebSocket URL (e.g. ws://localhost:8000/v1/realtime).
        model: Model name for session.update.
        audio_stream: Async iterator yielding base64-encoded PCM chunks.
        on_delta: Called with incremental transcription text.
        on_done: Called with final transcription text (optional).
    """
    async with websockets.connect(ws_url) as ws:
        # Initialize session with model
        await ws.send(
            json.dumps({"type": "session.update", "model": model})
        )

        # Wait for session.created
        msg = json.loads(await ws.recv())
        if msg.get("type") == "error":
            raise RuntimeError(f"Server error: {msg.get('error')}")

        async def send_audio():
            try:
                chunks_since_commit = 0
                async for chunk in audio_stream:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "input_audio_buffer.append",
                                "audio": chunk,
                            }
                        )
                    )
                    chunks_since_commit += 1

                    # Commit periodically so the server starts transcribing.
                    # At 480ms chunks, ~5 chunks ≈ 2.4s of audio per commit.
                    if chunks_since_commit >= 5:
                        await ws.send(
                            json.dumps(
                                {"type": "input_audio_buffer.commit"}
                            )
                        )
                        chunks_since_commit = 0
            except asyncio.CancelledError:
                # Send commit on shutdown so server can finalize
                with contextlib.suppress(Exception):
                    await ws.send(
                        json.dumps({"type": "input_audio_buffer.commit"})
                    )
                raise

        async def receive_text():
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    match msg.get("type"):
                        case "transcription.delta":
                            on_delta(msg.get("delta", ""))
                        case "transcription.done":
                            if on_done:
                                on_done(msg.get("text", ""))
                        case "error":
                            raise RuntimeError(
                                f"Server error: {msg.get('error')}"
                            )
            except websockets.ConnectionClosed:
                pass

        send_task = asyncio.create_task(send_audio())
        recv_task = asyncio.create_task(receive_text())

        try:
            await asyncio.gather(send_task, recv_task)
        except asyncio.CancelledError:
            send_task.cancel()
            recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(send_task, recv_task)
            raise
