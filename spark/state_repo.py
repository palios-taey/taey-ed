"""Repository chokepoint for taey-ed state-store transitions.

All Phase-A writers go through narrow methods here. The DDL enforces structural
shape; this module enforces caller roles, evidence presence, transition intent,
and same-transaction event writes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Iterable

from spark.state_db import immediate_transaction, now_ms, state_connection

logger = logging.getLogger(__name__)

ACTORS = {"api", "worker", "mac", "operator", "supervisor", "system"}
MASTER_CATEGORIES = {"NAVIGATION", "VIDEO", "ARTICLE", "EXERCISE", "TRANSITION"}
COORD_CLEAR_REASONS = {"user_stop_abandon", "user_stop_reset", "advanced", "yaml_fold"}


class StateRepoError(RuntimeError):
    pass


class StateRepo:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path

    def resolve_or_mint(
        self,
        *,
        platform: str,
        key_kind: str,
        key_hash: str,
        actor: str,
        evidence: dict[str, Any],
        features: dict[str, Any] | None = None,
    ) -> str:
        self._require_actor(actor, {"api", "worker", "operator", "system"}, "resolve_or_mint")
        self._require_evidence(evidence, "source")
        with immediate_transaction(self.db_path) as conn:
            screen_id, minted = self._resolve_or_mint(conn, platform, key_kind, key_hash, features)
            self._record_event(
                conn,
                kind="screen_minted" if minted else "screen_resolved",
                actor=actor,
                platform=platform,
                screen_id=screen_id,
                payload={"key_kind": key_kind, "key_hash": key_hash, "evidence": evidence},
            )
            return screen_id

    def record_classification_result(
        self,
        *,
        platform: str,
        key_kind: str,
        key_hash: str,
        screen_type: str | None,
        actor: str,
        evidence: dict[str, Any],
        success: bool = True,
        features: dict[str, Any] | None = None,
        bundle: dict[str, Any] | None = None,
    ) -> str:
        self._require_actor(actor, {"api", "worker", "operator", "system"}, "classify")
        self._require_evidence(evidence, "source")
        with immediate_transaction(self.db_path) as conn:
            screen_id, _ = self._resolve_or_mint(conn, platform, key_kind, key_hash, features)
            normalized_type = self._normalize_screen_type(screen_type)
            if success and normalized_type:
                self._ensure_screen_type(conn, platform, normalized_type)
                bundle_id = self._ensure_bundle_receipt(
                    conn,
                    call_kind="classify",
                    screen_id=screen_id,
                    bundle=bundle or {"source": evidence["source"], "screen_type": normalized_type},
                )
                conn.execute(
                    """
                    UPDATE screens
                       SET screen_type=?,
                           classification='classified',
                           classified_by_bundle_id=?,
                           last_seen=?
                     WHERE screen_id=?
                    """,
                    (normalized_type, bundle_id, now_ms(), screen_id),
                )
                status = "classified"
            else:
                conn.execute(
                    """
                    UPDATE screens
                       SET classification=?,
                           screen_type=NULL,
                           classified_by_bundle_id=NULL,
                           last_seen=?
                     WHERE screen_id=?
                    """,
                    ("operator_required" if normalized_type == "UNKNOWN" else "failed_retryable", now_ms(), screen_id),
                )
                status = "operator_required" if normalized_type == "UNKNOWN" else "failed_retryable"
            self._record_event(
                conn,
                kind="classify_result",
                actor=actor,
                platform=platform,
                screen_id=screen_id,
                payload={
                    "key_kind": key_kind,
                    "key_hash": key_hash,
                    "screen_type": normalized_type,
                    "status": status,
                    "evidence": evidence,
                },
            )
            return screen_id

    def record_behavior_tree(
        self,
        *,
        platform: str,
        key_kind: str,
        key_hash: str,
        bt_json: dict[str, Any],
        built_by: str,
        source_kind: str,
        actor: str,
        evidence: dict[str, Any],
        screen_type: str | None = None,
        bundle: dict[str, Any] | None = None,
        status: str = "candidate",
        success_count: int = 0,
    ) -> str:
        self._require_actor(actor, {"api", "worker", "operator", "system"}, "record_bt")
        self._require_evidence(evidence, "source")
        if status == "validated" and success_count < 1:
            raise StateRepoError("validated behavior trees require success_count >= 1")
        with immediate_transaction(self.db_path) as conn:
            screen_id, _ = self._resolve_or_mint(conn, platform, key_kind, key_hash, None)
            if screen_type:
                self._ensure_screen_type(conn, platform, screen_type)
            bundle_id = None
            if bundle is not None:
                bundle_id = self._ensure_bundle_receipt(
                    conn,
                    call_kind="bt_build",
                    screen_id=screen_id,
                    bundle=bundle,
                )
            body = self._json(bt_json)
            existing = conn.execute(
                "SELECT bt_id FROM behavior_trees WHERE screen_id=? AND bt_json=?",
                (screen_id, body),
            ).fetchone()
            if existing:
                self._record_event(
                    conn,
                    kind="bt_seen",
                    actor=actor,
                    platform=platform,
                    screen_id=screen_id,
                    payload={"bt_id": existing["bt_id"], "source_kind": source_kind, "evidence": evidence},
                )
                return existing["bt_id"]
            revision = self._next_bt_revision(conn, screen_id)
            bt_id = self._stable_id("bt", screen_id, revision, body)
            conn.execute(
                """
                INSERT INTO behavior_trees(
                    bt_id, screen_id, revision, bt_json, built_by, source_kind,
                    bundle_id, status, success_count
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (bt_id, screen_id, revision, body, built_by, source_kind, bundle_id, status, success_count),
            )
            self._record_event(
                conn,
                kind="bt_recorded",
                actor=actor,
                platform=platform,
                screen_id=screen_id,
                payload={
                    "bt_id": bt_id,
                    "revision": revision,
                    "status": status,
                    "source_kind": source_kind,
                    "evidence": evidence,
                },
            )
            return bt_id

    def mark_behavior_tree_validated(
        self,
        *,
        bt_id: str,
        actor: str,
        evidence: dict[str, Any],
    ) -> None:
        self._require_actor(actor, {"api", "worker", "operator", "system"}, "promote")
        self._require_evidence(evidence, "source")
        with immediate_transaction(self.db_path) as conn:
            row = conn.execute(
                "SELECT screen_id FROM behavior_trees WHERE bt_id=?",
                (bt_id,),
            ).fetchone()
            if row is None:
                raise StateRepoError(f"unknown behavior tree {bt_id!r}")
            conn.execute(
                """
                UPDATE behavior_trees
                   SET status='validated',
                       success_count=success_count + 1,
                       consecutive_failures=0
                 WHERE bt_id=?
                """,
                (bt_id,),
            )
            self._record_event(
                conn,
                kind="bt_validated",
                actor=actor,
                screen_id=row["screen_id"],
                payload={"bt_id": bt_id, "evidence": evidence},
            )

    def demote_behavior_tree(
        self,
        *,
        bt_id: str,
        actor: str,
        evidence: dict[str, Any],
    ) -> None:
        self._require_actor(actor, {"api", "worker", "operator", "system"}, "demote")
        self._require_evidence(evidence, "source")
        with immediate_transaction(self.db_path) as conn:
            row = conn.execute(
                "SELECT screen_id FROM behavior_trees WHERE bt_id=?",
                (bt_id,),
            ).fetchone()
            if row is None:
                raise StateRepoError(f"unknown behavior tree {bt_id!r}")
            conn.execute(
                """
                UPDATE behavior_trees
                   SET status='demoted',
                       consecutive_failures=consecutive_failures + 1
                 WHERE bt_id=?
                """,
                (bt_id,),
            )
            self._record_event(
                conn,
                kind="bt_demoted",
                actor=actor,
                screen_id=row["screen_id"],
                payload={"bt_id": bt_id, "evidence": evidence},
            )

    def mirror_hash_mapping(
        self,
        *,
        platform: str,
        skel_hash: str,
        screen_type: str,
        actor: str,
        evidence: dict[str, Any],
        validated: bool = False,
    ) -> str:
        screen_id = self.record_classification_result(
            platform=platform,
            key_kind="skeleton",
            key_hash=skel_hash,
            screen_type=screen_type,
            actor=actor,
            evidence=evidence,
            success=bool(screen_type and screen_type != "UNKNOWN"),
        )
        if validated:
            self.promote_screen_type(
                platform=platform,
                screen_type=screen_type,
                actor=actor,
                evidence={**evidence, "validated": True},
            )
        return screen_id

    def record_cache_delete(
        self,
        *,
        platform: str,
        key_kind: str,
        key_hash: str,
        actor: str,
        evidence: dict[str, Any],
        screen_type: str | None = None,
    ) -> None:
        self._require_actor(actor, {"api", "operator", "system"}, "cache_delete")
        self._require_evidence(evidence, "source")
        with immediate_transaction(self.db_path) as conn:
            screen_id, _ = self._resolve_or_mint(conn, platform, key_kind, key_hash, None)
            self._record_event(
                conn,
                kind="cache_delete",
                actor=actor,
                platform=platform,
                screen_id=screen_id,
                payload={
                    "key_kind": key_kind,
                    "key_hash": key_hash,
                    "screen_type": screen_type,
                    "evidence": evidence,
                },
            )

    def record_session_update(
        self,
        *,
        platform: str,
        skel_hash: str,
        update_kind: str,
        actor: str,
        evidence: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        self._require_actor(actor, {"api", "worker", "mac", "system"}, "session_update")
        self._require_evidence(evidence, "source")
        with immediate_transaction(self.db_path) as conn:
            screen_id, _ = self._resolve_or_mint(conn, platform, "skeleton", skel_hash, None)
            self._record_event(
                conn,
                kind=f"screen_session_{update_kind}",
                actor=actor,
                platform=platform,
                screen_id=screen_id,
                payload={**payload, "evidence": evidence},
            )

    def record_classification_job(
        self,
        *,
        platform: str,
        skel_hash: str,
        status: str,
        actor: str,
        evidence: dict[str, Any],
        classification_id: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        self._require_actor(actor, {"api", "worker", "system"}, "classification_job")
        self._require_evidence(evidence, "source")
        with immediate_transaction(self.db_path) as conn:
            screen_id, _ = self._resolve_or_mint(conn, platform, "skeleton", skel_hash, None)
            if result is not None:
                screen_type = result.get("screen_type")
                success = (
                    bool(result.get("success", True))
                    and bool(screen_type)
                    and str(screen_type).upper() != "UNKNOWN"
                )
                if success:
                    self._ensure_screen_type(conn, platform, screen_type)
                    bundle_id = self._ensure_bundle_receipt(
                        conn,
                        call_kind="classify",
                        screen_id=screen_id,
                        bundle={"classification_id": classification_id, "result": result},
                    )
                    conn.execute(
                        """
                        UPDATE screens
                           SET screen_type=?,
                               classification='classified',
                               classified_by_bundle_id=?,
                               last_seen=?
                         WHERE screen_id=?
                        """,
                        (screen_type, bundle_id, now_ms(), screen_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE screens
                           SET classification='operator_required',
                               screen_type=NULL,
                               classified_by_bundle_id=NULL,
                               last_seen=?
                         WHERE screen_id=?
                        """,
                        (now_ms(), screen_id),
                    )
            self._record_event(
                conn,
                kind="classification_job",
                actor=actor,
                platform=platform,
                screen_id=screen_id,
                payload={
                    "classification_id": classification_id,
                    "status": status,
                    "result": result,
                    "evidence": evidence,
                },
            )

    def record_consult_status_event(
        self,
        *,
        consult_id: str,
        platform: str,
        status: str,
        actor: str,
        evidence: dict[str, Any],
        screen_hash: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self._require_actor(actor, {"api", "worker", "operator", "system"}, "consult_status")
        self._require_evidence(evidence, "source")
        with immediate_transaction(self.db_path) as conn:
            screen_id = None
            if screen_hash:
                screen_id, _ = self._resolve_or_mint(conn, platform, "skeleton", screen_hash, None)
            self._record_event(
                conn,
                kind="consult_status",
                actor=actor,
                platform=platform,
                screen_id=screen_id,
                consult_id=consult_id,
                payload={"status": status, **(payload or {}), "evidence": evidence},
            )

    def mirror_signature(
        self,
        *,
        platform: str,
        sig_hash: str,
        screen_type: str,
        signature: Iterable[Any],
        actor: str,
        evidence: dict[str, Any],
        behavior_tree: dict[str, Any] | None = None,
        extract: dict[str, Any] | None = None,
        validated: bool = False,
    ) -> str:
        screen_id = self.record_classification_result(
            platform=platform,
            key_kind="signature",
            key_hash=sig_hash,
            screen_type=screen_type,
            actor=actor,
            evidence=evidence,
            features={
                "signature": list(signature),
                "extract": extract,
            },
            success=bool(screen_type and screen_type != "UNKNOWN"),
        )
        if behavior_tree:
            self.record_behavior_tree(
                platform=platform,
                key_kind="signature",
                key_hash=sig_hash,
                bt_json=behavior_tree,
                built_by=actor,
                source_kind=evidence["source"],
                actor=actor,
                evidence=evidence,
                screen_type=screen_type,
                status="validated" if validated else "candidate",
                success_count=1 if validated else 0,
            )
        return screen_id

    def promote_screen_type(
        self,
        *,
        platform: str,
        screen_type: str,
        actor: str,
        evidence: dict[str, Any],
    ) -> None:
        self._require_actor(actor, {"api", "worker", "operator", "system"}, "promote")
        self._require_evidence(evidence, "source")
        normalized_type = self._normalize_screen_type(screen_type)
        if not normalized_type or normalized_type == "UNKNOWN":
            raise StateRepoError("cannot promote missing or UNKNOWN screen type")
        with immediate_transaction(self.db_path) as conn:
            self._ensure_screen_type(conn, platform, normalized_type)
            current = conn.execute(
                """
                SELECT validated_successes
                  FROM screen_types
                 WHERE platform=? AND screen_type=?
                """,
                (platform, normalized_type),
            ).fetchone()
            next_count = int(current["validated_successes"]) + 1
            trust = "trusted" if next_count >= 3 else "provisional"
            conn.execute(
                """
                UPDATE screen_types
                   SET validated_successes=?,
                       trust=?
                 WHERE platform=? AND screen_type=?
                """,
                (next_count, trust, platform, normalized_type),
            )
            self._record_event(
                conn,
                kind="screen_type_promoted",
                actor=actor,
                platform=platform,
                payload={"screen_type": normalized_type, "validated_successes": next_count, "trust": trust, "evidence": evidence},
            )

    def demote_screen_type(
        self,
        *,
        platform: str,
        screen_type: str,
        actor: str,
        evidence: dict[str, Any],
    ) -> None:
        self._require_actor(actor, {"api", "worker", "operator", "system"}, "demote")
        self._require_evidence(evidence, "source")
        normalized_type = self._normalize_screen_type(screen_type)
        if not normalized_type or normalized_type == "UNKNOWN":
            raise StateRepoError("cannot demote missing or UNKNOWN screen type")
        with immediate_transaction(self.db_path) as conn:
            self._ensure_screen_type(conn, platform, normalized_type)
            conn.execute(
                """
                UPDATE screen_types
                   SET trust='demoted'
                 WHERE platform=? AND screen_type=?
                """,
                (platform, normalized_type),
            )
            self._record_event(
                conn,
                kind="screen_type_demoted",
                actor=actor,
                platform=platform,
                payload={"screen_type": normalized_type, "evidence": evidence},
            )

    def record_escalation_attempt(
        self,
        *,
        platform: str,
        screen_hash: str,
        consult_id: str,
        actor: str,
        evidence: dict[str, Any],
    ) -> int:
        self._require_actor(actor, {"api", "worker", "operator", "system"}, "escalate")
        self._require_evidence(evidence, "source")
        with immediate_transaction(self.db_path) as conn:
            screen_id, _ = self._resolve_or_mint(conn, platform, "skeleton", screen_hash, None)
            self._ensure_coordination(conn, screen_id, platform)
            row = conn.execute(
                "SELECT attempt_count,last_attempt_key,terminal FROM coordination WHERE screen_id=?",
                (screen_id,),
            ).fetchone()
            if int(row["terminal"]) == 1:
                self._record_event(
                    conn,
                    kind="escalation_attempt_ignored",
                    actor=actor,
                    platform=platform,
                    screen_id=screen_id,
                    consult_id=consult_id or None,
                    payload={"reason": "terminal", "evidence": evidence},
                )
                return int(row["attempt_count"])
            if consult_id and row["last_attempt_key"] == consult_id:
                return int(row["attempt_count"])
            next_attempt = min(int(row["attempt_count"]) + 1, 4)
            tier, state, terminal = self._tier_for_attempt(next_attempt)
            conn.execute(
                """
                UPDATE coordination
                   SET attempt_count=?,
                       last_attempt_key=?,
                       tier=?,
                       state=?,
                       terminal=?,
                       updated_at=?
                 WHERE screen_id=?
                """,
                (next_attempt, consult_id or "", tier, state, terminal, now_ms(), screen_id),
            )
            self._record_event(
                conn,
                kind="escalation_attempt",
                actor=actor,
                platform=platform,
                screen_id=screen_id,
                consult_id=consult_id or None,
                payload={"attempt_count": next_attempt, "tier": tier, "terminal": terminal, "evidence": evidence},
            )
            return next_attempt

    def mark_terminal(
        self,
        *,
        platform: str,
        screen_hash: str,
        actor: str,
        evidence: dict[str, Any],
    ) -> None:
        self._require_actor(actor, {"api", "worker", "operator", "supervisor", "system"}, "terminal")
        self._require_evidence(evidence, "source")
        with immediate_transaction(self.db_path) as conn:
            screen_id, _ = self._resolve_or_mint(conn, platform, "skeleton", screen_hash, None)
            self._ensure_coordination(conn, screen_id, platform)
            conn.execute(
                """
                UPDATE coordination
                   SET state='terminal',
                       terminal=1,
                       tier='terminal',
                       attempt_count=CASE WHEN attempt_count < 4 THEN 4 ELSE attempt_count END,
                       updated_at=?
                 WHERE screen_id=?
                """,
                (now_ms(), screen_id),
            )
            self._record_event(
                conn,
                kind="terminal_marked",
                actor=actor,
                platform=platform,
                screen_id=screen_id,
                payload={"evidence": evidence},
            )

    def clear_ladder(
        self,
        *,
        platform: str,
        screen_hash: str,
        reason: str,
        actor: str,
        evidence: dict[str, Any],
    ) -> None:
        self._require_actor(actor, {"api", "operator", "supervisor", "system"}, "clear")
        self._require_evidence(evidence, "source")
        cleared_reason = self._normalize_clear_reason(reason)
        with immediate_transaction(self.db_path) as conn:
            screen_id, _ = self._resolve_or_mint(conn, platform, "skeleton", screen_hash, None)
            self._ensure_coordination(conn, screen_id, platform)
            old = conn.execute(
                "SELECT terminal FROM coordination WHERE screen_id=?",
                (screen_id,),
            ).fetchone()
            terminal = 1 if cleared_reason == "yaml_fold" and int(old["terminal"]) == 1 else 0
            conn.execute(
                """
                UPDATE coordination
                   SET state='cleared',
                       cleared_reason=?,
                       terminal=?,
                       attempt_count=0,
                       tier=NULL,
                       last_attempt_key=NULL,
                       resume_at=NULL,
                       response_pending_until=NULL,
                       yaml_sha_at_attempt=NULL,
                       user_instructions=NULL,
                       updated_at=?
                 WHERE screen_id=?
                """,
                (cleared_reason, terminal, now_ms(), screen_id),
            )
            self._record_event(
                conn,
                kind="ladder_cleared",
                actor=actor,
                platform=platform,
                screen_id=screen_id,
                payload={"cleared_reason": cleared_reason, "original_reason": reason, "evidence": evidence},
            )

    def clear_platform_ladders(
        self,
        *,
        platform: str,
        reason: str,
        actor: str,
        evidence: dict[str, Any],
    ) -> int:
        self._require_actor(actor, {"api", "operator", "supervisor", "system"}, "clear")
        self._require_evidence(evidence, "source")
        cleared_reason = self._normalize_clear_reason(reason)
        with immediate_transaction(self.db_path) as conn:
            rows = conn.execute(
                "SELECT screen_id,terminal FROM coordination WHERE platform=? AND state <> 'cleared'",
                (platform,),
            ).fetchall()
            for row in rows:
                terminal = 1 if cleared_reason == "yaml_fold" and int(row["terminal"]) == 1 else 0
                conn.execute(
                    """
                    UPDATE coordination
                       SET state='cleared',
                           cleared_reason=?,
                           terminal=?,
                           attempt_count=0,
                           tier=NULL,
                           last_attempt_key=NULL,
                           resume_at=NULL,
                           response_pending_until=NULL,
                           yaml_sha_at_attempt=NULL,
                           user_instructions=NULL,
                           updated_at=?
                     WHERE screen_id=?
                    """,
                    (cleared_reason, terminal, now_ms(), row["screen_id"]),
                )
                self._record_event(
                    conn,
                    kind="ladder_cleared",
                    actor=actor,
                    platform=platform,
                    screen_id=row["screen_id"],
                    payload={"cleared_reason": cleared_reason, "original_reason": reason, "evidence": evidence},
                )
            return len(rows)

    def dispatch_once(
        self,
        *,
        platform: str,
        screen_hash: str,
        tier: str,
        cycle_id: str,
        actor: str,
        evidence: dict[str, Any],
    ) -> bool:
        self._require_actor(actor, {"api", "operator", "system"}, "dispatch_once")
        self._require_evidence(evidence, "source")
        with immediate_transaction(self.db_path) as conn:
            screen_id, _ = self._resolve_or_mint(conn, platform, "skeleton", screen_hash, None)
            exists = conn.execute(
                "SELECT 1 FROM tier_dispatches WHERE screen_id=? AND tier=? AND cycle_id=?",
                (screen_id, tier, cycle_id),
            ).fetchone()
            if exists:
                return False
            conn.execute(
                "INSERT INTO tier_dispatches(screen_id,tier,cycle_id) VALUES(?,?,?)",
                (screen_id, tier, cycle_id),
            )
            self._record_event(
                conn,
                kind="tier_dispatched",
                actor=actor,
                platform=platform,
                screen_id=screen_id,
                payload={"tier": tier, "cycle_id": cycle_id, "evidence": evidence},
            )
            return True

    def notify_once(
        self,
        *,
        platform: str,
        screen_hash: str,
        cycle_id: str,
        actor: str,
        evidence: dict[str, Any],
    ) -> bool:
        self._require_actor(actor, {"api", "operator", "system"}, "notify_once")
        self._require_evidence(evidence, "source")
        with immediate_transaction(self.db_path) as conn:
            screen_id, _ = self._resolve_or_mint(conn, platform, "skeleton", screen_hash, None)
            exists = conn.execute(
                "SELECT 1 FROM notify_cycles WHERE screen_id=? AND cycle_id=?",
                (screen_id, cycle_id),
            ).fetchone()
            if exists:
                return False
            conn.execute(
                "INSERT INTO notify_cycles(screen_id,cycle_id) VALUES(?,?)",
                (screen_id, cycle_id),
            )
            self._record_event(
                conn,
                kind="notify_emitted",
                actor=actor,
                platform=platform,
                screen_id=screen_id,
                payload={"cycle_id": cycle_id, "evidence": evidence},
            )
            return True

    def open_consult(
        self,
        *,
        consult_id: str,
        platform: str,
        payload_dir: str,
        actor: str,
        evidence: dict[str, Any],
        screen_hash: str | None = None,
    ) -> None:
        self._require_actor(actor, {"api", "operator", "system"}, "consult")
        self._require_evidence(evidence, "source")
        with immediate_transaction(self.db_path) as conn:
            screen_id = None
            if screen_hash:
                screen_id, _ = self._resolve_or_mint(conn, platform, "skeleton", screen_hash, None)
            existing = conn.execute(
                "SELECT status FROM consults WHERE consult_id=?",
                (consult_id,),
            ).fetchone()
            if existing:
                self._record_event(
                    conn,
                    kind="consult_seen",
                    actor=actor,
                    platform=platform,
                    screen_id=screen_id,
                    consult_id=consult_id,
                    payload={"status": existing["status"], "evidence": evidence},
                )
                return
            conn.execute(
                """
                INSERT INTO consults(consult_id,screen_id,platform,status,payload_dir)
                VALUES(?,?,?,?,?)
                """,
                (consult_id, screen_id, platform, "pending", payload_dir),
            )
            self._record_event(
                conn,
                kind="consult_opened",
                actor=actor,
                platform=platform,
                screen_id=screen_id,
                consult_id=consult_id,
                payload={"payload_dir": payload_dir, "evidence": evidence},
            )

    def resolve_consult(
        self,
        *,
        consult_id: str,
        status: str,
        actor: str,
        evidence: dict[str, Any],
        failure_reason: str | None = None,
        abandon_reason: str | None = None,
    ) -> None:
        self._require_actor(actor, {"api", "worker", "operator", "system"}, "consult")
        self._require_evidence(evidence, "source")
        if status not in {"complete", "worker_failed", "abandoned"}:
            raise StateRepoError(f"invalid resolved consult status {status!r}")
        with immediate_transaction(self.db_path) as conn:
            row = conn.execute(
                "SELECT platform,screen_id,status FROM consults WHERE consult_id=?",
                (consult_id,),
            ).fetchone()
            if row is None:
                raise StateRepoError(f"cannot resolve unknown consult {consult_id!r}")
            if row["status"] == status:
                return
            conn.execute(
                """
                UPDATE consults
                   SET status=?,
                       failure_reason=?,
                       abandon_reason=?,
                       resolved_at=?
                 WHERE consult_id=?
                """,
                (status, failure_reason, abandon_reason, now_ms(), consult_id),
            )
            self._record_event(
                conn,
                kind="consult_resolved",
                actor=actor,
                platform=row["platform"],
                screen_id=row["screen_id"],
                consult_id=consult_id,
                payload={
                    "status": status,
                    "failure_reason": failure_reason,
                    "abandon_reason": abandon_reason,
                    "evidence": evidence,
                },
            )

    def record_bt_execution(
        self,
        *,
        platform: str,
        screen_hash: str,
        actor: str,
        evidence: dict[str, Any],
        outcome: str,
        detail: str = "",
        bt_actions: list[Any] | None = None,
    ) -> None:
        self._require_actor(actor, {"api", "worker", "mac", "system"}, "bt_execution")
        self._require_evidence(evidence, "source")
        with immediate_transaction(self.db_path) as conn:
            screen_id, _ = self._resolve_or_mint(conn, platform, "skeleton", screen_hash, None)
            self._record_event(
                conn,
                kind="bt_execution",
                actor=actor,
                platform=platform,
                screen_id=screen_id,
                payload={
                    "outcome": outcome,
                    "detail": detail,
                    "bt_actions": bt_actions or [],
                    "evidence": evidence,
                },
            )

    def record_advance(
        self,
        *,
        platform: str,
        screen_hash: str,
        actor: str,
        evidence: dict[str, Any],
    ) -> None:
        self._require_actor(actor, {"api", "mac", "system"}, "advance")
        self._require_evidence(evidence, "source")
        with immediate_transaction(self.db_path) as conn:
            screen_id, _ = self._resolve_or_mint(conn, platform, "skeleton", screen_hash, None)
            self._record_event(
                conn,
                kind="screen_advanced",
                actor=actor,
                platform=platform,
                screen_id=screen_id,
                payload={"evidence": evidence},
            )

    def archive_events_before(
        self,
        *,
        cutoff_ms: int,
        actor: str,
        evidence: dict[str, Any],
        limit: int = 1000,
    ) -> int:
        self._require_actor(actor, {"system", "supervisor"}, "archive_events")
        self._require_evidence(evidence, "source")
        with immediate_transaction(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT event_id,kind,platform,screen_id,consult_id,actor,payload_json,created_at
                  FROM events
                 WHERE created_at < ?
                 ORDER BY event_id
                 LIMIT ?
                """,
                (cutoff_ms, limit),
            ).fetchall()
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO events_archive(
                        event_id, kind, platform, screen_id, consult_id, actor,
                        payload_json, created_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        row["event_id"],
                        row["kind"],
                        row["platform"],
                        row["screen_id"],
                        row["consult_id"],
                        row["actor"],
                        row["payload_json"],
                        row["created_at"],
                    ),
                )
                conn.execute("DELETE FROM events WHERE event_id=?", (row["event_id"],))
            return len(rows)

    def counts(self) -> dict[str, int]:
        tables = (
            "platforms",
            "screen_types",
            "screens",
            "screen_keys",
            "behavior_trees",
            "coordination",
            "consults",
            "events",
        )
        with state_connection(self.db_path) as conn:
            return {table: int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]) for table in tables}

    def _resolve_or_mint(
        self,
        conn,
        platform: str,
        key_kind: str,
        key_hash: str,
        features: dict[str, Any] | None,
    ) -> tuple[str, bool]:
        platform = self._require_text(platform, "platform")
        key_hash = self._require_text(key_hash, "key_hash")
        if key_kind not in {"skeleton", "signature", "widget_set"}:
            raise StateRepoError(f"invalid screen key kind {key_kind!r}")
        self._ensure_platform(conn, platform)
        row = conn.execute(
            """
            SELECT screen_id
              FROM screen_keys
             WHERE platform=? AND key_kind=? AND key_hash=?
             ORDER BY created_at ASC
             LIMIT 1
            """,
            (platform, key_kind, key_hash),
        ).fetchone()
        if row:
            screen_id = row["screen_id"]
            conn.execute("UPDATE screens SET last_seen=? WHERE screen_id=?", (now_ms(), screen_id))
            self._store_features(conn, screen_id, features)
            return screen_id, False
        screen_id = self._stable_id("screen", platform, key_kind, key_hash)
        if not conn.execute("SELECT 1 FROM screens WHERE screen_id=?", (screen_id,)).fetchone():
            conn.execute(
                "INSERT INTO screens(screen_id,platform,question_fingerprint) VALUES(?,?,?)",
                (screen_id, platform, key_hash if key_kind == "skeleton" else None),
            )
        if not conn.execute(
            """
            SELECT 1 FROM screen_keys
             WHERE platform=? AND key_kind=? AND key_hash=? AND screen_id=?
            """,
            (platform, key_kind, key_hash, screen_id),
        ).fetchone():
            conn.execute(
                "INSERT INTO screen_keys(platform,key_kind,key_hash,screen_id) VALUES(?,?,?,?)",
                (platform, key_kind, key_hash, screen_id),
            )
        self._store_features(conn, screen_id, features)
        return screen_id, True

    def _ensure_coordination(self, conn, screen_id: str, platform: str) -> None:
        row = conn.execute(
            "SELECT state FROM coordination WHERE screen_id=?",
            (screen_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO coordination(screen_id,platform,state,attempt_count,terminal) VALUES(?,?,?,?,?)",
                (screen_id, platform, "normal", 0, 0),
            )
            return
        if row["state"] == "cleared":
            conn.execute(
                """
                UPDATE coordination
                   SET state='normal',
                       attempt_count=0,
                       terminal=0,
                       tier=NULL,
                       updated_at=?
                 WHERE screen_id=?
                """,
                (now_ms(), screen_id),
            )

    def _ensure_platform(self, conn, platform: str) -> None:
        if not conn.execute("SELECT 1 FROM platforms WHERE platform=?", (platform,)).fetchone():
            conn.execute("INSERT INTO platforms(platform) VALUES(?)", (platform,))

    def _ensure_screen_type(self, conn, platform: str, screen_type: str) -> None:
        normalized_type = self._normalize_screen_type(screen_type)
        if not normalized_type or normalized_type == "UNKNOWN":
            raise StateRepoError("screen type is required")
        category = self._category_for_type(normalized_type)
        if normalized_type == category:
            raise StateRepoError(f"bare master screen type is not storable: {normalized_type}")
        self._ensure_platform(conn, platform)
        if conn.execute(
            "SELECT 1 FROM screen_types WHERE platform=? AND screen_type=?",
            (platform, normalized_type),
        ).fetchone():
            return
        artifact_path, artifact_sha = self._artifact_for_screen_type(platform, normalized_type)
        deterministic = 1 if category in {"ARTICLE", "VIDEO", "NAVIGATION"} else 0
        conn.execute(
            """
            INSERT INTO screen_types(
                platform, screen_type, category, artifact_path, artifact_sha, deterministic
            ) VALUES(?,?,?,?,?,?)
            """,
            (platform, normalized_type, category, artifact_path, artifact_sha, deterministic),
        )

    def _ensure_bundle_receipt(
        self,
        conn,
        *,
        call_kind: str,
        screen_id: str,
        bundle: dict[str, Any],
    ) -> str:
        body = self._json(bundle)
        receipt_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        bundle_id = self._stable_id("bundle", call_kind, screen_id, receipt_sha)
        if conn.execute("SELECT 1 FROM bundle_receipts WHERE bundle_id=?", (bundle_id,)).fetchone():
            return bundle_id
        conn.execute(
            """
            INSERT INTO bundle_receipts(
                bundle_id, call_kind, screen_id, slices_json, dropped_json,
                total_chars, receipt_sha
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (bundle_id, call_kind, screen_id, body, "[]", len(body), receipt_sha),
        )
        return bundle_id

    def _store_features(self, conn, screen_id: str, features: dict[str, Any] | None) -> None:
        if not features:
            return
        for key, value in features.items():
            if value is None:
                continue
            conn.execute(
                """
                INSERT INTO screen_features(screen_id,feature_kind,feature_value)
                VALUES(?,?,?)
                ON CONFLICT(screen_id,feature_kind) DO UPDATE SET feature_value=excluded.feature_value
                """,
                (screen_id, str(key), self._json(value)),
            )

    def _record_event(
        self,
        conn,
        *,
        kind: str,
        actor: str,
        payload: dict[str, Any],
        platform: str | None = None,
        screen_id: str | None = None,
        consult_id: str | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO events(kind,platform,screen_id,consult_id,actor,payload_json,created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (kind, platform, screen_id, consult_id, actor, self._json(payload), now_ms()),
        )

    def _next_bt_revision(self, conn, screen_id: str) -> int:
        value = conn.execute(
            "SELECT max(revision) FROM behavior_trees WHERE screen_id=?",
            (screen_id,),
        ).fetchone()[0]
        return int(value or 0) + 1

    def _artifact_for_screen_type(self, platform: str, screen_type: str) -> tuple[str, str]:
        path = Path(__file__).resolve().parent / "platforms" / platform / "screen_types" / f"{screen_type}.yaml"
        if path.exists():
            body = path.read_bytes()
            return str(path.relative_to(Path(__file__).resolve().parent.parent)), hashlib.sha256(body).hexdigest()
        source = f"{platform}:{screen_type}:artifact-missing"
        return f"missing:{platform}/{screen_type}.yaml", hashlib.sha256(source.encode("utf-8")).hexdigest()

    def _category_for_type(self, screen_type: str) -> str:
        if "__" in screen_type:
            candidate = screen_type.split("__", 1)[0]
        elif "_" in screen_type:
            candidate = screen_type.split("_", 1)[0]
        else:
            candidate = screen_type
        if candidate not in MASTER_CATEGORIES:
            raise StateRepoError(f"cannot infer category for screen type {screen_type!r}")
        return candidate

    def _normalize_screen_type(self, screen_type: str | None) -> str | None:
        if screen_type is None:
            return None
        value = str(screen_type).strip()
        return value or None

    def _normalize_clear_reason(self, reason: str) -> str:
        value = str(reason or "").strip()
        aliases = {
            "advance": "advanced",
            "advanced": "advanced",
            "screen_advance": "advanced",
            "user-stop": "user_stop_abandon",
            "user_stop": "user_stop_abandon",
            "abandon": "user_stop_abandon",
            "abandon_consultation": "user_stop_abandon",
            "session_reset": "user_stop_reset",
            "reset": "user_stop_reset",
            "yaml_fold": "yaml_fold",
            "fold": "yaml_fold",
        }
        normalized = aliases.get(value, value)
        if normalized not in COORD_CLEAR_REASONS:
            raise StateRepoError(f"invalid clear reason {reason!r}")
        return normalized

    def _tier_for_attempt(self, attempt: int) -> tuple[str, str, int]:
        if attempt <= 2:
            return "tier1", "diagnosing", 0
        if attempt == 3:
            return "tier2", "diagnosing", 0
        if attempt == 4:
            return "tier3", "diagnosing", 0
        return "terminal", "terminal", 1

    def _require_actor(self, actor: str, allowed: set[str], action: str) -> None:
        if actor not in ACTORS:
            raise StateRepoError(f"{action} rejected unknown actor {actor!r}")
        if actor not in allowed:
            raise StateRepoError(f"{action} rejected actor {actor!r}")

    def _require_evidence(self, evidence: dict[str, Any], *keys: str) -> None:
        if not isinstance(evidence, dict) or not evidence:
            raise StateRepoError("state transition requires evidence")
        missing = [key for key in keys if not evidence.get(key)]
        if missing:
            raise StateRepoError(f"state transition evidence missing keys: {missing}")

    def _require_text(self, value: str, name: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise StateRepoError(f"{name} is required")
        return text

    def _stable_id(self, prefix: str, *parts: Any) -> str:
        body = self._json(parts)
        return f"{prefix}_{hashlib.sha256(body.encode('utf-8')).hexdigest()[:24]}"

    def _json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)


def get_state_repo(db_path: Path | None = None) -> StateRepo:
    return StateRepo(db_path=db_path)
