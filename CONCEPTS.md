# Concepts

Shared domain vocabulary for this project — entities, named processes, and status concepts with project-specific meaning. Seeded with core domain vocabulary, then accretes as ce-compound and ce-compound-refresh process learnings; direct edits are fine. Glossary only, not a spec or catch-all.

## Speech pipeline

### Purr
The demo application: a streaming speech-to-text Space that turns microphone speech into a cleaned transcript.

### Continuous streaming
The pipeline emits a live transcript while the user is still speaking. The current utterance is re-decoded on a timer and its result is shown as a partial; the pipeline does not wait for a pause before producing text.

### Partial transcript
The re-decoded hypothesis for the utterance in progress. It replaces the previous partial on each update and may change wording slightly as more audio arrives; it is distinct from the committed transcript lines behind it.

### Utterance
One spoken input, buffered into a single segment while the user talks. It is the unit that gets enhanced and re-decoded, and a run of silence (or reaching the maximum length) commits it to the transcript.

### Quail enhancement
The optional speech-enhancement stage that cleans an utterance with ai-coustics Quail before transcription. It runs on CPU as a per-connection streaming processor. When enhancement is unavailable — for example a missing license — the pipeline falls back to the raw audio and annotates the transcript.

### Enhancement mode
The user-facing choice between *enhanced* (run Quail enhancement before transcription) and *raw* (transcribe the microphone audio as-is). Raw reveals the fidelity the transcriber actually receives.

### Microphone source
The input device a recording streams from, chosen by the visitor in the control rail. The choice applies to the next recording, not the one in progress, and is only available once the browser has granted microphone access.

### Transcript
The accumulated text output: the committed lines from finished utterances followed by the live partial. Enhancement is observed through the transcript rather than through audio playback, which is intentionally absent to avoid feedback.

### Local ASR
The speech-to-text stage. It runs in-process on the Space's CPU rather than through a hosted provider, so there is no per-request network latency, token, or cost. The recognition engine and checkpoint are configurable.

## Space deployment

### ZeroGPU
The Hugging Face hardware tier that free-tier Gradio Spaces run on. It allocates GPU time per request, so an app must expose at least one GPU-marked handler to start — even when the app itself does no GPU work.

### ZeroGPU startup probe
A no-op handler marked for GPU execution and bound to a UI event, whose only purpose is to satisfy ZeroGPU's startup scan. It is never invoked and performs no GPU work. Merely defining a marked handler is not enough; it must be bound to an event the UI registers.

### sdk_version / gradio pin coupling
The invariant that the Space card's declared SDK version and the project's own Gradio dependency pin must name the same version. The Space build installs the declared SDK version into the same environment as the project's pins, so the two become a dependency conflict when they disagree.

### WebRTC relay
The relayed path that carries audio between the browser and the Space when a direct WebRTC connection cannot be made — the Space's firewall blocks direct connections, so audio needs a relay. Purr serves a public STUN configuration by default and relays through TURN only when relay credentials are configured; the Hugging Face token is never sent to a TURN endpoint.
