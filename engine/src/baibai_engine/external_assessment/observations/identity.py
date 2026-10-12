"""Persisted identities retained until the explicit data cutover (refs #1414)."""

MODEL_VALUATION_TABLE = "retail_model_valuation_observations"
ANALYST_RATING_TABLE = "retail_analyst_rating_observations"
QUANTS_SCORE_TABLE = "retail_quants_score_observations"

MODEL_VALUATION_DATASET = "retail_dynamics.model_valuation_observations"
ANALYST_RATING_DATASET = "retail_dynamics.analyst_rating_observations"
QUANTS_SCORE_DATASET = "retail_dynamics.quants_score_observations"

STAGING_FORMAT = "retail-dynamics-staging-v1"
PAIR_ACQUISITION_POLICY_VERSION = "retail-pair-acquisition-v5"
