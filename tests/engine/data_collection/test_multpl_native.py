"""Native source contracts for multpl_native."""

from __future__ import annotations

from datetime import date
from unittest import TestCase

from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.provider_fixtures import _multpl_monthly_table, _source

from baibai_engine.data_collection.providers.multpl import parse_multpl_table
from baibai_engine.data_collection.transport.context import IndicatorsProviderError


class NativeParserTests(TestCase):
    def test_multpl_retains_cells_and_source_explanation(self) -> None:
        series = _source(
            "multpl",
            "shiller-pe",
        )
        html = """<table id="datatable">
          <caption>† Estimate</caption>
          <tr><th>Date</th><th>Value</th></tr>
          <tr><td>Oct 15, 2026</td><td>41.01</td></tr>
          <tr><td>Oct 1, 2026</td><td><abbr title="Estimate">†</abbr> 40.81%</td></tr>
          <tr><td>Sep 1, 2026</td><td>null</td></tr>
        </table>
        <div id="description">Data from <a href="http://www.econ.yale.edu/~shiller/data.htm">Yale</a>.</div>"""
        rows = parse_multpl_table(series, html, start=date(2026, 9, 1), end=date(2026, 10, 15))
        self.assertEqual(list(rows), [date(2026, 10, 15), date(2026, 10, 1), date(2026, 9, 1)])
        self.assertEqual(rows[date(2026, 10, 1)][0]["value_text"], "† 40.81%")
        self.assertEqual(
            rows[date(2026, 10, 1)][0]["value_annotations"], [{"text": "†", "title": "Estimate"}]
        )
        self.assertEqual(rows[date(2026, 10, 1)][0]["caption_text"], "† Estimate")
        self.assertEqual(rows[date(2026, 9, 1)][0]["value_text"], "null")
        self.assertEqual(rows[date(2026, 10, 1)][0]["description_text"], "Data from Yale.")
        self.assertEqual(
            rows[date(2026, 10, 1)][0]["description_links"],
            [{"text": "Yale", "href": "http://www.econ.yale.edu/~shiller/data.htm"}],
        )

    def test_multpl_does_not_invent_explanation(self) -> None:
        series = _source("multpl", "shiller-pe")
        html = '<table id="datatable"><tr><td>Oct 15, 2026</td><td>41.01</td></tr></table>'
        rows = parse_multpl_table(series, html, start=date(2026, 10, 15), end=date(2026, 10, 15))
        self.assertNotIn("description_text", rows[date(2026, 10, 15)][0])
        self.assertNotIn("description_links", rows[date(2026, 10, 15)][0])
        self.assertNotIn("caption_text", rows[date(2026, 10, 15)][0])
        self.assertNotIn("value_annotations", rows[date(2026, 10, 15)][0])

    def test_multpl_rejects_missing_or_malformed_table(self) -> None:
        series = _source("multpl", "shiller-pe")
        for html, reason in (
            ("<html>no table</html>", "table missing or empty"),
            ('<table id="datatable"><tr><td>Oct 15, 2026</td></tr></table>', "exactly two"),
            (
                '<table id="datatable"><tr><td>bad date</td><td>41.01</td></tr></table>',
                "invalid table date",
            ),
        ):
            with self.subTest(html=html), self.assertRaisesRegex(IndicatorsProviderError, reason):
                parse_multpl_table(series, html, start=date(2026, 10, 1), end=date(2026, 10, 15))

    def test_multpl_completeness_checks_rows_not_values(self) -> None:
        series = _source("multpl", "shiller-pe")
        html = _multpl_monthly_table(floor=date(1871, 2, 1), latest=date(2026, 7, 1))
        html = html.replace(
            '<table id="datatable">',
            '<table id="datatable"><tr><td>Aug 3, 2026</td><td>null</td></tr>',
        )
        html = html.replace("<td>20.0</td>", "<td>null</td>", 1)
        rows = parse_multpl_table(series, html, start=date(1871, 1, 1), end=date(2026, 8, 3))
        self.assertEqual(rows[date(2026, 8, 3)][0]["value_text"], "null")
        self.assertIn(date(1871, 2, 1), rows)
        missing = html.replace("<tr><td>May 1, 2020</td><td>20.0</td></tr>", "")
        with self.assertRaisesRegex(IndicatorsProviderError, "missing monthly row.*2020-05-01"):
            parse_multpl_table(series, missing, start=date(1871, 1, 1), end=date(2026, 8, 3))

    def test_multpl_retains_same_date_current_and_monthly_rows(self) -> None:
        series = _source("multpl", "shiller-pe")
        html = (
            '<table id="datatable">'
            "<tr><td>Oct 1, 2026</td><td>41.01</td></tr>"
            "<tr><td>Oct 1, 2026</td><td>40.81</td></tr></table>"
        )
        rows = parse_multpl_table(series, html, start=date(2026, 10, 1), end=date(2026, 10, 1))
        self.assertEqual([row["value_text"] for row in rows[date(2026, 10, 1)]], ["41.01", "40.81"])
        self.assertEqual([row["table_row"] for row in rows[date(2026, 10, 1)]], [0, 1])
