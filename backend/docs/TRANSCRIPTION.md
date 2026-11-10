# Live transcription: which API should power it?

*Decision document — facts checked on 2026-11-09 against the vendor pages linked below. Prices are
US list prices for pay-as-you-go streaming, converted to USD per audio hour; "unverified" marks
things we could not confirm from a primary source today.*

## Recommendation

| Role | Choice | Why |
|---|---|---|
| **Primary** | **Deepgram** (Nova-3 streaming today; Flux when we want model-driven end-of-turn) | Lowest price of the top-tier streaming engines (~$0.46–0.55/hr), first-class keyterm prompting (up to 100 terms, updatable mid-stream — exactly what our per-deck trigger phrases need), sub-second interims, 60+ languages, a plain WebSocket API and a $200 free credit that covers hundreds of hours of development. |
| **Fallback** | **AssemblyAI** (Universal-3.6 Pro realtime) | Same wire pattern and price class ($0.45/hr, keyterms included), independent infrastructure, sessions up to 3 hours. The code path already exists. |
| **Third option** | **OpenAI** (`gpt-live-transcribe`) | Good accuracy and keyword/prompt hints, but ~2× the price ($1.02/hr), a 60-minute session cap, 24 kHz-only input and no server-side turn detection (we had to write our own). Useful for teams that already hold only an OpenAI key. |
| **Zero-cost** | **Browser Web Speech API** (`browser` mode) | $0 and no server audio path; fine for demos and development. Quality and biasing are browser-dependent, so it is not what we ship as default. The in-process **mock** provider covers tests. |

