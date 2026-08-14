# Macro World Model experimental contracts

This directory holds the reversible Stage A workspace tooling. It does not define a
production store, API, or second macro head.

## Version 2 honesty contract

New cycles use version 2 for `evidence-snapshot.json`, `world-model.yaml`, and
`v4-projection.yaml`. Version 1 is accepted only for the exact frozen cycle 1 and cycle
2 workspace digests, so changing a schema number cannot downgrade a new cycle out of
the honesty gate.

Evidence Snapshot v2 copies these public `macro reading` fields into every
`coverage_scan` row in addition to the raw value and unit:

- `window_years`, `window_observations`, `expected_observations`
- `statistic`, `statistic_unit`, `statistic_value`
- `next_print_estimate`, `print_due_in_days`

The builder rejects a reading row that omits any of them. A legitimate null is retained
as null; absence and null are not collapsed. The final validator also checks their
types, finite numeric values, allowed statistic, and the next-print date arithmetic
against the snapshot `as_of`. Snapshot, world model, and charter `as_of` must agree.

World Model v2 adds `state_ids`, `hypothesis_ids`, `node_ids`, `edge_ids`, and
`evidence_ids` to exactly three scenarios. Referenced edges must have both endpoints in
the same `node_ids` list. Key judgments use the same closed-subgraph rule.

## Forward projection

`v4-projection.yaml` v2 keeps scenario probability mapping and adds material claim
lineage. The shape of one claim is:

```yaml
- claim_id: demand-divergence
  summary: Household demand and investment demand diverge.
  upstream:
    state_ids: [us-demand]
    hypothesis_ids: [capex-offset]
    judgment_ids: [kj-demand-divergence]
    scenario_ids: [capex-plateau]
    node_ids: [n-household-demand, n-capex]
    edge_ids: [e-demand-divergence]
    evidence:
      - evidence_id: series:us.core_capex_orders
        measure: statistic_value
        units: [percent]
  source_qualifier:
    claim_strength: plausible
    uncertainty: [data, state, structural, policy]
    basis:
      real_nominal: nominal
      stock_flow: flow
      observation_expectation: observation
    units: [percent]
  targets:
    - artifact: v4
      field_path: /summary
      output_qualifier:
        claim_strength: plausible
        uncertainty: [data, state, structural, policy]
        basis:
          real_nominal: nominal
          stock_flow: flow
          observation_expectation: observation
        units: [percent]
```

`field_path` is an RFC 6901 JSON Pointer. V4 pointers resolve against the final v4 YAML.
The v4 document must first satisfy the canonical `MacroContextDocument` model. Its
judgment surface is then collected from synthesis, every non-fact core output (including
regime comparison and monitoring), and connection while source/identity fields are
excluded. One-page pointers address the fields rendered from the frozen model and
revision diff:
key judgments and their horizons, baseline path items, scenario rank/name/shock/
propagation/policy, unresolved tensions, signposts, and revision changes. Rendering and
lineage use the same structured render plan, including the no-change fallback.

Each claim must name at least one state, hypothesis, node, and edge in addition to its
evidence; judgment and scenario references may be empty only when the target is not one
of those consumers. Its evidence must overlap the evidence on every referenced state,
hypothesis, judgment, scenario, and edge, so typed references cannot be attached to an
unrelated source list.

For snapshot evidence, `measure` is a finite field path and its units are exact:
`latest_value` uses raw `unit`; `statistic_value` uses `statistic_unit`; percentile and
z-score use `dimensionless`; window fields use `years` / `observations`; observed and
next-print dates use `date`; due days use `days`. Release-change and revision paths use
their stored raw / percent / date units. A null or absent measure cannot be cited.
External evidence uses its verified unit and a descriptive measure; prose without a
quantity uses `not-applicable`. `source_qualifier.units` must equal the union of units on
its evidence entries.
If an output performs a real unit conversion, its target must add an exact
`unit_transform` with `formula`, `from_units`, and `to_units`; an undeclared mismatch is
rejected.

The check rejects:

- a source claim stronger than its weakest referenced edge, or an output stronger than
  its source claim;
- omission of a source uncertainty dimension;
- changes to real/nominal, stock/flow, or observation/expectation basis;
- judgment-bearing v4 or one-page paths without a material claim;
- selected evidence absent from every material claim;
- graph nodes or edges unused by key judgments, scenarios, or final-output claims.

Run the final gate before publish:

```bash
python -m tools.experiments.macro_world_model.validate_world_model check \
  <cycle> \
  --freeze <cycle>/blind-freeze.json \
  --v4-projection <cycle>/v4-projection.yaml \
  --v4-document <cycle>/final-v4.yaml \
  --revision-diff <cycle>/revision-diff.yaml
```

For a v2 workspace, `check` requires all four final inputs shown above and rejects a v1
projection. Projection probabilities must use the 0.05 grid, sum to 1.00, and equal the
probabilities on exactly one mapped base, bear, and bull final-v4 case. An inverse mapping
may add a non-blank top-level `projection_note`. `revision-diff.yaml` must use schema 1
and the same `as_of` as the cycle. The `freeze` subcommand remains the pre-projection
validation entry point.

The validator checks declarations and finite reachability, not whether prose honestly
expresses those declarations. Independent review must still read each claim vertically
from evidence through state/hypothesis/edge to one-page and v4 output. Re-run the check
after editing the final v4 document, projection, or revision diff and immediately before
publish.
