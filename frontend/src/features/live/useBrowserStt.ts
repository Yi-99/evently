// Browser speech recognition (Web Speech API) for `stt_provider: browser` sessions: the host's browser
// transcribes and the page forwards the text as `transcript.text {text, is_final}` on the control socket.
import { useEffect, useRef, useState } from 'react'

interface RecognitionAlternative {
  transcript: string
}
interface RecognitionResult {
  readonly isFinal: boolean
  readonly length: number
  [index: number]: RecognitionAlternative
}
interface RecognitionEvent {
  readonly resultIndex: number
  readonly results: { readonly length: number; [index: number]: RecognitionResult }
}
interface Recognition {
  lang: string
  continuous: boolean
  interimResults: boolean
  maxAlternatives: number
  onresult: ((e: RecognitionEvent) => void) | null
  onerror: ((e: { error: string; message?: string }) => void) | null
  onend: (() => void) | null
  onstart: (() => void) | null
  start(): void
  stop(): void
  abort(): void
}
type RecognitionCtor = new () => Recognition

const Ctor: RecognitionCtor | undefined =
  typeof window === 'undefined'
    ? undefined
    : ((window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor }).SpeechRecognition ??
      (window as unknown as { webkitSpeechRecognition?: RecognitionCtor }).webkitSpeechRecognition)

export const browserSttSupported = Boolean(Ctor)

export type BrowserSttStatus = 'off' | 'listening' | 'restarting' | 'error'

/** Errors that a restart cannot fix. */
const FATAL = new Set(['not-allowed', 'service-not-allowed', 'audio-capture', 'language-not-supported'])
const DESCRIBE: Record<string, string> = {
  'not-allowed': 'Microphone permission was denied for speech recognition.',
  'service-not-allowed': 'This browser does not allow speech recognition here (needs HTTPS, Chrome/Edge/Safari).',
  'audio-capture': 'No microphone available for speech recognition.',
  'language-not-supported': 'The session language is not supported by this browser’s speech recognition.',
  network: 'Speech recognition lost its connection; retrying…',
}

export interface UseBrowserStt {
  supported: boolean
  status: BrowserSttStatus
  error: string | null
}

/**
 * While `enabled`, run continuous recognition and call `onText(text, isFinal)`: interim text is the
 * whole current utterance (cumulative), finals are emitted once per utterance. Auto-restarts on end.
 */
export function useBrowserStt(enabled: boolean, lang: string, onText: (text: string, isFinal: boolean) => void): UseBrowserStt {
  const [status, setStatus] = useState<BrowserSttStatus>('off')
  const [error, setError] = useState<string | null>(null)
  const onTextRef = useRef(onText)
  useEffect(() => {
    onTextRef.current = onText
  }, [onText])

  useEffect(() => {
    if (!enabled || !Ctor) return
    let active = true
    let failures = 0
    let timer: ReturnType<typeof setTimeout> | undefined
    let lastInterim = ''
    const rec = new Ctor()
    rec.lang = lang
    rec.continuous = true
    rec.interimResults = true
    rec.maxAlternatives = 1

    rec.onstart = () => {
      setStatus('listening')
    }
    rec.onresult = (e) => {
      failures = 0
      setError(null)
      let interim = ''
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const result = e.results[i]
        const text = result[0]?.transcript ?? ''
        if (result.isFinal) {
          if (text.trim()) onTextRef.current(text.trim(), true)
          lastInterim = ''
        } else {
          interim += text
        }
      }
      interim = interim.trim()
      if (interim && interim !== lastInterim) {
        lastInterim = interim
        onTextRef.current(interim, false)
      }
    }
    rec.onerror = (e) => {
      if (e.error === 'no-speech' || e.error === 'aborted') return // benign: onend restarts
      failures++
      if (FATAL.has(e.error)) active = false
      setError(DESCRIBE[e.error] ?? `Speech recognition error: ${e.error}`)
      if (!active) setStatus('error')
    }
    rec.onend = () => {
      if (!active) return
      // Chrome ends continuous recognition after silence or ~60 s; restart (slower after repeated failures).
      setStatus('restarting')
      timer = setTimeout(
        () => {
          if (!active) return
          try {
            rec.start()
          } catch {
            // already started
          }
        },
        Math.min(5000, 250 * 2 ** failures),
      )
    }
    try {
      rec.start()
    } catch (e) {
      timer = setTimeout(() => {
        setStatus('error')
        setError(e instanceof Error ? e.message : 'Could not start speech recognition.')
      })
    }

    return () => {
      active = false
      clearTimeout(timer)
      rec.onend = null
      rec.onresult = null
      rec.onerror = null
      try {
        rec.abort()
      } catch {
        // not running
      }
      setStatus('off')
    }
  }, [enabled, lang])

  return { supported: browserSttSupported, status, error }
}
