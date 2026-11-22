/**
 * Parses a user-typed decimal. Accepts Turkish ("285,40", "1.234,56") and English ("285.40", "1,234.56") input.
 * With both separators the last one is the decimal mark; a lone comma is a decimal comma; a lone dot is a
 * decimal point unless it repeats ("1.234.567") or is followed by exactly three digits ("1.500" = 1500, the
 * Turkish thousands form; "285.40" stays 285.4). Returns null for anything that is not a number.
 */
export function parseDecimal(input: string): number | null {
  const s = input.replace(/[\s']/g, '') // \s already covers NBSP
  if (!s || !/^[+-]?[\d.,]+$/.test(s)) return null
  const lastDot = s.lastIndexOf('.')
  const lastComma = s.lastIndexOf(',')
  let normalized: string
  if (lastDot >= 0 && lastComma >= 0) {
    const decimalMark = lastDot > lastComma ? '.' : ','
    const thousands = decimalMark === '.' ? ',' : '.'
    normalized = s.split(thousands).join('').replace(decimalMark, '.')
  } else if (lastComma >= 0) {
    if (s.indexOf(',') !== lastComma) return null // "1,234,567": ambiguous, not a plain decimal
    normalized = s.replace(',', '.')
  } else if (lastDot >= 0 && (s.indexOf('.') !== lastDot || /^[+-]?\d{1,3}\.\d{3}$/.test(s))) {
    normalized = s.split('.').join('') // "1.234.567"
  } else {
    normalized = s
  }
  if (!/^[+-]?(\d+\.?\d*|\.\d+)$/.test(normalized)) return null
  const n = Number(normalized)
  return Number.isFinite(n) ? n : null
}
