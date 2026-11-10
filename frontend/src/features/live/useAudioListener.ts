// Listener side of the audio relay: WS /ws/sessions/{id}/audio?role=listener&token=<participant token>.
//
// The listener sends no hello. It receives:
//   {"type":"format",codec,sample_rate,channels,frame_ms}   a stream (re)started
//   binary frames (12-byte header + PCM16 LE), verbatim from the speaker; a seq jump = frames dropped for us
//   {"type":"stopped","reason"}   the stream ended (source_left, takeover, broadcast_disabled…)
//   {"type":"disabled"}           audio_broadcast is off (the socket stays open)
//   {"type":"error","code","message"} before a server close (4401/4404/4409/4429…)
// Frames are scheduled back-to-back as AudioBufferSourceNodes behind a ~150 ms jitter buffer.
import { useCallback, useEffect, useRef, useState } from 'react'
import { wsUrl } from '../../api/client'
import { CODEC_PCM16, StreamResampler, decodeFrame } from './pcm'
import { backoff } from './session'

export type ListenerStatus = 'off' | 'connecting' | 'waiting' | 'playing' | 'disabled' | 'not_live' | 'error'

export interface UseAudioListener {
  status: ListenerStatus
  /** Why the stream stopped (`stopped.reason`), or the error message. */
  reason: string | null
  /** Audio queued ahead of the playhead, ms. */
  bufferedMs: number
  /** Frames lost on the way (server drops for slow sockets + our own catch-up drops). */
  dropped: number
  /** Start listening. Call from a tap: AudioContext needs a user gesture (autoplay policy). */
  start: () => void
  stop: () => void
  /** Reconnect now (e.g. the session just went live after a 4409). */
  retry: () => void
}

const JITTER_S = 0.15 // initial buffer after a start or an underrun
const MAX_AHEAD_S = 0.6 // beyond this we drop frames to catch up rather than drift late
const PING_MS = 20_000
const FATAL: Record<number, string> = {
  4401: 'Your join link expired. Rejoin to listen.',
  4403: 'Listening is not allowed.',
  4404: 'This live session no longer exists.',
  4429: 'This session has reached its listener limit.',
}

type AudioCtor = typeof AudioContext
const AudioCtx: AudioCtor | undefined =
  typeof window === 'undefined' ? undefined : (window.AudioContext ?? (window as unknown as { webkitAudioContext?: AudioCtor }).webkitAudioContext)

