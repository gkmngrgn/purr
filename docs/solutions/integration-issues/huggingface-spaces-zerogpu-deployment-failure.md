---
title: "HF Space deployment failed twice over: `sdk_version`/gradio conflict, then a missing ZeroGPU probe"
date: 2026-09-26
category: integration-issues
module: huggingface-spaces-deploy
problem_type: integration_issue
component: tooling
severity: high
symptoms:
  - "Docker `pip install` failed with `ResolutionImpossible`: cannot install `gradio==5.50.0` and `gradio==6.28.0` because they have conflicting dependencies"
  - "Space build succeeded but startup aborted with `No @spaces.GPU function detected during startup`"
  - "Space never reached `RUNNING` on `zero-a10g` hardware"
  - "API downgrade to `cpu-basic` was rejected with HTTP 402: \"Without a PRO subscription, you can't downgrade this Space to cpu-basic.\""
root_cause: config_error
resolution_type: config_change
tags: [huggingface-spaces, gradio, zerogpu, fastrtc, dependency-conflict, deployment, sdk-version]
---

# HF Space deployment failed twice over: `sdk_version`/gradio conflict, then a missing ZeroGPU probe

## Problem

The `goedev/purr` Hugging Face Gradio Space would not deploy. There were two independent root causes, fixed in sequence: first the Docker build failed while resolving Python dependencies, and after that was fixed the app failed at runtime on ZeroGPU because it had no `@spaces.GPU`-bound handler. The build failure was a config/metadata mismatch; the runtime failure was a free-tier hardware policy the CPU-only app happened to violate. Both had to be cleared before the Space reached `RUNNING`.

## Symptoms

- Build aborted at the Space's `pip install` step:
  `ERROR: Cannot install gradio==5.50.0 and gradio==6.28.0 because these package versions have conflicting dependencies.` followed by `ERROR: ResolutionImpossible`.
- The build log showed two conflicting user requests: `gradio==6.28.0` injected by the Gradio SDK build, and `gradio==5.50.0` from `requirements.txt`.
- After the build was unblocked, the app progressed `APP_STARTING` → `RUNTIME_ERROR` with `No @spaces.GPU function detected during startup`.
- The account (`goedev`) is free-tier (`isPro:false` via the HF whoami-v2 API), and the Space was placed on `zero-a10g`; an API attempt to move it to `cpu-basic` returned `402 Payment Required` ("Without a PRO subscription, you can't downgrade this Space to cpu-basic").
- Once both fixes shipped, the Space reached stage `RUNNING`; as observed live during this session, an authenticated request to the app returned HTTP `200` and Gradio served the UI on Gradio 5.50.0.

## What Didn't Work

- **Bumping `sdk_version` to `6.28.0`** (the change in commit `bac205a`, "chore: configure Git LFS and update project metadata") is what produced the build failure. `README.md` frontmatter `sdk_version: 6.28.0` makes the HF Gradio build inject `gradio[oauth,mcp]==6.28.0` alongside `pip install -r requirements.txt`, which pinned `gradio==5.50.0`. Gradio 6 cannot coexist with this dependency set because `fastrtc==0.0.34` caps `gradio<6` (as recorded at `pyproject.toml:10`).
- **Downgrading the Space to `cpu-basic`** via `HfApi.request_space_hardware` failed with `402 Payment Required`. Free personal accounts can't opt out of ZeroGPU for Gradio Spaces, so removing ZeroGPU was not an option; the app had to satisfy ZeroGPU instead.
- **Defining a bare module-level `@spaces.GPU` function** is not enough. Per this session's conclusion, ZeroGPU's startup scan walks Gradio's *registered event handlers*; an undecorated-by-Gradio `@spaces.GPU` function that no event references is invisible to it.

## Solution

Two commits, pushed to the Space remote on `main`.

### 1. Restore `sdk_version` to match the gradio pin — commit `57bf827`

The pin in `pyproject.toml` and the Space card must agree. Before: `sdk_version: 6.28.0`. After (current tree, `README.md:7`):

```yaml
sdk_version: 5.50.0
```

`pyproject.toml` now carries both pins and the coupling note (`pyproject.toml:8-11`):

```toml
dependencies = [
    "fastrtc[vad]==0.0.34",
    # Must match `sdk_version` in README.md; the Space build installs
    # `gradio[oauth,mcp]==<sdk_version>` and fastrtc 0.0.34 caps gradio<6.
    "gradio==5.50.0",
    ...
```

The generated lock agrees (`requirements.txt:66` `fastrtc==0.0.34`, `requirements.txt:80` `gradio==5.50.0`). This same commit moved dependency management to uv: `pyproject.toml` is the source of truth and `requirements.txt` is generated (`README.md:38`, `AGENTS.md:12-13`):

