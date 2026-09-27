# Concepts

Shared domain vocabulary for this project — entities, named processes, and status concepts with project-specific meaning. Seeded with core domain vocabulary, then accretes as ce-compound and ce-compound-refresh process learnings; direct edits are fine. Glossary only, not a spec or catch-all.

## Speech pipeline

### Purr
The demo application: a real-time speech-to-text Space that turns microphone speech into a cleaned transcript.

### Turn detection
The stage that decides when a spoken utterance has ended, so the pipeline fires once per utterance rather than on a fixed timer. Purr gets the decision from ai-coustics' voice activity detector, whose verdict also distinguishes speech that produced no words from a press with no speech.

### Utterance
One spoken input captured as a single continuous segment. It is the unit that gets enhanced, played back, and transcribed, and it is never split across stages.

### Quail enhancement
The optional speech-enhancement stage that cleans an utterance with ai-coustics Quail before playback and transcription. It runs on CPU rather than the GPU. When enhancement is unavailable — for example a missing license — the pipeline falls back to the unenhanced audio and annotates the transcript.

### Enhancement mode
The user-facing choice between *enhanced* (run Quail enhancement before playback and transcription) and *raw* (use the microphone audio as-is). Raw reveals the fidelity the transcriber actually receives.

### Transcript
The accumulated text output, appended to across utterances. It distinguishes recognized words from two non-word outcomes: speech that produced no words, and a press with no speech at all.

### Speechless press
A press-and-release during which the turn detector confirms no speech. It is reported as a distinct no-speech line rather than a transcription failure, and it is separate from an utterance that produced no recognized words.

### Whisper transcription
The speech-to-text stage. It sends the (possibly enhanced) utterance to Whisper through Hugging Face Inference Providers rather than running a model locally.

## Space deployment

### ZeroGPU
The Hugging Face hardware tier that free-tier Gradio Spaces run on. It allocates GPU time per request, so an app must expose at least one GPU-marked handler to start — even when the app itself does no GPU work.

### ZeroGPU startup probe
A no-op handler marked for GPU execution and bound to a UI event, whose only purpose is to satisfy ZeroGPU's startup scan. It is never invoked and performs no GPU work. Merely defining a marked handler is not enough; it must be bound to an event the UI registers.

### sdk_version / gradio pin coupling
The invariant that the Space card's declared SDK version and the project's own Gradio dependency pin must name the same version. The Space build installs the declared SDK version into the same environment as the project's pins, so the two become a dependency conflict when they disagree.

### WebRTC relay
The relayed path that carries audio between the browser and the Space when a direct WebRTC connection cannot be made — the Space's firewall blocks direct connections, so audio needs a relay. Purr serves a public STUN configuration by default and relays through TURN only when relay credentials are configured; the Hugging Face token is never sent to a TURN endpoint.
