const sentences = new Intl.Segmenter('ja', { granularity: 'sentence' })

// Excerpt the saved author's words, rather than deriving a new investment judgment.
export function reportExcerpt(text: string) {
  const parts = Array.from(sentences.segment(text))
  return parts.length > 3 ? parts.slice(0, 3).map(part => part.segment).join('').trimEnd() : text
}
