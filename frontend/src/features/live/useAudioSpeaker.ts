// Speaker side of the audio relay: microphone -> 16 kHz mono PCM16 -> 20 ms framed binary messages
// over WS /ws/sessions/{id}/audio?role=speaker&token=<host token>.
//
// Handshake: -> {"type":"hello","codec":"pcm16","sample_rate":16000,"channels":1,"frame_ms":20,"takeover":true}
//            <- {"type":"ready",...,"max_payload_bytes","broadcast"}  (frames are only sent after this)
//            <- {"type":"error","code","message"} then a close (4400/4401/4403/4404/4408/4409/4410/4423)
import { useCallback, useEffect, useRef, useState } from 'react'
import { wsUrl } from '../../api/client'
import { FRAME_MS, FRAME_SAMPLES, StreamResampler, WIRE_SAMPLE_RATE, encodeFrame, floatToPcm16, rms } from './pcm'
import { backoff } from './session'

export type SpeakerStatus = 'off' | 'starting' | 'connecting' | 'live' | 'reconnecting' | 'error'

export interface UseAudioSpeaker {
  status: SpeakerStatus
  error: string | null
  /** Mic input level 0..1 (RMS, roughly perceptual), updated ~12×/s while the mic is on. */
  level: number
  /** From the server's `ready`: false when the host turned audio broadcast off (frames feed STT only). */
  broadcast: boolean | null
  framesSent: number
  framesDropped: number
  /** Ask for the mic and start streaming. Call from a click handler (AudioContext needs a user gesture). */
  start: () => Promise<void>
  stop: () => void
}

const PING_MS = 15_000
/** Drop frames instead of queueing when the socket is this far behind (≈ 1 s of audio). */
const MAX_BUFFERED_BYTES = 32_000
const STOP_CODES: Record<number, string> = {
  4400: 'The server rejected the audio stream.',
  4401: 'The host token was rejected.',
  4403: 'You are not allowed to broadcast audio in this session.',
  4404: 'This live session no longer exists.',
  4409: 'Start the session before turning the microphone on.',
  4410: 'Another host took over the audio stream.',
  4423: 'Someone else is already broadcasting.',
  4429: 'Too many listeners.',
}

// AudioWorklet processor: batches ~20 ms of mono input and posts it to the main thread.
const WORKLET = `
class EventlyCapture extends AudioWorkletProcessor {
  constructor() { super(); this.target = Math.max(128, Math.round(sampleRate * 0.02)); this.buf = new Float32Array(this.target + 128); this.n = 0 }
  process(inputs) {
    const input = inputs[0]
    if (input && input.length) {
      const chs = input.length, len = input[0].length
      for (let i = 0; i < len; i++) {
        let v = 0
        for (let c = 0; c < chs; c++) v += input[c][i]
        this.buf[this.n++] = v / chs
      }
      if (this.n >= this.target) { this.port.postMessage(this.buf.slice(0, this.n)); this.n = 0 }
    }
    return true
  }
}
registerProcessor('evently-capture', EventlyCapture)
`

interface Pipeline {
  stream: MediaStream
  ctx: AudioContext
  nodes: AudioNode[]
}

function micError(e: unknown): string {
  const name = e instanceof DOMException ? e.name : ''
  if (name === 'NotAllowedError' || name === 'SecurityError') return 'Microphone permission was denied.'
  if (name === 'NotFoundError') return 'No microphone found.'
  if (name === 'NotReadableError') return 'The microphone is in use by another app.'
  if (!window.isSecureContext) return 'The microphone needs HTTPS (or localhost).'
  return e instanceof Error ? e.message : 'Could not start the microphone.'
}

