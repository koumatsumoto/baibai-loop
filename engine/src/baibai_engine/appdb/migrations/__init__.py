"""Numbered, forward-only application database migrations."""

from __future__ import annotations

from dataclasses import dataclass

from baibai_engine.appdb.schema import APPLICATION_SCHEMA_VERSION


@dataclass(frozen=True)
class Migration:
    version: int
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        statements=(
            """
            CREATE TABLE app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) STRICT
            """,
        ),
    ),
    Migration(
        version=2,
        statements=(
            """
            CREATE TABLE task (
                task_id TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK (status IN ('open', 'done', 'dropped')),
                kind TEXT NOT NULL CHECK (
                    kind IN ('earnings-review', 'ops', 'follow-up', 'other')
                ),
                ticker TEXT,
                due_date TEXT NOT NULL,
                event_date TEXT,
                created_at TEXT NOT NULL,
                closed_at TEXT,
                payload TEXT NOT NULL CHECK (json_valid(payload))
            ) STRICT
            """,
            "CREATE INDEX task_status_due_idx ON task(status, due_date, task_id)",
            "CREATE INDEX task_ticker_idx ON task(ticker) WHERE ticker IS NOT NULL",
        ),
    ),
    Migration(
        version=3,
        statements=(
            """
            CREATE TABLE macro_context (
                context_id TEXT PRIMARY KEY,
                as_of TEXT NOT NULL,
                valid_until TEXT NOT NULL,
                published_at TEXT NOT NULL,
                supersedes_id TEXT REFERENCES macro_context(context_id),
                payload TEXT NOT NULL CHECK (json_valid(payload)),
                CHECK (valid_until >= as_of)
            ) STRICT
            """,
            """
            CREATE TABLE macro_context_head (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                context_id TEXT NOT NULL REFERENCES macro_context(context_id)
            ) STRICT
            """,
            "CREATE INDEX macro_context_asof_idx ON macro_context(as_of, published_at, context_id)",
        ),
    ),
    Migration(
        version=4,
        statements=(
            """
            CREATE TABLE reviewed_shortlist (
                shortlist_id TEXT PRIMARY KEY,
                selection_id TEXT NOT NULL,
                run_revision_id TEXT NOT NULL,
                as_of TEXT NOT NULL,
                published_at TEXT NOT NULL,
                payload TEXT NOT NULL CHECK (json_valid(payload))
            ) STRICT
            """,
            "CREATE INDEX reviewed_shortlist_asof_idx ON reviewed_shortlist(as_of, published_at)",
            "CREATE INDEX reviewed_shortlist_selection_idx ON reviewed_shortlist(selection_id)",
        ),
    ),
    Migration(
        version=5,
        statements=(
            """
            CREATE TABLE research_packet (
                packet_id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                as_of TEXT NOT NULL,
                recommendation TEXT NOT NULL,
                published_at TEXT NOT NULL,
                supersedes_id TEXT REFERENCES research_packet(packet_id),
                payload TEXT NOT NULL CHECK (json_valid(payload)),
                CHECK (supersedes_id IS NULL OR supersedes_id <> packet_id)
            ) STRICT
            """,
            (
                "CREATE INDEX research_packet_ticker_idx "
                "ON research_packet(ticker, as_of, published_at, packet_id)"
            ),
            """
            CREATE TABLE research_review (
                review_id TEXT PRIMARY KEY,
                packet_id TEXT NOT NULL REFERENCES research_packet(packet_id),
                reviewed_at TEXT NOT NULL,
                payload TEXT NOT NULL CHECK (json_valid(payload))
            ) STRICT
            """,
            (
                "CREATE INDEX research_review_packet_idx "
                "ON research_review(packet_id, reviewed_at, review_id)"
            ),
            """
            CREATE TABLE holding_review (
                holding_review_id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                as_of TEXT NOT NULL,
                packet_id TEXT NOT NULL REFERENCES research_packet(packet_id),
                candidate_packet_id TEXT REFERENCES research_packet(packet_id),
                payload TEXT NOT NULL CHECK (json_valid(payload)),
                CHECK (candidate_packet_id IS NULL OR candidate_packet_id <> packet_id)
            ) STRICT
            """,
            (
                "CREATE INDEX holding_review_ticker_idx "
                "ON holding_review(ticker, as_of, holding_review_id)"
            ),
            "CREATE INDEX holding_review_packet_idx ON holding_review(packet_id)",
        ),
    ),
    Migration(
        version=6,
        statements=(
            """
            CREATE TABLE operation_session (
                operation_id TEXT PRIMARY KEY,
                session_kind TEXT NOT NULL CHECK (
                    session_kind IN (
                        'opportunity',
                        'pending-result',
                        'monthly-contribution',
                        'earnings-material-event',
                        'annual-outcome',
                        'improvement'
                    )
                ),
                status TEXT NOT NULL CHECK (status IN ('active', 'completed')),
                as_of TEXT NOT NULL,
                ticker TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                payload TEXT NOT NULL CHECK (json_valid(payload)),
                CHECK (
                    (status = 'active' AND completed_at IS NULL)
                    OR (status = 'completed' AND completed_at IS NOT NULL)
                )
            ) STRICT
            """,
            (
                "CREATE UNIQUE INDEX operation_session_one_active_idx "
                "ON operation_session(status) WHERE status = 'active'"
            ),
            (
                "CREATE INDEX operation_session_kind_completed_idx "
                "ON operation_session(session_kind, completed_at, operation_id)"
            ),
            """
            CREATE TRIGGER operation_session_completed_no_update
            BEFORE UPDATE ON operation_session
            WHEN OLD.status = 'completed'
            BEGIN
                SELECT RAISE(ABORT, 'completed operation session is immutable');
            END
            """,
            """
            CREATE TRIGGER operation_session_completed_no_delete
            BEFORE DELETE ON operation_session
            WHEN OLD.status = 'completed'
            BEGIN
                SELECT RAISE(ABORT, 'completed operation session is immutable');
            END
            """,
        ),
    ),
    Migration(
        version=7,
        statements=(
            """
            CREATE TABLE proposal (
                proposal_id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                packet_id TEXT NOT NULL REFERENCES research_packet(packet_id),
                review_id TEXT NOT NULL REFERENCES research_review(review_id),
                created_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'approved', 'deferred', 'rejected')
                ),
                decided_at TEXT,
                payload TEXT NOT NULL CHECK (json_valid(payload)),
                CHECK (
                    (status = 'pending' AND decided_at IS NULL)
                    OR (status <> 'pending' AND decided_at IS NOT NULL)
                )
            ) STRICT
            """,
            "CREATE INDEX proposal_status_created_idx ON proposal(status, created_at, proposal_id)",
            "CREATE INDEX proposal_ticker_created_idx ON proposal(ticker, created_at, proposal_id)",
        ),
    ),
    Migration(
        version=8,
        statements=(
            """
            CREATE TABLE ledger_event (
                append_seq INTEGER PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE,
                occurred_at TEXT NOT NULL,
                same_instant_order INTEGER NOT NULL CHECK (same_instant_order >= 0),
                event_type TEXT NOT NULL CHECK (
                    event_type IN (
                        'opening_balance', 'contribution', 'withdrawal',
                        'reservation', 'release', 'execution', 'income',
                        'cost', 'tax_confirmed'
                    )
                ),
                ticker TEXT,
                proposal_id TEXT REFERENCES proposal(proposal_id),
                payload TEXT NOT NULL CHECK (json_valid(payload)),
                UNIQUE (occurred_at, same_instant_order)
            ) STRICT
            """,
            (
                "CREATE INDEX ledger_event_replay_idx "
                "ON ledger_event(occurred_at, same_instant_order, append_seq)"
            ),
            (
                "CREATE INDEX ledger_event_proposal_idx "
                "ON ledger_event(proposal_id) WHERE proposal_id IS NOT NULL"
            ),
            """
            CREATE TABLE ledger_market_price (
                ticker TEXT PRIMARY KEY,
                observed_at TEXT NOT NULL,
                price_yen TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                price_basis TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                payload TEXT NOT NULL CHECK (json_valid(payload))
            ) STRICT
            """,
            """
            CREATE TABLE ledger_meta (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL,
                portfolio_scope TEXT NOT NULL CHECK (portfolio_scope = 'repository_only'),
                as_of TEXT NOT NULL,
                estimated_exit_tax_rate_bps INTEGER,
                estimated_exit_tax_basis TEXT,
                payload TEXT NOT NULL CHECK (json_valid(payload)),
                CHECK (
                    estimated_exit_tax_rate_bps IS NULL
                    OR estimated_exit_tax_rate_bps BETWEEN 0 AND 10000
                )
            ) STRICT
            """,
        ),
    ),
    Migration(
        version=9,
        statements=(
            """
            CREATE TABLE portfolio_outcome (
                outcome_id TEXT PRIMARY KEY,
                horizon TEXT NOT NULL CHECK (horizon IN ('1y', '3y', '5y')),
                period_start_date TEXT NOT NULL,
                period_end_date TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('resolved', 'unresolved')),
                payload TEXT NOT NULL CHECK (json_valid(payload)),
                CHECK (period_end_date >= period_start_date)
            ) STRICT
            """,
            (
                "CREATE INDEX portfolio_outcome_period_idx "
                "ON portfolio_outcome(period_end_date, horizon, outcome_id)"
            ),
        ),
    ),
    # Domain vocabulary: the per-security judgment artifact is the thesis, the Research Gate
    # gate output is the shortlist, and the pre-cap rank pool is the longlist.
    # Row data is preserved; ID string values in existing rows stay opaque and are
    # not rewritten. Completed operation_session payloads are immutable final
    # records and keep their historical keys.
    Migration(
        version=10,
        statements=(
            "ALTER TABLE reviewed_shortlist RENAME TO shortlist",
            "DROP INDEX reviewed_shortlist_asof_idx",
            "CREATE INDEX shortlist_asof_idx ON shortlist(as_of, published_at)",
            "DROP INDEX reviewed_shortlist_selection_idx",
            "CREATE INDEX shortlist_selection_idx ON shortlist(selection_id)",
            """
            UPDATE shortlist SET payload = json_set(payload, '$.kind', 'shortlist')
            WHERE json_extract(payload, '$.kind') = 'reviewed-shortlist'
            """,
            "ALTER TABLE research_packet RENAME TO thesis",
            "ALTER TABLE thesis RENAME COLUMN packet_id TO thesis_id",
            "DROP INDEX research_packet_ticker_idx",
            "CREATE INDEX thesis_ticker_idx ON thesis(ticker, as_of, published_at, thesis_id)",
            "ALTER TABLE research_review RENAME TO thesis_review",
            "ALTER TABLE thesis_review RENAME COLUMN packet_id TO thesis_id",
            "DROP INDEX research_review_packet_idx",
            (
                "CREATE INDEX thesis_review_thesis_idx "
                "ON thesis_review(thesis_id, reviewed_at, review_id)"
            ),
            """
            UPDATE thesis_review SET payload = json_remove(
                json_insert(
                    payload,
                    '$.reviewed_thesis_sha256',
                    json_extract(payload, '$.reviewed_packet_sha256')
                ),
                '$.reviewed_packet_sha256'
            )
            WHERE json_type(payload, '$.reviewed_packet_sha256') IS NOT NULL
            """,
            "ALTER TABLE holding_review RENAME COLUMN packet_id TO thesis_id",
            "ALTER TABLE holding_review RENAME COLUMN candidate_packet_id TO candidate_thesis_id",
            "DROP INDEX holding_review_packet_idx",
            "CREATE INDEX holding_review_thesis_idx ON holding_review(thesis_id)",
            # Key-presence guards use json_type (SQL NULL only when the path is
            # absent) so keys holding JSON null are renamed too: readers validate
            # payloads with extra="forbid" and would reject leftover legacy keys.
            """
            UPDATE holding_review SET payload = json_remove(
                json_insert(
                    payload,
                    '$.sources.holding_thesis',
                    CASE
                        WHEN json_type(payload, '$.sources.holding_packet')
                            IN ('object', 'array')
                        THEN json(json_extract(payload, '$.sources.holding_packet'))
                        ELSE json_extract(payload, '$.sources.holding_packet')
                    END
                ),
                '$.sources.holding_packet'
            )
            WHERE json_type(payload, '$.sources.holding_packet') IS NOT NULL
            """,
            """
            UPDATE holding_review SET payload = json_remove(
                json_insert(
                    payload,
                    '$.sources.candidate_thesis',
                    CASE
                        WHEN json_type(payload, '$.sources.candidate_packet')
                            IN ('object', 'array')
                        THEN json(json_extract(payload, '$.sources.candidate_packet'))
                        ELSE json_extract(payload, '$.sources.candidate_packet')
                    END
                ),
                '$.sources.candidate_packet'
            )
            WHERE json_type(payload, '$.sources.candidate_packet') IS NOT NULL
            """,
            "ALTER TABLE proposal RENAME COLUMN packet_id TO thesis_id",
        ),
    ),
    Migration(
        version=11,
        statements=(
            # The macro context contract drops the author-declared shelf life
            # (`valid_until`) and records the contract version each revision was
            # written under, so reads can serve only what the current contract can
            # express. Both changes need a table rebuild: `valid_until` carries a
            # CHECK constraint, and foreign keys stay enforced inside the migration
            # transaction, so the head table is carried across in step with it.
            """
            CREATE TABLE macro_context_next (
                context_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                as_of TEXT NOT NULL,
                published_at TEXT NOT NULL,
                supersedes_id TEXT REFERENCES macro_context_next(context_id),
                payload TEXT NOT NULL CHECK (json_valid(payload))
            ) STRICT
            """,
            # SQLite checks immediate foreign keys at statement end, so the whole
            # copy lands before the self reference is verified; the ordering is only
            # there to keep the rows readable in publication order. The version is cast
            # because a non-integer in the payload would otherwise abort the migration
            # with no forward path.
            """
            INSERT INTO macro_context_next (
                context_id, schema_version, as_of, published_at, supersedes_id, payload
            )
            SELECT
                context_id,
                CAST(COALESCE(json_extract(payload, '$.schema_version'), 0) AS INTEGER),
                as_of,
                published_at,
                supersedes_id,
                payload
            FROM macro_context
            ORDER BY published_at, context_id
            """,
            """
            CREATE TABLE macro_context_head_next (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                context_id TEXT NOT NULL REFERENCES macro_context_next(context_id)
            ) STRICT
            """,
            """
            INSERT INTO macro_context_head_next (singleton, context_id)
            SELECT singleton, context_id FROM macro_context_head
            """,
            "DROP TABLE macro_context_head",
            "DROP TABLE macro_context",
            "ALTER TABLE macro_context_next RENAME TO macro_context",
            "ALTER TABLE macro_context_head_next RENAME TO macro_context_head",
            "CREATE INDEX macro_context_asof_idx ON macro_context(as_of, published_at, context_id)",
        ),
    ),
    Migration(
        version=12,
        statements=(
            # 1 opportunity cycle の統合判断。proposal を作らないサイクルにも成立
            # するので、per-ticker の proposal ではなく per-cycle の immutable
            # revision として持つ。数値は thesis / proposal から再導出できるため、
            # 索引に出すのは判断の所在 — 結果・基準日・source shortlist — だけにする。
            """
            CREATE TABLE bargain_assessment (
                assessment_id TEXT PRIMARY KEY,
                as_of TEXT NOT NULL,
                published_at TEXT NOT NULL,
                result TEXT NOT NULL CHECK (
                    result IN ('proposal', 'no_actionable_bargain', 'defer')
                ),
                shortlist_id TEXT NOT NULL REFERENCES shortlist(shortlist_id),
                payload TEXT NOT NULL CHECK (json_valid(payload))
            ) STRICT
            """,
            """
            CREATE INDEX bargain_assessment_asof_idx
            ON bargain_assessment(as_of, published_at, assessment_id)
            """,
        ),
    ),
    Migration(
        version=13,
        statements=(
            # 基盤・手法の改善は operation session を使わず、self-contained issue →
            # PR delivery で回す。session はどの kind でも同時に 1 件しか active に
            # できない排他資源なので、使わない kind が開始できる状態は投資判断の
            # trigger を締め出せることを意味する。CHECK からも外して、SQL を直に
            # 書いても作れないようにする。
            """
            CREATE TABLE operation_session_next (
                operation_id TEXT PRIMARY KEY,
                session_kind TEXT NOT NULL CHECK (
                    session_kind IN (
                        'opportunity',
                        'pending-result',
                        'monthly-contribution',
                        'earnings-material-event',
                        'annual-outcome'
                    )
                ),
                status TEXT NOT NULL CHECK (status IN ('active', 'completed')),
                as_of TEXT NOT NULL,
                ticker TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                payload TEXT NOT NULL CHECK (json_valid(payload)),
                CHECK (
                    (status = 'active' AND completed_at IS NULL)
                    OR (status = 'completed' AND completed_at IS NOT NULL)
                )
            ) STRICT
            """,
            """
            INSERT INTO operation_session_next (
                operation_id, session_kind, status, as_of, ticker,
                started_at, completed_at, payload
            )
            SELECT operation_id, session_kind, status, as_of, ticker,
                   started_at, completed_at, payload
            FROM operation_session
            """,
            # DROP TABLE は DELETE trigger を発火しないので、completed row の
            # immutability trigger は移送を妨げない。index と trigger は table と
            # 一緒に落ちるため、同じ名前で作り直す。
            "DROP TABLE operation_session",
            "ALTER TABLE operation_session_next RENAME TO operation_session",
            (
                "CREATE UNIQUE INDEX operation_session_one_active_idx "
                "ON operation_session(status) WHERE status = 'active'"
            ),
            (
                "CREATE INDEX operation_session_kind_completed_idx "
                "ON operation_session(session_kind, completed_at, operation_id)"
            ),
            """
            CREATE TRIGGER operation_session_completed_no_update
            BEFORE UPDATE ON operation_session
            WHEN OLD.status = 'completed'
            BEGIN
                SELECT RAISE(ABORT, 'completed operation session is immutable');
            END
            """,
            """
            CREATE TRIGGER operation_session_completed_no_delete
            BEFORE DELETE ON operation_session
            WHEN OLD.status = 'completed'
            BEGIN
                SELECT RAISE(ABORT, 'completed operation session is immutable');
            END
            """,
        ),
    ),
    Migration(
        version=14,
        statements=(
            # published thesis の core hash を、モデルから毎回導出するのではなく publish
            # 時に焼き込む。導出のままだと schema へ field を足し引きするたびに published
            # thesis の hash が動き、その thesis に束縛された review・proposal・holding
            # review・bargain assessment・price watch が同時に読めなくなる。hash は
            # 「publish された時点で何が書かれていたか」の事実であってモデルの性質ではない。
            #
            # 既存行の値は `thesis_review.reviewed_thesis_sha256` から取る。review が
            # 束縛している hash がまさに「publish された時点の identity」であり、これが
            # 唯一の出どころである。**現在の導出と一致することを当てにしない** — 特例を
            # 外した時点で導出は過去の hash を再現しなくなっており、それこそがこの列を
            # 置く理由である。review の無い thesis は promote が作れないので backfill は
            # 全行を埋めるが、値が入らなかった行を読み手が黙って通さないよう、欠損は
            # `require_recorded_identity` が名指しで拒否する。
            #
            # 列の追加で済ませ、table を作り直さない。`thesis` は `thesis_review` /
            # `holding_review` / `proposal` から参照されており、作り直すとその 4 表すべてを
            # 移送することになる。得られるのは NOT NULL 制約 1 つで、writer は
            # application service だけなので割に合わない。
            "ALTER TABLE thesis ADD COLUMN core_sha256 TEXT",
            """
            UPDATE thesis SET core_sha256 = (
                SELECT json_extract(r.payload, '$.reviewed_thesis_sha256')
                FROM thesis_review r WHERE r.thesis_id = thesis.thesis_id
            )
            """,
            # 記録した identity は payload の内容を検査しない。導出だった頃は hash が
            # 内容の checksum を兼ねていたので、publish 後に payload を書き換えれば
            # 「review が束縛していない内容」として落ちた。その検査が無くなる分を、
            # payload そのものを変更不能にすることで置き換える — completed operation
            # session と同じ形である。`core_sha256` の書き込みはこの trigger の対象外
            # なので、上の backfill も将来の列追加も妨げない。
            """
            CREATE TRIGGER thesis_payload_immutable
            BEFORE UPDATE OF payload ON thesis
            WHEN OLD.payload <> NEW.payload
            BEGIN
                SELECT RAISE(ABORT, 'published thesis payload is immutable');
            END
            """,
        ),
    ),
    Migration(
        version=15,
        statements=(
            # `payload.handoff` は model から外れたが、保存済みの 10 行はまだ持っている。
            # `OperationPayload` は `extra="forbid"` なので、履歴を 1 行でも読むと
            # `operation show` が全件で落ちる。session 規約が求める「開始時に active を
            # 確認する」ができない状態だった。
            #
            # 読み取り側を寛容にする案は採らない。model から field を外すたびに読み取り
            # 経路へ分岐が積もり、落とした内容が観測されないまま消える。移行は 1 度で
            # 終わり、後の経路に何も残さない。
            #
            # 内容を持つ行は現行 model の `result` へ畳む。`handoff` が記録していたのは
            # 発注提案の可否と理由で、`result` が同じ役割を持つ。
            "DROP TRIGGER IF EXISTS operation_session_completed_no_update",
            """
            UPDATE operation_session
            SET payload = json_set(
                json_remove(payload, '$.handoff'),
                '$.result',
                json_extract(payload, '$.handoff.order_proposal')
                || ': '
                || COALESCE(json_extract(payload, '$.handoff.reason'), '')
            )
            WHERE json_extract(payload, '$.handoff') IS NOT NULL
              AND json_extract(payload, '$.result') IS NULL
            """,
            # 値の無い `handoff` は key を落とすだけでよい。`result` が既に埋まっている
            # 行も同じで、記録済みの結論を上書きしない。
            """
            UPDATE operation_session
            SET payload = json_remove(payload, '$.handoff')
            WHERE json_type(payload, '$.handoff') IS NOT NULL
            """,
            """
            CREATE TRIGGER operation_session_completed_no_update
            BEFORE UPDATE ON operation_session
            WHEN OLD.status = 'completed'
            BEGIN
                SELECT RAISE(ABORT, 'completed operation session is immutable');
            END
            """,
        ),
    ),
    Migration(
        version=16,
        statements=(
            # v1 が作った `app_meta` は 1 行も書かれたことがない。schema 版は
            # `PRAGMA user_version` が持ち、他の meta も専用 table を使っている。
            # 読み書きの経路が無い table は、次に読む人へ「ここに版が入る」と
            # 誤った契約を示すだけになる。
            "DROP TABLE app_meta",
        ),
    ),
)

LATEST_VERSION = APPLICATION_SCHEMA_VERSION
assert MIGRATIONS[-1].version == LATEST_VERSION

__all__ = ["LATEST_VERSION", "MIGRATIONS", "Migration"]