This is why the "auto" selection order in the backend is `deepgram → assemblyai → openai`, falling back
to `browser` when no key is configured (details in [How selection works](#how-provider-selection-works-in-this-codebase)).

**Why not the hyperscalers?** Google, Azure and Amazon all stream well, but for this product they lose
on at least one of: price (Google $0.96/hr, Azure $1.00/hr), integration effort (Azure's real-time
API is SDK-only; Amazon needs SigV4-signed WebSockets), or weaker phrase biasing than Deepgram/AssemblyAI
keyterms. They become attractive only if the company standardises on that cloud.

**Worth a trial later:** Soniox ($0.12/hr, custom-vocabulary `terms`) is the price leader and
ElevenLabs Scribe v2 Realtime (~150 ms, 90+ languages, $0.39/hr) is strong on latency; both are
young enough that we would want our own accuracy test on venue audio before promoting them.

## What matters for this product

During a live presentation the speaker's audio is transcribed, shown to attendees, and used to change
slides when the speaker says a slide's trigger keyword or "next slide". So we judged every option on:

1. **Streaming latency and interim quality** — a slide change must feel instant, so we act on interim
   (non-final) text; we need interims within a few hundred milliseconds that rarely flip.
2. **Keyword / keyterm boosting** — each deck has its own trigger phrases and names; the engine must
   accept a per-session list of terms to bias towards (ideally replaceable mid-session as slides change).
3. **Price per audio hour** — an event runs 1–3 hours; a season of events is hundreds of hours.
4. **Language coverage** — events are not English-only.
5. **Noisy-venue robustness** — PA systems, crowd noise, laptop microphones.
6. **Integration effort** — plain WebSocket + PCM16 fits our audio relay; SDK-only or signed-request
   APIs cost more to build and to keep working.

## Comparison

Streaming/real-time offerings only. Latency figures are vendor claims unless noted.

| Option | Price / audio hr (streaming) | Interim results & latency | Keyword boosting | Languages | Noise / venue features | Integration | Notes |
|---|---|---|---|---|---|---|---|
| **Deepgram** Nova-3 / Flux | Nova-3 mono $0.46, multilingual $0.55; Flux EN $0.46 (+$0.08 keyterms add-on). $200 free credit. [pricing](https://deepgram.com/pricing) | Word-level interims over WebSocket; Flux adds model-based end-of-turn (~260 ms claimed) and `EagerEndOfTurn` events. [Flux docs](https://developers.deepgram.com/docs/flux/quickstart) | `keyterm` query param, up to 100 terms / 500 tokens, replaceable mid-stream via `Configure`. [keyterm docs](https://developers.deepgram.com/docs/keyterm) | Nova-3: 63 locale entries mono, 55 multilingual (code-switching). Flux: English, or 10-language multi. [languages](https://developers.deepgram.com/docs/models-languages-overview) | Tunable end-of-turn thresholds; 8–48 kHz PCM accepted. No explicit noise suppression. | Low: raw PCM16 over `wss`, token header. | 150 concurrent streams on PAYG. Our provider: `app/transcription/deepgram.py`. |
| **AssemblyAI** Universal-3.6 Pro realtime | $0.45 (keyterms included); Universal-Streaming EN $0.15 (+$0.04 keyterms). Billed on **session** time, idle included. $50 credit. [pricing](https://www.assemblyai.com/pricing) | `Turn` messages with running transcript and `end_of_turn`; word-level `word_is_final`. No ms figure published on the pages we read. [docs](https://www.assemblyai.com/docs/speech-to-text/universal-streaming) | `keyterms_prompt`, up to 100 terms ≤ 50 chars, updatable mid-session. [docs](https://www.assemblyai.com/docs/streaming/keyterms-prompting) | Multilingual via `language_codes`; full list not verified today. | Trained on voice-agent audio; `end_of_turn_confidence` for endpointing. | Low: PCM16 over `wss://streaming.assemblyai.com/v3/ws`, key header. | Sessions auto-close after 3 h. Our provider: `app/transcription/assemblyai.py`. |
| **OpenAI** `gpt-live-transcribe` | $1.02 ($0.017/min, billed by audio duration). [pricing](https://developers.openai.com/api/docs/pricing) | Text deltas as speech arrives; final per committed turn; `delay` knob `minimal…xhigh`. No timestamps or confidence. [guide](https://developers.openai.com/api/docs/guides/realtime-transcription) | `keywords` list + free-text `prompt` (limits not documented). | ISO-639-1 `languages` hints; count not published. | Optional `noise_reduction` (near/far field). **No server VAD**: client must detect turn ends. | Medium: GA Realtime WebSocket, but 24 kHz PCM only (we resample) and we implement endpointing. | **60-minute session cap** (reconnect needed for longer talks). Older `gpt-4o-transcribe`/`whisper-1` shut down 2027-02-26. Our provider: `app/transcription/openai_realtime.py`. |
| **Speechmatics** Realtime | Enhanced $0.80, Standard $0.45 (33% off with training opt-in). $100 credit. [pricing](https://www.speechmatics.com/pricing) | Partials < 500 ms (may be revised), finals ~0.7–2 s via `max_delay`. [docs](https://docs.speechmatics.com/speech-to-text/realtime/quickstart) | Custom dictionary (`additional_vocab`); limits not verified today. | 50+ per vendor marketing (not verified today). | Strong accent coverage claims; no specific noise feature verified. | Low–medium: WebSocket, JSON config. | 50 concurrent sessions self-serve. |
| **Google Cloud STT v2** (Chirp 3) | $0.96 ($0.016/min first 500k min/mo; 60 free min/mo). [pricing](https://cloud.google.com/speech-to-text/pricing) | `StreamingRecognize` with interim results; latency not published. [Chirp 3](https://docs.cloud.google.com/speech-to-text/v2/docs/chirp_3-model) | Speech adaptation / phrase hints (GA). | ~111 languages/locales in streaming (29 GA, 82 preview). | No diarization in streaming; denoiser not documented for streaming. | Medium: gRPC bidi stream + Google auth (client library in practice). | `us`/`eu` multi-regions only. |
| **Azure AI Speech** real-time | $1.00 (S1 "Speech To Text", East US retail price via [Azure Retail Prices API](https://prices.azure.com/api/retail/prices)); 5 free hrs/mo. [pricing page](https://azure.microsoft.com/en-us/pricing/details/cognitive-services/speech-services/) | Interim + final recognitions through the Speech SDK; latency not published. | Phrase list at runtime, ≤ 200 phrases recommended, weight 0–2. [docs](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/improve-accuracy-phrase-list) | 100+ per vendor marketing (not verified today). | Mature; custom speech models if ever needed. | **High for us**: real-time API is SDK-only (no public raw WebSocket protocol); our rule is no vendor SDKs. | Diarization/language-ID add-ons $0.30/hr each. |
| **Amazon Transcribe** streaming | $0.60 ($0.0001667/s = $0.01/min, us-east-1, from the [AWS price list](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/transcribe/current/us-east-1/index.json)); 60 free min/mo for 12 months. [pricing](https://aws.amazon.com/transcribe/pricing/) | Partial results with optional stabilisation; latency not published. | Custom vocabularies (included) and vocabulary filters; must be created ahead of time, not per-request. | Streaming supports a subset of the batch languages (count not verified today). | Partial-result stabilisation helps UI flicker. | Medium–high: HTTP/2 or WebSocket with SigV4 signing (SDK in practice). | Billed per second, no minimum. |
| **ElevenLabs** Scribe v2 Realtime | $0.39 on every plan (silence billed). [pricing](https://elevenlabs.io/pricing/api) | ~150 ms claimed; partial + committed transcripts; manual or VAD commit. [docs](https://elevenlabs.io/docs/overview/capabilities/speech-to-text) | Keyterms: up to 50 terms ≤ 20 chars. | 90+. | Not documented. | Low–medium: WebSocket, single-use tokens for clients. | Newest entrant here; accuracy untested by us. |
| **Soniox** stt-rt-v6 | $0.12 ($2/1M audio tokens). [pricing](https://soniox.com/pricing) | Non-final/final tokens, endpoint detection, `finalize` message; latency not published. [docs](https://docs.soniox.com/stt/rt/real-time-transcription) | `context.terms` custom vocabulary + free-text context. | 60+. | Not documented. | Low: WebSocket, PCM/auto formats. | Cheapest by far; needs our own accuracy check. |
| **Gladia** Solaria | €0.68 (~$0.74) self-serve; €50 credit. [pricing](https://www.gladia.io/pricing) | Partial transcripts; latency not published. [docs](https://docs.gladia.io/chapters/live-stt/features) | Custom vocabulary (phoneme-based), no extra cost. | 100+ (Solaria-1); Solaria-3 only 5. | Not documented. | Medium: `POST /v2/live` then WebSocket. | 30 concurrent sessions self-serve. |
| **Browser Web Speech API** | $0 | `continuous` + `interimResults`; latency depends on the browser's engine. [MDN](https://developer.mozilla.org/en-US/docs/Web/API/Web_Speech_API) | `SpeechRecognitionPhrase` contextual biasing exists in the spec; browser support not verified today. | Whatever the browser engine offers. | None controllable. | Low on the server (we receive text), but client-only; no attendee audio relay from it. | Chrome/Edge send audio to Google; Safari uses Apple's; Firefox support not verified today. Recognition can stop after pauses and needs restarting. |
| **Self-hosted Whisper family** (WhisperLiveKit, whisper_streaming, faster-whisper; NVIDIA Parakeet/Nemotron for English) | GPU time only (roughly $0.5–1+/hr for a small cloud GPU, unverified) plus operations. | Community guides report ~0.5–0.8 s for Whisper streaming wrappers; Nemotron streaming 80–1120 ms configurable. ([WhisperLiveKit](https://pypi.org/project/whisperlivekit/)) | Weak: Whisper takes only an initial prompt; no term boosting. | Whisper: ~100 languages; Parakeet/Nemotron: English-centric. | Depends on model; no vendor tuning. | High: we run and scale GPU servers and the streaming logic ourselves. | Only compelling for data-residency or offline venues. |

## OpenAI provider: what we verified and what we assumed

Built against the GA Realtime API docs read on 2026-11-09:

- Connect to `wss://api.openai.com/v1/realtime?intent=transcription` with `Authorization: Bearer <key>`
  and **no** `model=` in the URL; then send `session.update` with `session.type: "transcription"`,
  `audio.input.format {type: "audio/pcm", rate: 24000}`, `audio.input.transcription {model, languages,
  keywords, prompt, delay}` and `turn_detection: null`.
- Audio goes in `input_audio_buffer.append` (base64 PCM16 at 24 kHz); each turn ends with
  `input_audio_buffer.commit`; results arrive as `conversation.item.input_audio_transcription.delta`
  / `.completed` (matched by `item_id`); `error` events carry `error.message` / `error.code`.
- Because `gpt-live-transcribe` has no server VAD, the provider includes an energy-based endpointer
  (commit after 500 ms of quiet, or every 15 s). It has only been exercised with synthetic audio.
- **Assumed, not verified without a key:** that `delay: "low"` is accepted for `gpt-live-transcribe`
  (the guide shows it; the reference text still says `gpt-realtime-whisper` only), the maximum number
  of `keywords`, and that an empty-buffer commit is the only commit error. If a real session rejects
  the configuration, the provider raises `TranscriptionError` with OpenAI's message.

## How provider selection works in this codebase

- **`STT_PROVIDER`** (env / `.env`): `auto` (default), `deepgram`, `assemblyai`, `openai`, `browser`
  or `mock`. Keys: `DEEPGRAM_API_KEY`, `ASSEMBLYAI_API_KEY`, `OPENAI_API_KEY`.
- **`auto`** picks the first of `deepgram → assemblyai → openai` whose key is set
  (`AUTO_ORDER` in `app/transcription/factory.py`); with no key at all it resolves to `browser`.
- **Per-session override:** `SessionSettings.stt_provider` (stored on the live session) may name a
  provider explicitly; `auto` there defers to `STT_PROVIDER`. Unknown names are rejected
  (`ValueError`); a named provider without its key raises `TranscriptionError` naming the env var
  (the live-session API turns that into a 503, per the implementation plan).
- **Browser mode** means the server runs no STT: the host's browser transcribes with the Web Speech
  API and sends `transcript.text` messages over `WS /ws/sessions/{session_id}`; the slide tracker and
  attendee transcript work the same way.
- **Mock** (`MockTranscriptionProvider`) replays a scripted list of transcript events as audio arrives;
  tests and keyless development use it (`stt_provider="mock"` in the test settings).
- All providers implement `app/transcription/base.py`: `provider.open(config)` returns a stream with
  `send_audio()`, `events()`, `finish()`, `close()`; audio is PCM16 mono at `config.sample_rate`
  (16 kHz by default), `config.keyterms` carries the deck's trigger phrases, and every failure is a
  `TranscriptionError`.

## Adding another provider

1. Create `app/transcription/<name>.py` with a `TranscriptionProvider` subclass (`name = "<name>"`),
   constructed as `Provider(api_key=...)`, whose `open()` returns a `TranscriptionStream`. Map the
   vendor's partials to `TranscriptEvent(is_final=False)` with cumulative text and each finished
   utterance to exactly one `is_final=True` event; pass `config.language` and `config.keyterms`; raise
   `TranscriptionError` on auth, protocol and connection failures; make `close()` idempotent.
2. Add `<name>_api_key` to `Settings` and the `stt_provider` literal in `app/core/config.py`, and the
   name to `PROVIDER_NAMES`, `_KEY_FIELDS` and (if it should be auto-selected) `AUTO_ORDER` in
   `factory.py`, importing the module lazily inside `get_transcription_provider`.
3. Test it against an in-process fake WebSocket server (see `tests/test_stt_openai.py` for the
   pattern) — no network in tests — and add a row to the table above.
