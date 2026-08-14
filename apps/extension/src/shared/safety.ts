const SECRET_PATTERNS = [
  /\bBearer\s+[A-Za-z0-9._~+/=-]+/gi,
  /\b(?:sk|rk)-[A-Za-z0-9_-]{16,}/gi,
  /\b(?:ghp|github_pat|xox[baprs])-[A-Za-z0-9-]{12,}/gi,
  /\b(?:AKIA|AIza)[A-Za-z0-9_-]{12,}/g,
  /\b(?:password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*[^\s,;]+/gi,
  /\b(?:\d[ -]*?){13,19}\b/g,
]

export function redactSensitiveText(value: string): string {
  return SECRET_PATTERNS.reduce((text, pattern) => text.replace(pattern, '[REDACTED]'), value)
}
