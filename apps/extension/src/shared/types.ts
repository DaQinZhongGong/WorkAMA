export type SessionState = {
  baseUrl: string
  token: string
  context: string
}

export type CaptureResult =
  | { ok: true; text: string; title?: string; url?: string }
  | { ok: false; error: string }
