// Monetary gains use gold and losses use blue. Labels and signs carry the same meaning.
export function pnlTone(value: number | null): string {
  if (value === null || value === 0) return 'text-muted-foreground'
  return value > 0 ? 'text-profit' : 'text-loss'
}