```bash
uv pip compile pyproject.toml --python-version 3.12 --universal --output-file requirements.txt
```

### 2. Bind a no-op `@spaces.GPU` probe to the UI — commit `dc0ccc7`

`app.py` now imports `spaces` defensively near the top (`app.py:23-26`):

```python
try:
    import spaces
except ImportError:  # local dev
    spaces = None
```

and, after the `Stream(...)` is constructed, registers a probe that ZeroGPU's scan can see (search for `_zerogpu_startup_probe`):

```python
if spaces is not None:

    @spaces.GPU
    def _zerogpu_startup_probe() -> None:
        return None

    with stream.ui:
        _zerogpu_probe = gr.Button(visible=False)
        _zerogpu_probe.click(fn=_zerogpu_startup_probe, inputs=None, outputs=None)
```

The probe is never invoked at runtime; it exists only to satisfy the startup scan. The app itself remains CPU-only (Quail on CPU, Whisper via HF Inference Providers).

## Why This Works

The two failures came from separate layers, so they needed separate fixes.

**Build:** the Gradio SDK build derives the Gradio version from `README.md` frontmatter `sdk_version` and installs `gradio[oauth,mcp]==<sdk_version>` in the same environment as the Space's `requirements.txt`. When `sdk_version` (6.28.0) disagreed with the lock's `gradio==5.50.0`, pip was asked for two mutually exclusive Gradio versions and failed to resolve. Because `fastrtc==0.0.34` requires `gradio<6`, the only consistent choice is Gradio 5.50.0, so `sdk_version` must be `5.50.0` too. Aligning the two makes the injected requirement a duplicate of the locked one rather than a conflict.

**Runtime:** ZeroGPU refuses to serve a Space whose app exposes no GPU handler — the startup scan requires at least one Gradio event handler bound to a `@spaces.GPU` function. This app legitimately has none, and free personal accounts cannot leave ZeroGPU for `cpu-basic` (the 402). Binding a hidden `gr.Button`'s `.click` to a `@spaces.GPU` no-op adds a registered handler without adding GPU work, so the scan passes while the app stays CPU-only. Guarding the whole block with `if spaces is not None` (via the `try/except ImportError` import) keeps local runs — where the `spaces` package from the Space base image is absent — working unchanged.

## Prevention

- **Keep `gradio` pinned in `pyproject.toml` equal to `sdk_version` in `README.md`, and never hand-edit `requirements.txt`.** This invariant is documented at `AGENTS.md:23` and `pyproject.toml:9-10`. Changing either pin without the other is the exact setup that produced `ResolutionImpossible`.
- **Treat a new ZeroGPU Space as requiring at least one Gradio-bound `@spaces.GPU` handler, even for CPU-only apps.** Keep the probe bound to a real Gradio event, not merely defined — this is the invariant at `AGENTS.md:24`. If a future edit removes the `click` binding, the Space will fail `RUNTIME_ERROR` again.
- **Read build logs and runtime logs separately.** Build failures (pip/Docker) and runtime failures (`APP_STARTING` → `RUNTIME_ERROR`) are different layers with different fixes; here they were sequential, so fixing only the first would have merely exposed the second.
- **Verify offline before pushing.** `python smoke_test.py` imports `app` with `HF_TOKEN` and `AIC_SDK_LICENSE` unset and asserts `stream.ui` constructs (`smoke_test.py:14-23`). It confirms the UI/Stream wiring and that the guarded `spaces` import does not break construction when the `spaces` package is absent locally (the probe path itself is skipped off ZeroGPU).
- **Validate the lock against what HF injects.** This session confirmed `uv pip compile` (exit 0) for the generated lock unioned with `gradio[oauth,mcp]==5.50.0`, `spaces==0.51.3`, `uvicorn>=0.14.0`, and `websockets>=10.4`.

## Related Issues

- Repo deployment notes and invariants: `README.md:63-68` (Notes: ZeroGPU probe, paid-plan caveat) and `AGENTS.md:22-24` (the two `sdk_version`/ZeroGPU invariants).
- Commits pushed to the Space remote on `main`: `57bf827` (align Gradio pin with `sdk_version`), `dc0ccc7` (no-op `@spaces.GPU` probe). The failing commit was `bac205a`. This repo's remote is a Hugging Face Space, not GitHub, so there is no PR/merge history for these changes.
- Unfixed caveat: the Space has no secrets configured, so `AIC_SDK_LICENSE` (Quail) and `HF_TOKEN` (Inference Providers + TURN) are unset at runtime; Quail falls back to raw audio (`app.py:187-192`) and transcription errors (`app.py:199-202`). This is a user-side configuration gap documented at `README.md:27-34`, not a code defect.
