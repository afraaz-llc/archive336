/**
 * Country-name → ISO code abbreviation.
 *
 * YouTube returns localized full country names ("United States", "United
 * Kingdom"). We display the 2-letter ISO code instead. We brute-force build
 * the reverse map once using the browser's built-in Intl.DisplayNames data,
 * so it covers every country without a hardcoded list.
 */

let _cache: Record<string, string> | null = null

function buildCache(): Record<string, string> {
  if (typeof Intl === "undefined" || !Intl.DisplayNames) return {}
  const dn = new Intl.DisplayNames(["en"], { type: "region" })
  const out: Record<string, string> = {}
  for (let i = 0; i < 26; i++) {
    for (let j = 0; j < 26; j++) {
      const code =
        String.fromCharCode(65 + i) + String.fromCharCode(65 + j)
      try {
        const name = dn.of(code)
        if (name && name !== code) out[name] = code
      } catch {
        // Not a valid region code — skip.
      }
    }
  }
  return out
}

/**
 * Convert a localized country name to its 2-letter ISO code (e.g. "United
 * States" → "US"). Falls back to the input if no match is found.
 */
export function abbreviateCountry(name: string): string {
  if (!name) return ""
  if (!_cache) _cache = buildCache()
  return _cache[name] ?? name
}
