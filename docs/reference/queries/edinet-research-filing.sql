-- l1.query source alias: docs = edinet.documents (entire retained document range).
-- Parameters: ticker (four-character code), cutoff (JST date, inclusive).
-- Choose the filing BEFORE querying its facts. Never fall back after this selection.
WITH RECURSIVE origins AS (
  SELECT *, row_number() OVER (PARTITION BY doc_id ORDER BY doc_date DESC, sequence_number DESC) AS version
  FROM docs
  WHERE doc_type_code IN ('120', '130')
    AND substr(submit_datetime, 1, 10) <= $cutoff
    AND doc_date <= $cutoff
    AND coalesce(doc_info_edit_status, '') != '1'
    AND coalesce(withdrawal_status, '') != '1'
    AND coalesce(disclosure_status, '') NOT IN ('1', '3')
), families AS (
  SELECT doc_id, doc_id AS root_id, period_end AS annual_period_end
  FROM origins WHERE version = 1 AND doc_type_code = '120'
  UNION
  SELECT c.doc_id, p.root_id, p.annual_period_end
  FROM origins c JOIN families p ON c.parent_doc_id = p.doc_id
  WHERE c.version = 1 AND c.doc_type_code = '130'
), unresolved_annual_events AS (
  SELECT coalesce(e.sec_code, linked.sec_code) AS sec_code,
    greatest(coalesce(e.doc_date, ''), coalesce(substr(e.submit_datetime, 1, 10), '')) AS known_on
  FROM docs e LEFT JOIN origins linked
    ON linked.version = 1 AND (e.doc_id = linked.doc_id OR e.parent_doc_id = linked.doc_id)
  WHERE e.doc_date <= $cutoff
    AND (e.doc_info_edit_status = '1' OR e.withdrawal_status = '1'
      OR e.disclosure_status IN ('1', '3'))
    AND (e.doc_type_code IN ('120', '130') OR linked.doc_id IS NOT NULL)
    AND NOT EXISTS (
      SELECT 1 FROM origins resolved WHERE resolved.version = 1
        AND resolved.doc_id = CASE WHEN e.withdrawal_status = '1'
          THEN e.parent_doc_id ELSE e.doc_id END
    )
), ranked AS (
  SELECT o.*, f.root_id, f.annual_period_end, root.submit_datetime AS root_submitted,
    row_number() OVER (ORDER BY f.annual_period_end DESC, o.submit_datetime DESC, o.doc_id DESC) AS rank
  FROM origins o JOIN families f ON o.doc_id = f.doc_id
  JOIN origins root ON root.doc_id = f.root_id AND root.version = 1
  WHERE o.version = 1 AND substr(root.sec_code, 1, 4) = $ticker
), selected AS (
  SELECT r.*,
    CASE WHEN r.legal_status IN ('1', '2') AND r.disclosure_status = '0'
      AND r.withdrawal_status = '0' AND r.doc_info_edit_status = '0' AND r.xbrl_flag = '1'
      AND r.annual_period_end IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM unresolved_annual_events e
        WHERE substr(e.sec_code, 1, 4) = $ticker
          AND (e.known_on = '' OR r.root_submitted IS NULL
            OR e.known_on >= substr(r.root_submitted, 1, 10))
      )
      AND NOT EXISTS (
        SELECT 1 FROM origins newer
        WHERE newer.version = 1 AND newer.doc_type_code = '120'
          AND substr(newer.sec_code, 1, 4) = $ticker
          AND newer.period_end IS NULL AND newer.submit_datetime >= r.root_submitted
      )
      AND NOT EXISTS (
        SELECT 1 FROM origins o JOIN families f ON o.doc_id = f.doc_id
        WHERE o.version = 1 AND f.root_id = r.root_id
          AND (o.withdrawal_status = '2' OR o.disclosure_status = '2'
            OR o.doc_info_edit_status = '2')
      )
      AND NOT EXISTS (
        SELECT 1 FROM docs e JOIN families f
          ON (e.doc_id = f.doc_id OR e.parent_doc_id = f.doc_id)
        WHERE f.root_id = r.root_id AND e.doc_date <= $cutoff
          AND (e.doc_info_edit_status = '1' OR e.withdrawal_status = '1'
            OR e.disclosure_status IN ('1', '3'))
      )
      AND NOT EXISTS (
        SELECT 1 FROM origins o LEFT JOIN families f ON o.doc_id = f.doc_id
        WHERE o.version = 1 AND substr(o.sec_code, 1, 4) = $ticker
          AND o.doc_type_code = '130' AND f.doc_id IS NULL
      )
      THEN 'usable' ELSE 'validity_unknown' END AS validity
  FROM ranked r WHERE r.rank = 1
)
SELECT doc_id AS source_doc_id, submit_datetime, annual_period_end, validity
FROM selected
