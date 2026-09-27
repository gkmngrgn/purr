---
title: "FastRTC WebRTC failed to connect: dead turn.fastrtc.org TURN endpoint left the browser with zero ICE servers"
date: 2026-09-27
category: integration-issues
module: app
problem_type: integration_issue
component: service_object
severity: high
symptoms:
  - "Mic button hung at connecting, then reported connection failed"
  - "WebRTC negotiated with zero ICE servers"
  - "rtc_configuration() returned None when fastrtc's TURN credential fetch failed"
  - "HF_TOKEN was sent to the dead turn.fastrtc.org host"
root_cause: config_error
resolution_type: config_change
tags: [fastrtc, webrtc, turn, stun, cloudflare, ice-servers, huggingface-spaces, dependency-failure]
---

# FastRTC WebRTC failed to connect: dead turn.fastrtc.org TURN endpoint left the browser with zero ICE servers

## Problem

On the deployed Hugging Face Space, pressing the mic button never produced a working WebRTC session: the UI sat at "connecting" and then reported "connection failed", so no audio reached the transcription pipeline. FastRTC's credential lookup targeted a dead upstream domain, and when that failed the app handed the browser an empty ICE configuration.

## Symptoms

- Space logs, repeated: `[turn] falling back to default ICE servers: [Errno -3] Temporary failure in name resolution`.
- Server-side aioice traceback: `self.transport.sendto(...)` raised `AttributeError: 'NoneType' object has no attribute 'sendto'` — ICE connectivity checks never completed.
- Browser UI stuck at "connecting"; after a partial fix it showed "connection failed".
- STUN-only ICE candidates did not connect on this Space, implying direct peer paths were blocked; no relay was configured.

## What Didn't Work

- **Assuming public STUN alone would be enough.** It did not connect with STUN only, implying direct peer paths were blocked on this Space. FastRTC requires an `rtc_configuration` on Spaces (its `Stream` raises without one), but only a relay carried the media here.
- **Relying on FastRTC's free `HF_TOKEN` TURN path.** `get_cloudflare_turn_credentials` defaults to the hardcoded `CLOUDFLARE_FASTRTC_TURN_URL = "https://turn.fastrtc.org/credentials"`. That host is dead upstream — `fastrtc.org`'s hosted zone is gone, so public resolvers return SERVFAIL — producing the `Errno -3` name-resolution failures. Passing `hf_token=os.environ["HF_TOKEN"]` also sent the HF token to a dangling-delegation domain, a token-takeover risk.
- **Returning `None` from `rtc_configuration()` on failure.** FastRTC substitutes no default when the callable returns `None`, so the browser received zero ICE servers and could only hang at "connecting".
- **The `--secrets-file .env` shortcut.** The committed template ships a placeholder `HF_TOKEN`, so pointing `--secrets-file` at it would inject the placeholder rather than the real secret.

## Solution

`rtc_configuration()` in `app.py` now always returns usable ICE servers and only uses relay credentials when real Cloudflare TURN keys are configured:

- **Public STUN as the floor**, returned unconditionally:

```python
return {
    "iceServers": [
        {"urls": "stun:stun.l.google.com:19302"},
        {"urls": "stun:stun.cloudflare.com:3478"},
    ]
}
```

- **Cloudflare TURN gated on both keys and `is_space()`**, passing `hf_token=""` to force the Cloudflare-keys branch:

```python
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
```

- **Never returns `None`** and never sends `HF_TOKEN` to the dead host.

Space secrets `CLOUDFLARE_TURN_KEY_ID` and `CLOUDFLARE_TURN_KEY_API_TOKEN` were added (user-side Space configuration, not tracked in the repo; Cloudflare Calls → TURN Server app). `CONCEPTS.md` and `.env.sample` were updated, and the WebRTC connection established in a manual Space test after the relay credentials were set (2026-09-27).

## Why This Works

`get_cloudflare_turn_credentials` selects its branch on the truthiness of `hf_token`: passing `hf_token=""` is falsy, so it skips the branch that requests credentials from the dead `turn.fastrtc.org` host and instead requests fresh ICE servers from Cloudflare's TURN API. Cloudflare relays the media around the Space firewall. When the keys are absent or the Cloudflare call fails, the fallback still returns the public STUN list, so the browser always gets a non-empty `iceServers` array instead of the zero-server configuration that caused the permanent "connecting" state. Keeping `HF_TOKEN` out of the call also closes the exposure to the stale `turn.fastrtc.org` DNS delegation.

## Prevention

- Never return `None` from `rtc_configuration()`; always supply at least public STUN.
- Treat FastRTC's `hf_token` default as unusable on this version: always pass `hf_token=""` and configure `CLOUDFLARE_TURN_KEY_ID` / `CLOUDFLARE_TURN_KEY_API_TOKEN` for relay.
- Do not send `HF_TOKEN` to third-party TURN endpoints. The app no longer needs `HF_TOKEN` at all — transcription runs locally — so keep the token out of the relay path entirely.
- When a Space shows "connecting"/"connection failed", check the logs for `[turn]` lines and name-resolution errors before assuming the app code is at fault.

## Related Issues

- Upstream: gradio-app/fastrtc#429 — dead `turn.fastrtc.org`, open and unanswered since 2026-06-04.
- `docs/solutions/integration-issues/fastrtc-custom-vad-pause-detector.md` — the adjacent FastRTC configuration seam (turn detection) on the same `app.py`.
- `docs/solutions/integration-issues/huggingface-spaces-zerogpu-deployment-failure.md` — other Space deployment layers; no longer claims `HF_TOKEN` is needed for TURN (that caveat was removed when transcription moved local).
- Repo vocabulary: `CONCEPTS.md`; environment template: `.env.sample`.