export function useAudioSpeaker(sessionId: string, token: string | null): UseAudioSpeaker {
  const [status, setStatus] = useState<SpeakerStatus>('off')
  const [error, setError] = useState<string | null>(null)
  const [level, setLevel] = useState(0)
  const [broadcast, setBroadcast] = useState<boolean | null>(null)
  const [counts, setCounts] = useState({ sent: 0, dropped: 0 })

  // Everything the audio callbacks touch lives in refs: they run outside React.
  const r = useRef({
    wanted: false,
    pipeline: null as Pipeline | null,
    ws: null as WebSocket | null,
    ready: false,
    attempt: 0,
    retry: undefined as ReturnType<typeof setTimeout> | undefined,
    ping: undefined as ReturnType<typeof setInterval> | undefined,
    meter: undefined as ReturnType<typeof setInterval> | undefined,
    resampler: null as StreamResampler | null,
    pending: new Float32Array(FRAME_SAMPLES * 16),
    pendingLen: 0,
    seq: 0,
    samplesSent: 0,
    level: 0,
    sent: 0,
    dropped: 0,
    lastServerError: null as string | null,
  })

  const teardown = useCallback(() => {
    const s = r.current
    s.wanted = false
    clearTimeout(s.retry)
    clearInterval(s.ping)
    clearInterval(s.meter)
    if (s.ws) {
      s.ws.onclose = null
      s.ws.close(1000)
      s.ws = null
    }
    s.ready = false
    if (s.pipeline) {
      for (const n of s.pipeline.nodes) n.disconnect()
      s.pipeline.stream.getTracks().forEach((t) => t.stop())
      void s.pipeline.ctx.close().catch(() => undefined)
      s.pipeline = null
    }
    s.pendingLen = 0
    s.level = 0
  }, [])

  const stop = useCallback(() => {
    teardown()
    setStatus('off')
    setLevel(0)
    setBroadcast(null)
  }, [teardown])

  const fail = useCallback(
    (message: string) => {
      teardown()
      setStatus('error')
      setError(message)
      setLevel(0)
    },
    [teardown],
  )

  // 16 kHz samples -> 20 ms frames -> socket (or dropped while not ready / backed up).
  const pushSamples = useCallback((input: Float32Array) => {
    const s = r.current
    s.level = Math.max(s.level * 0.85, Math.min(1, rms(input) * 4))
    const samples = s.resampler ? s.resampler.process(input) : input
    if (s.pendingLen + samples.length > s.pending.length) s.pendingLen = 0 // cannot happen at sane rates; never grow unbounded
    s.pending.set(samples, s.pendingLen)
    s.pendingLen += samples.length
    let offset = 0
    while (s.pendingLen - offset >= FRAME_SAMPLES) {
      const chunk = s.pending.subarray(offset, offset + FRAME_SAMPLES)
      offset += FRAME_SAMPLES
      const ws = s.ws
      if (!ws || ws.readyState !== WebSocket.OPEN || !s.ready || ws.bufferedAmount > MAX_BUFFERED_BYTES) {
        s.dropped++
        continue
      }
      const timestampMs = (s.samplesSent * 1000) / WIRE_SAMPLE_RATE
      ws.send(encodeFrame(s.seq++, timestampMs, WIRE_SAMPLE_RATE, floatToPcm16(chunk)))
      s.samplesSent += FRAME_SAMPLES
      s.sent++
    }
    s.pending.copyWithin(0, offset, s.pendingLen)
    s.pendingLen -= offset
  }, [])

  const connect = useCallback(function reconnect() {
    const s = r.current
    if (!s.wanted || !token) return
    setStatus(s.attempt ? 'reconnecting' : 'connecting')
    const ws = new WebSocket(wsUrl(`/ws/sessions/${encodeURIComponent(sessionId)}/audio?role=speaker`, token))
    ws.binaryType = 'arraybuffer'
    s.ws = ws
    s.ready = false
    s.lastServerError = null
    ws.onopen = () => {
      ws.send(
        JSON.stringify({ type: 'hello', codec: 'pcm16', sample_rate: WIRE_SAMPLE_RATE, channels: 1, frame_ms: FRAME_MS, takeover: true }),
      )
      clearInterval(s.ping)
      s.ping = setInterval(() => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify({ type: 'ping', t: Date.now() })), PING_MS)
    }
    ws.onmessage = (msg) => {
      if (typeof msg.data !== 'string') return
      let body: { type?: string; broadcast?: boolean; code?: string; message?: string }
      try {
        body = JSON.parse(msg.data)
      } catch {
        return
      }
      if (body.type === 'ready') {
        s.ready = true
        s.attempt = 0
        setBroadcast(body.broadcast !== false)
        setStatus('live')
        setError(null)
      } else if (body.type === 'error') {
        s.lastServerError = body.message || body.code || 'error'
      }
    }
    ws.onclose = (ev) => {
      clearInterval(s.ping)
      if (s.ws === ws) s.ws = null
      s.ready = false
      if (!s.wanted) return
      const stopReason = STOP_CODES[ev.code]
      if (stopReason) {
        fail(ev.code === 4400 && s.lastServerError ? `${stopReason} (${s.lastServerError})` : stopReason)
        return
      }
      setStatus('reconnecting')
      s.retry = setTimeout(reconnect, backoff(s.attempt++, 8000))
    }
  }, [sessionId, token, fail])

  const start = useCallback(async () => {
    const s = r.current
    if (s.wanted) return
    if (!token) {
      setError('No host token for this session.')
      setStatus('error')
      return
    }
    s.wanted = true
    s.attempt = 0
    s.seq = 0
    s.samplesSent = 0
    s.sent = 0
    s.dropped = 0
    setError(null)
    setStatus('starting')

    // Create the context inside the click, before any await, so autoplay policies let it run.
    const ctx = new AudioContext({ latencyHint: 'interactive' })
    void ctx.resume()
    let stream: MediaStream
    try {
      if (!navigator.mediaDevices?.getUserMedia) throw new DOMException('getUserMedia unavailable', window.isSecureContext ? 'NotFoundError' : 'SecurityError')
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      })
    } catch (e) {
      void ctx.close()
      fail(micError(e))
      return
    }
    if (!s.wanted) {
      stream.getTracks().forEach((t) => t.stop())
      void ctx.close()
      return
    }

    const source = ctx.createMediaStreamSource(stream)
    const mute = ctx.createGain()
    mute.gain.value = 0
    mute.connect(ctx.destination) // keeps the graph pulled; nothing is audible
    let capture: AudioNode
    try {
      const url = URL.createObjectURL(new Blob([WORKLET], { type: 'application/javascript' }))
      try {
        await ctx.audioWorklet.addModule(url)
      } finally {
        URL.revokeObjectURL(url)
      }
      const node = new AudioWorkletNode(ctx, 'evently-capture', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] })
      node.port.onmessage = (e: MessageEvent<Float32Array>) => pushSamples(e.data)
      capture = node
    } catch {
      // No AudioWorklet (old Safari, insecure context): ScriptProcessor fallback.
      const node = ctx.createScriptProcessor(2048, 1, 1)
      node.onaudioprocess = (e) => pushSamples(new Float32Array(e.inputBuffer.getChannelData(0)))
      capture = node
    }
    if (!s.wanted) {
      stream.getTracks().forEach((t) => t.stop())
      void ctx.close()
      return
    }
    source.connect(capture)
    capture.connect(mute)
    s.pipeline = { stream, ctx, nodes: [source, capture, mute] }
    s.resampler = new StreamResampler(ctx.sampleRate, WIRE_SAMPLE_RATE)
    s.pendingLen = 0
    // A track can end on its own (device unplugged, permission revoked).
    stream.getAudioTracks()[0]?.addEventListener('ended', () => r.current.wanted && fail('The microphone was disconnected.'))

    clearInterval(s.meter)
    s.meter = setInterval(() => {
      setLevel(Math.round(r.current.level * 100) / 100)
      setCounts((c) => (c.sent === r.current.sent && c.dropped === r.current.dropped ? c : { sent: r.current.sent, dropped: r.current.dropped }))
    }, 80)
    connect()
  }, [token, connect, fail, pushSamples])

  useEffect(() => teardown, [teardown])

  return { status, error, level, broadcast, framesSent: counts.sent, framesDropped: counts.dropped, start, stop }
}
