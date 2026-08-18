import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// The types are generated from the Python read models, with the JSON Schema in between
// as the reviewable artifact. `tools/quality/drift/check_readmodel_contract.py` refuses
// a stale copy of either file, but only the Python gate sees them together — nothing on
// this side would notice a schema whose roots the checked-in types no longer declare.
const uiRoot = resolve(import.meta.dirname, '..')
const repoRoot = resolve(uiRoot, '../..')
const schema = JSON.parse(
  readFileSync(resolve(repoRoot, 'web/contracts/read-model.schema.json'), 'utf8'),
) as { roots: Record<string, string>; $defs: Record<string, unknown> }
const types = readFileSync(resolve(uiRoot, 'src/api/types.ts'), 'utf8')

function declaredNames(source: string): Set<string> {
  return new Set(
    [...source.matchAll(/^export (?:interface|type) (\w+)/gm)].map(([, name]) => name),
  )
}

describe('read-model contract', () => {
  it('declares a type for every view the routes are served from', () => {
    const declared = declaredNames(types)
    const roots = Object.values(schema.roots).map((reference) =>
      reference.replace('#/$defs/', ''),
    )

    expect(roots.length).toBeGreaterThan(10)
    expect(roots.filter((name) => !declared.has(name))).toEqual([])
  })

  it('declares a type for every definition the schema carries', () => {
    const declared = declaredNames(types)

    expect(Object.keys(schema.$defs).filter((name) => !declared.has(name))).toEqual([])
  })

  it('keeps the run summary out of the generated file', () => {
    // `system/latest-run.json` is written by the daily batch, not by the read models,
    // so its types are hand-written next door. A generated copy would go stale the
    // moment the batch changed the object.
    const runSummary = readFileSync(resolve(uiRoot, 'src/api/run-summary.ts'), 'utf8')

    expect(declaredNames(types).has('WorkflowRunSummaryView')).toBe(false)
    expect(declaredNames(runSummary).has('WorkflowRunSummaryView')).toBe(true)
  })
})
