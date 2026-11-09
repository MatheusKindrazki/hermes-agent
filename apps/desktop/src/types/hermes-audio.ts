/** `POST /api/audio/tts-lease` — TTS engine warm-up / release driven by speech toggles. */
export interface AudioTtsLeaseResponse {
  ok: boolean
  lease: string
  active: boolean
  /** Live lease holders after this call (null when the backend call itself failed). */
  leases: null | number
  /** Warm-up outcome: `loaded` | `cached` | `installed` | `noop` | `error`. */
  action?: string
  provider?: string
  /** Resident local models dropped (release path). */
  released?: number
  error?: string
}

/** `POST /api/audio/stt-lease` — local STT pre-load driven by voice-input sessions. */
export interface AudioSttLeaseResponse {
  ok: boolean
  lease: string
  active: boolean
  /** Live lease holders after this call (null when the backend call itself failed). */
  leases: null | number
  /** Warm-up outcome: `loaded` | `cached` | `noop` | `error`. Release carries no action. */
  action?: string
  /** Whether the configured engine was actually warmed (`noop` for cloud providers carries false). */
  warmed?: boolean
  provider?: string
  error?: string
}