export function useAudioListener(sessionId: string, token: string | null): UseAudioListener {
  const [status, setStatus] = useState<ListenerStatus>('off')
  const [reason, setReason] = useState<string | null>(null)
  const [stats, setStats] = useState({ bufferedMs: 0, dropped: 0 })

  const r = useRef({
    wanted: false,
    ctx: null as AudioContext | null,
    gain: null as GainNode | null,
    ws: null as WebSocket | null,
    attempt: 0,
    retry: undefined as ReturnType<typeof setTimeout> | undefined,
    ping: undefined as ReturnType<typeof setInterval> | undefined,
    stats: undefined as ReturnType<typeof setInterval> | undefined,
    nextTime: 0,
    lastSeq: -1,
    resampler: null as StreamResampler | null,
    dropped: 0,
    playing: false,
  })

  const closeSocket = useCallback(() => {
    const s = r.current
    clearTimeout(s.retry)
    clearInterval(s.ping)
    if (s.ws) {
      s.ws.onclose = null
      s.ws.close(1000)
      s.ws = null
    }
  }, [])

  const resetStream = useCallback(() => {
    const s = r.current
    s.lastSeq = -1
    s.resampler = null
    s.nextTime = 0
    s.playing = false
  }, [])

  const onFrame = useCallback((buf: ArrayBuffer) => {
    const s = r.current
    const ctx = s.ctx
    const gain = s.gain
    if (!ctx || !gain) return
    const frame = decodeFrame(buf)
    if (!frame || frame.codec !== CODEC_PCM16 || !frame.samples.length) return
    if (s.lastSeq >= 0) {
      const gap = (frame.seq - s.lastSeq - 1 + 0x1_0000_0000) % 0x1_0000_0000
      if (gap > 0 && gap < 10_000) s.dropped += gap
    }
    s.lastSeq = frame.seq
    if (!s.playing) {
      s.playing = true
      setStatus('playing')
      setReason(null)
    }
    if (!s.resampler || s.resampler.inRate !== frame.sampleRate) s.resampler = new StreamResampler(frame.sampleRate, ctx.sampleRate)
    const samples = s.resampler.process(frame.samples)
    if (!samples.length) return

    const now = ctx.currentTime
    if (s.nextTime < now + 0.01) {
      s.nextTime = now + JITTER_S // start, or underrun: rebuild the cushion
    } else if (s.nextTime > now + MAX_AHEAD_S) {
      s.dropped++ // too far ahead (tab was throttled, burst after a stall): skip to catch up
      return
    }
    const buffer = ctx.createBuffer(1, samples.length, ctx.sampleRate)
    buffer.getChannelData(0).set(samples)
    const node = ctx.createBufferSource()
    node.buffer = buffer
    node.connect(gain)
    node.start(s.nextTime)
    s.nextTime += buffer.duration
  }, [])

  const connect = useCallback(function reconnect() {
    const s = r.current
    if (!s.wanted || !token) return
    closeSocket()
    setStatus((st) => (st === 'off' || st === 'error' ? 'connecting' : st))
    const ws = new WebSocket(wsUrl(`/ws/sessions/${encodeURIComponent(sessionId)}/audio?role=listener`, token))
    ws.binaryType = 'arraybuffer'
    s.ws = ws
    let serverError: string | null = null
    ws.onopen = () => {
      s.attempt = 0
      resetStream()
      setStatus((st) => (st === 'disabled' ? st : 'waiting'))
      s.ping = setInterval(() => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify({ type: 'ping', t: Date.now() })), PING_MS)
    }
    ws.onmessage = (msg) => {
      if (msg.data instanceof ArrayBuffer) {
        onFrame(msg.data)
        return
      }
      let body: { type?: string; reason?: string; code?: string; message?: string }
      try {
        body = JSON.parse(msg.data as string)
      } catch {
        return
      }
      switch (body.type) {
        case 'format':
          resetStream()
          setStatus('waiting') // 'playing' once the first frame arrives
          setReason(null)
          break
        case 'stopped':
          resetStream()
          setStatus(body.reason === 'broadcast_disabled' ? 'disabled' : 'waiting')
          setReason(body.reason ?? null)
          break
        case 'disabled':
          setStatus('disabled')
          setReason(body.reason ?? 'audio_broadcast_off')
          break
        case 'error':
          serverError = body.message || body.code || null
          break
      }
    }
    ws.onclose = (ev) => {
      clearInterval(s.ping)
      if (s.ws === ws) s.ws = null
      resetStream()
      if (!s.wanted) return
      const fatal = FATAL[ev.code]
      if (fatal) {
        s.wanted = false
        setStatus('error')
        setReason(fatal)
        return
      }
      if (ev.code === 4409) {
        setStatus('not_live')
        setReason(serverError ?? 'session_not_live')
      } else {
        setStatus('connecting')
      }
      s.retry = setTimeout(reconnect, backoff(s.attempt++, ev.code === 4409 ? 5000 : 10000))
    }
  }, [sessionId, token, closeSocket, onFrame, resetStream])

  const start = useCallback(() => {
    const s = r.current
    if (s.wanted) return
    if (!AudioCtx) {
      setStatus('error')
      setReason('This browser cannot play live audio.')
      return
    }
    s.wanted = true
    s.dropped = 0
    // Created and resumed synchronously inside the tap, or iOS/Chrome keep it suspended.
    if (!s.ctx || s.ctx.state === 'closed') {
      s.ctx = new AudioCtx({ latencyHint: 'interactive' })
      s.gain = s.ctx.createGain()
      s.gain.connect(s.ctx.destination)
    }
    const ctx = s.ctx
    // Safari 17+: play through the silent switch like a media app.
    const session = (navigator as unknown as { audioSession?: { type: string } }).audioSession
    if (session) session.type = 'playback'
    void ctx.resume()
    // A one-sample silent buffer "unlocks" output on older iOS.
    const unlock = ctx.createBufferSource()
    unlock.buffer = ctx.createBuffer(1, 1, ctx.sampleRate)
    unlock.connect(ctx.destination)
    unlock.start()

    clearInterval(s.stats)
    s.stats = setInterval(() => {
      const c = r.current.ctx
      const ahead = c && r.current.playing ? Math.max(0, r.current.nextTime - c.currentTime) * 1000 : 0
      setStats((prev) => {
        const next = { bufferedMs: Math.round(ahead / 10) * 10, dropped: r.current.dropped }
        return prev.bufferedMs === next.bufferedMs && prev.dropped === next.dropped ? prev : next
      })
    }, 250)
    connect()
  }, [connect])

  const stop = useCallback(() => {
    const s = r.current
    s.wanted = false
    closeSocket()
    clearInterval(s.stats)
    resetStream()
    void s.ctx?.suspend().catch(() => undefined)
    setStatus('off')
    setReason(null)
    setStats({ bufferedMs: 0, dropped: 0 })
  }, [closeSocket, resetStream])

  const retry = useCallback(() => {
    const s = r.current
    if (!s.wanted || (s.ws && s.ws.readyState <= WebSocket.OPEN)) return
    s.attempt = 0
    connect()
  }, [connect])

  // Resume output when the phone comes back from the background.
  useEffect(() => {
    const onVisible = () => {
      const s = r.current
      if (document.visibilityState !== 'visible' || !s.wanted) return
      void s.ctx?.resume()
      if (!s.ws) {
        s.attempt = 0
        connect()
      }
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => document.removeEventListener('visibilitychange', onVisible)
  }, [connect])

  useEffect(
    () => () => {
      const s = r.current
      s.wanted = false
      closeSocket()
      clearInterval(s.stats)
      void s.ctx?.close().catch(() => undefined)
      s.ctx = null
    },
    [closeSocket],
  )

  return { status, reason, bufferedMs: stats.bufferedMs, dropped: stats.dropped, start, stop, retry }
}
