---
title: Separate Microphone-Source Control - Plan
type: refactor
date: 2026-09-27
topic: microphone-source-control
artifact_contract: ce-unified-plan/v1
artifact_readiness: requirements-only
product_contract_source: ce-brainstorm
execution: code
---

# Separate Microphone-Source Control - Plan

## Goal Capsule

- **Objective:** Replace the CSS restyle of FastRTC's built-in source selector with a separate, labelled microphone-source control in the right rail, and make the chosen device the one the next recording streams from.
- **Product authority:** Decisions come from this brainstorm. The rest of the PURR UI redesign (control rail, full-height transcript, responsive reflow) is already implemented and is not active scope here.
- **Supersedes:** the source-picker portion of `docs/plans/2026-09-27-002-refactor-ui-control-rail-layout-plan.md` (R10, R11, and unit U3). The rest of that plan stands.
- **Open blockers:** None. The browser-side device hook is carried as a risk with a fallback.

---

## Product Contract

### Summary

The right rail gains a labelled microphone-source control that lists the visitor's input devices and sets the one the next recording uses. It replaces the current CSS restyle of FastRTC's selector, fills in once microphone permission is granted, and leaves the record button, enhancement selector, and transcript behavior unchanged.

### Problem Frame

The committed UI restyle tried to make FastRTC's built-in source selector read as a separate control by scoped CSS. That cannot work: FastRTC only mounts its `<select>` while the picker is open, so CSS can reposition a transient popup but cannot present an always-visible, separately labelled control. FastRTC also exposes no Python-side device parameter — `track_constraints` is fixed when the component is built, and device discovery lives entirely in its browser code — so a real control has to own the choice in the browser.

### Key Decisions

- **A separate, labelled device control over a distinct trigger for FastRTC's own list.** (session-settled: user-directed — chosen over just re-triggering FastRTC's picker: a real device choice is worth owning.) Governs R1, R2, R5.
- **Choose before recording over live switching.** (session-settled: user-directed — chosen over swapping tracks on an active stream: keeps the control independent of the recording.) Governs R6.
- **Populate after the first permission grant over an up-front permission prompt.** (session-settled: user-directed — chosen over requesting permission on load: avoids an extra step before recording.) Governs R4, R5.

### Requirements

**Placement and appearance**

- R1. The microphone-source control renders in the right control rail as a labelled control, visually distinct from the record button.
- R2. The control replaces the CSS restyle of FastRTC's built-in selector; the interim `.purr-webrtc` CSS is removed.
- R3. The record button, the enhancement selector, and the transcript keep their current behavior and placement.

**Device selection**

- R4. Before microphone permission is granted, the control shows a hint state and no device names.
- R5. Once permission is granted, the control lists the available input devices by label, with the browser default pre-selected.
- R6. The device the visitor selects is the one their next recording streams from; the choice does not affect an in-progress recording.
- R7. If the selected device is unavailable when a recording starts, the stream falls back to the browser default rather than failing.

**Preserved behavior**

- R8. Streaming transcription, enhancement-mode selection, error surfacing, the ZeroGPU probe binding, and the `gradio`/`fastrtc` pins are unchanged.

### Key Flows

- F1. First visit, no permission
  - **Trigger:** A visitor loads the page without having granted microphone access.
  - **Steps:** The rail renders the record button and a source control showing a hint state.
  - **Outcome:** The visitor can still start recording; the source control has no device list yet.
  - **Covers:** R1, R4.
- F2. Grant permission and choose a device
  - **Trigger:** The visitor grants microphone access.
  - **Steps:** The control lists the input devices with the default pre-selected; the visitor picks one.
  - **Outcome:** The chosen device is shown as selected.
  - **Covers:** R5.
- F3. Recording uses the chosen device
  - **Trigger:** The visitor starts a new recording after choosing a device.
  - **Steps:** The recording streams audio from the chosen device.
  - **Outcome:** Transcription reflects the chosen input.
  - **Covers:** R6.
- F4. Chosen device disappears
  - **Trigger:** The chosen device is gone when a recording starts.
  - **Steps:** The recording falls back to the browser default.
  - **Outcome:** The recording still starts; no error blocks it.
  - **Covers:** R7.

### Acceptance Examples

- AE1. Pre-permission state
  - **Given:** the page is loaded with microphone permission not yet granted.
  - **When:** the visitor looks at the rail.
  - **Then:** the source control shows a hint and lists no devices, and the record button still works.
  - **Covers:** R1, R4.
- AE2. Populated after permission
  - **Given:** the visitor has granted microphone access.
  - **When:** they open the source control.
  - **Then:** it lists the available input devices with the browser default selected.
  - **Covers:** R5.
- AE3. Next recording uses the chosen device
  - **Given:** the visitor has more than one input device and selects a non-default one.
  - **When:** they start a recording.
  - **Then:** audio streams from the selected device.
  - **Covers:** R6.
- AE4. In-progress recording is not disturbed
  - **Given:** a recording is in progress.
  - **When:** the visitor changes the source control.
  - **Then:** the current recording keeps its device; the change applies to the next one.
  - **Covers:** R6.
- AE5. Missing device falls back
  - **Given:** the visitor's chosen device is no longer available.
  - **When:** they start a recording.
  - **Then:** the recording starts on the browser default instead of failing.
  - **Covers:** R7.
- AE6. No leftover restyle
  - **Given:** the control ships.
  - **When:** the rail renders.
  - **Then:** FastRTC's built-in arrow/dropdown restyle is gone and the record button reads as a single clean control.
  - **Covers:** R2.

### Scope Boundaries

- Deferred for later: switching the device during an active recording; remembering the choice across visits; a microphone level meter.
- Outside this change: enumerating device names before permission (the browser hides them); forking or modifying FastRTC; and the already-shipped layout work.

### Dependencies / Assumptions

- Device selection can only happen in the browser. Gradio and FastRTC expose no Python device parameter, and FastRTC's `track_constraints` is fixed when the component is built, so the control needs a browser-side hook.
- Device labels require microphone permission; before permission, device enumeration returns no usable labels.
- The committed layout (rail, full-height transcript, responsive reflow) stays; this work only replaces the picker.

### Outstanding Questions

- The exact mechanism that binds the chosen device to the next recording's media request. Deferred to Planning.
- How that mechanism interacts with FastRTC's own device handling when the next recording starts. Deferred to Planning.

### Sources / Research

- `.venv/Lib/site-packages/fastrtc/webrtc.py` — no device-selection parameter; `track_constraints` is set at construction.
- `.venv/Lib/site-packages/fastrtc/templates/component/index-EQgvOkvr.js` — the `<select>` is mounted only while the picker is open.
- `docs/plans/2026-09-27-002-refactor-ui-control-rail-layout-plan.md` — the superseded CSS approach.
