from __future__ import annotations

import unittest
from datetime import date


class SnapshotFixtures(unittest.TestCase):
    _READING_CASES: tuple[tuple[str, str, date, date, float | None], ...] = (
        (
            "reads_headline_for_month",
            "the headline S&P Global Japan Manufacturing PMI picked up to 54.8 in June from 54.5 in May and signalled an improvement in operating conditions.",
            date(2026, 6, 1),
            date(2026, 6, 1),
            54.8,
        ),
        (
            "returns_none_when_month_absent",
            "the headline PMI picked up to 54.8 in June from 54.5 in May.",
            date(2026, 3, 1),
            date(2026, 6, 1),
            None,
        ),
        (
            # Services releases phrase the value as "the headline index posted X in
            # Month" — the value sentence names no "PMI", and a definitional
            # "the headline figure is ..." sentence comes first.
            "reads_services_headline_without_pmi_token",
            "The headline figure is the Services Business Activity Index, which tracks changes in the volume of business activity. A reading above 50.0 indicates growth. The headline index posted 53.2 in November, up fractionally from 53.1 in October and signalled a further solid expansion.",
            date(2025, 11, 1),
            date(2025, 11, 1),
            53.2,
        ),
        (
            # Older releases drop "the headline" and lead with the index name.
            "reads_index_anchored_statement_without_headline",
            "The seasonally adjusted S&P Global US Services PMI® Business Activity Index posted 52.9 in January, down markedly from 56.8 in December.",
            date(2025, 1, 1),
            date(2025, 1, 1),
            52.9,
        ),
        (
            # "posted at the neutral level of 50.0 in October" — a qualifier sits
            # between the reporting verb and the number.
            "reads_value_with_qualifier_between_verb_and_number",
            "The seasonally adjusted S&P Global US Manufacturing Purchasing Managers' Index™ (PMI) posted at the neutral level of 50.0 in October, in line with the earlier flash estimate.",
            date(2023, 10, 1),
            date(2023, 10, 1),
            50.0,
        ),
        (
            # A release states the month before it as well as its own, which is how a
            # month whose own release is unavailable is read.
            "reads_the_previous_month_named_in_a_restating_release",
            "The headline index posted 53.2 in November, up fractionally from 53.1 in October and signalled a further solid expansion.",
            date(2025, 10, 1),
            date(2025, 11, 1),
            53.1,
        ),
        (
            # The comparison names no month, so it can only be read for the month before
            # the one the release reports.
            "reads_the_previous_month_from_a_bare_comparison",
            "The seasonally adjusted S&P Global US Manufacturing Purchasing Managers’ Index™ (PMI®) remained above the crucial 50.0 no-change mark in March. Recording 50.2, down from 52.7, the PMI signaled a marginal improvement.",
            date(2025, 2, 1),
            date(2025, 3, 1),
            52.7,
        ),
        (
            # The statement introduces its reading without naming a month, so it belongs
            # to the release's own month and must not answer for the month before it.
            "does_not_attribute_a_release_reading_to_the_previous_month",
            "The headline index posted 53.2 in November, ending a soft patch.",
            date(2025, 10, 1),
            date(2025, 11, 1),
            None,
        ),
        (
            # pypdf renders "47.9" as "47 .9" in some releases.
            "reads_a_value_the_pdf_text_split_at_the_decimal",
            "The PMI fell to 47 .9 in August, from 49.0 in July, indicating a downturn.",
            date(2023, 8, 1),
            date(2023, 8, 1),
            47.9,
        ),
        (
            # "in <month> to <value>" — the month leads the value in the same clause.
            "reads_a_month_stated_before_its_value",
            "The seasonally adjusted S&P Global US Services PMI ® Business Activity Index fell for the third month running in April to 51.3 from 51.7 in March.",
            date(2024, 4, 1),
            date(2024, 4, 1),
            51.3,
        ),
        (
            # "during <month>", and the flash estimate in the same sentence is provisional.
            "reads_a_value_stated_during_the_month",
            "The S&P Global US Services PMI® Business Activity Index recorded 53.7 during May, which was stronger than the earlier 'flash' reading of 52.3.",
            date(2025, 5, 1),
            date(2025, 5, 1),
            53.7,
        ),
        (
            # The statement sentence gives only the no-change threshold; the reading
            # follows in the next sentence.
            "reads_the_level_stated_after_the_threshold_sentence",
            "The headline au Jibun Bank Japan Services Business Activity Index remained above the 50.0 no-change mark for the fourteenth successive month in October, signalling a further expansion. That said, at 51.6 the index was down from 53.8 in September and pointed to a modest rise in output.",
            date(2023, 10, 1),
            date(2023, 10, 1),
            51.6,
        ),
        (
            # "slipped from <previous> in <previous month> to <reading>".
            "reads_a_movement_destination_stated_after_a_comparison",
            "However, the headline index slipped from 53.2 in November to 51.6, to signal a modest rate of growth that was the slowest seen since May.",
            date(2025, 12, 1),
            date(2025, 12, 1),
            51.6,
        ),
        (
            # "reaching a 33-month high of <reading> following a reading of <previous>".
            "reads_a_reading_named_as_a_record_level",
            "The seasonally adjusted S&P Global US Services PMI® Business Activity Index rose for the second month running in December, reaching a 33-month high of 56.8 following a reading of 56.1 in November.",
            date(2024, 12, 1),
            date(2024, 12, 1),
            56.8,
        ),
        (
            # A reading of exactly 50.0 is stated as equal to the threshold.
            "reads_a_reading_equal_to_the_no_change_mark",
            "The seasonally adjusted S&P Global US Manufacturing Purchasing Managers’ Index™ (PMI ®) posted in line with the 50.0 no-change mark in April to point to stable business conditions.",
            date(2024, 4, 1),
            date(2024, 4, 1),
            50.0,
        ),
        (
            # "broadly in line with" states an approximation, not the reading.
            "ignores_a_hedged_comparison_with_the_no_change_mark",
            "The seasonally adjusted S&P Global US Manufacturing Purchasing Managers’ Index™ (PMI ®) was broadly in line with the 50.0 no-change mark in April.",
            date(2024, 4, 1),
            date(2024, 4, 1),
            None,
        ),
        (
            # "below the 50.0 no-change mark in November" is the threshold; the reading is
            # the level stated beside it.
            "ignores_a_threshold_the_reading_is_measured_against",
            "The seasonally adjusted S&P Global US Manufacturing Purchasing Managers’ Index™ (PMI®) remained below the 50.0 no-change mark in November, but at 49.7 pointed to only a marginal worsening in the health of the sector.",
            date(2024, 11, 1),
            date(2024, 11, 1),
            49.7,
        ),
        (
            # A span average is not any single month's reading.
            "ignores_an_average_over_several_months",
            "The Business Activity Index has trended at 53.7 from January to November, comfortably above the next-highest annual average of 52.4 set in 2013.",
            date(2023, 11, 1),
            date(2023, 11, 1),
            None,
        ),
        (
            # The composite index is published in the same release and its statement reads
            # like the headline one.
            "ignores_the_composite_index_statement",
            "S&P Global US Services PMI® At 50.7 in November, the final S&P Global US Composite PMI Output Index* was unchanged from October. The headline S&P Global US Services PMI® Business Activity Index recorded 54.1 in November.",
            date(2025, 11, 1),
            date(2025, 11, 1),
            54.1,
        ),
        (
            # A sub-index moves on its own and must never answer for the headline.
            "ignores_a_sub_index_statement",
            "The headline index posted 49.7 in November, a marginal worsening. The New Orders Index rose to 48.2 in November.",
            date(2024, 11, 1),
            date(2024, 11, 1),
            49.7,
        ),
        (
            # Only a sentence that refers back to the index carries the statement on, so a
            # sentence about another subject cannot supply the headline reading.
            "ignores_a_sentence_that_moves_on_from_the_statement",
            "The headline index remained subdued in June. Employment growth eased to 51.2 in June.",
            date(2026, 6, 1),
            date(2026, 6, 1),
            None,
        ),
        (
            # "compared to <previous>" borrows the preposition a movement uses, without
            # stating the release's own reading.
            "ignores_a_comparison_sharing_the_movement_preposition",
            "The headline index improved in October, compared to 52.0 in September.",
            date(2025, 10, 1),
            date(2025, 10, 1),
            None,
        ),
        (
            # A chart caption pairs the month with a year, which is not a reading.
            "ignores_a_year_beside_the_month",
            "Comment January 2026 Index, sa, >50 = growth m/m. The headline index eased.",
            date(2026, 1, 1),
            date(2026, 1, 1),
            None,
        ),
    )
    _REFUSAL_CASES: tuple[tuple[str, str, date, date, str], ...] = (
        (
            "rejects_implausible_reading",
            "the headline PMI surged to 101.0 in June, an unprecedented reading.",
            date(2026, 6, 1),
            date(2026, 6, 1),
            "outside plausible range",
        ),
        (
            "rejects_conflicting_values",
            "the headline PMI reading was 54.8 in June. Separately, the headline PMI figure was 55.9 in June per a revised estimate.",
            date(2026, 6, 1),
            date(2026, 6, 1),
            "conflicting",
        ),
    )
