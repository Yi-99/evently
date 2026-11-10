// PCM16 audio frames for the live-session audio relay (WS /ws/sessions/{id}/audio).
//
// Wire format (backend/app/live/audio_hub.py): a 12-byte big-endian header, struct ">IIHBB"
//   uint32 seq, uint32 timestamp_ms, uint16 sample_rate_hz / 100, uint8 codec (0 = PCM16 LE), uint8 flags
// followed by the payload: PCM signed 16-bit little-endian mono samples.

export const HEADER_SIZE = 12
export const CODEC_PCM16 = 0
/** What the speaker sends: 16 kHz mono PCM16 in 20 ms frames (320 samples, 640 payload bytes). */
export const WIRE_SAMPLE_RATE = 16000
export const FRAME_MS = 20
export const FRAME_SAMPLES = (WIRE_SAMPLE_RATE * FRAME_MS) / 1000

const U32 = 0x1_0000_0000

export interface FrameHeader {
  seq: number
  timestampMs: number
  sampleRate: number
  codec: number
  flags: number
}

export interface DecodedFrame extends FrameHeader {
  /** Payload converted to [-1, 1] floats (empty for non-PCM16 codecs). */
  samples: Float32Array
}

/** Header + PCM16 LE payload in one ArrayBuffer, ready for `WebSocket.send`. */
export function encodeFrame(seq: number, timestampMs: number, sampleRate: number, samples: Int16Array, flags = 0): ArrayBuffer {
  const buf = new ArrayBuffer(HEADER_SIZE + samples.length * 2)
  const view = new DataView(buf)
  view.setUint32(0, seq % U32, false)
  view.setUint32(4, Math.floor(timestampMs) % U32, false)
  view.setUint16(8, Math.round(sampleRate / 100), false)
  view.setUint8(10, CODEC_PCM16)
  view.setUint8(11, flags & 0xff)
  for (let i = 0; i < samples.length; i++) view.setInt16(HEADER_SIZE + i * 2, samples[i], true)
  return buf
}

export function decodeHeader(buf: ArrayBuffer): FrameHeader | null {
  if (buf.byteLength < HEADER_SIZE) return null
  const view = new DataView(buf)
  return {
    seq: view.getUint32(0, false),
    timestampMs: view.getUint32(4, false),
    sampleRate: view.getUint16(8, false) * 100,
    codec: view.getUint8(10),
    flags: view.getUint8(11),
  }
}

export function decodeFrame(buf: ArrayBuffer): DecodedFrame | null {
  const header = decodeHeader(buf)
  if (!header) return null
  if (header.codec !== CODEC_PCM16) return { ...header, samples: new Float32Array(0) }
  const count = (buf.byteLength - HEADER_SIZE) >> 1
  const view = new DataView(buf, HEADER_SIZE)
  const samples = new Float32Array(count)
  for (let i = 0; i < count; i++) samples[i] = view.getInt16(i * 2, true) / 32768
  return { ...header, samples }
}

export function floatToPcm16(input: Float32Array): Int16Array {
  const out = new Int16Array(input.length)
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i]))
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff
  }
  return out
}

/** Root-mean-square level of a block, 0..1. */
export function rms(input: Float32Array): number {
  if (!input.length) return 0
  let sum = 0
  for (let i = 0; i < input.length; i++) sum += input[i] * input[i]
  return Math.sqrt(sum / input.length)
}

/**
 * Streaming mono resampler that keeps its phase across chunks, so consecutive chunks join without clicks.
 * Downsampling averages the input samples falling into each output period (a box low-pass, enough for
 * speech going to 16 kHz); upsampling interpolates linearly.
 */
export class StreamResampler {
  readonly inRate: number
  readonly outRate: number
  private readonly step: number // input samples per output sample
  private pos = 0 // position of the next output sample, relative to the start of the pending input
  private last = 0 // final input sample of the previous chunk (upsampling)
  private acc = 0 // running sum / count of the output sample being built (downsampling)
  private accN = 0

  constructor(inRate: number, outRate: number) {
    this.inRate = inRate
    this.outRate = outRate
    this.step = inRate / outRate
  }

  get passthrough(): boolean {
    return this.inRate === this.outRate
  }

  process(input: Float32Array): Float32Array {
    if (this.passthrough) return input
    return this.step > 1 ? this.down(input) : this.up(input)
  }

  private down(input: Float32Array): Float32Array {
    const out = new Float32Array(Math.ceil((input.length + this.accN) / this.step) + 1)
    let n = 0
    let boundary = this.pos // input index (fractional) where the current output period ends
    if (boundary <= 0) boundary += this.step
    for (let i = 0; i < input.length; i++) {
      this.acc += input[i]
      this.accN++
      if (i + 1 >= boundary) {
        out[n++] = this.acc / this.accN
        this.acc = 0
        this.accN = 0
        boundary += this.step
      }
    }
    this.pos = boundary - input.length
    return out.subarray(0, n)
  }

  private up(input: Float32Array): Float32Array {
    // Interpolate between `last` (index -1) and input[0..]; pos is measured from index -1.
    const out = new Float32Array(Math.ceil((input.length - this.pos) / this.step) + 1)
    let n = 0
    let pos = this.pos
    while (pos < input.length) {
      const i = Math.floor(pos)
      const frac = pos - i
      const a = i === 0 ? this.last : input[i - 1]
      const b = input[i]
      out[n++] = a + (b - a) * frac
      pos += this.step
    }
    this.pos = pos - input.length
    if (input.length) this.last = input[input.length - 1]
    return out.subarray(0, n)
  }
}
