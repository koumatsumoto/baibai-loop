"""External Assessmentの現行保存identity。旧履歴の移行は明示cutoverで行う。"""

MODEL_VALUATION_TABLE = "external_assessment_model_valuation_observations"
ANALYST_RATING_TABLE = "external_assessment_analyst_rating_observations"
QUANTS_SCORE_TABLE = "external_assessment_quants_score_observations"

MODEL_VALUATION_DATASET = "external_assessment.model_valuation_observations"
ANALYST_RATING_DATASET = "external_assessment.analyst_rating_observations"
QUANTS_SCORE_DATASET = "external_assessment.quants_score_observations"

STAGING_FORMAT = "external-assessment-staging-v1"
PAIR_ACQUISITION_POLICY_VERSION = "external-assessment-pair-acquisition-v7"
