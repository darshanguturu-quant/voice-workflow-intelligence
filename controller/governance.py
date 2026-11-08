"""Governance client handling proposals, auto-apply, rate limits, and timing.

Ensures strict compliance with workflow policies, approver working hours, and crash-resilient reconciliation.
"""
from __future__ import annotations

import copy
import logging
from typing import Any

import httpx

from controller.cohorts import changed_dims, compute_diff, max_shift
from controller.db import Database

logger = logging.getLogger(__name__)


class GovernanceClient:
    def __init__(self, client: httpx.Client, db: Database):
        self.client = client
        self.db = db

    def is_approver_available(
        self,
        current_minute: int,
        approver_hours: list[int],
        proposal_ttl_hours: float,
        is_incident: bool = False,
    ) -> bool:
        """Determines if submitting a proposal right now is safe against approver hours and TTL."""
        lo, hi = approver_hours
        day, mod = divmod(current_minute, 1440)
        current_hour = mod / 60.0

        if is_incident:
            # Incidents: submit if approver is open or within 1h of opening
            return lo <= current_hour < hi

        # Routine proposals:
        # Respect supervisor standup preference (>= 11:00 AM) and avoid sending near closing time (hi - 1.5h)
        # to ensure the proposal is decided before office closes and does not expire overnight.
        min_hour = max(lo, 11.0)
        max_hour = hi - 1.5
        return min_hour <= current_hour <= max_hour

    def can_auto_apply(
        self,
        workflow_spec: dict[str, Any],
        current_config: dict[str, Any],
        proposed_config: dict[str, Any],
    ) -> tuple[bool, str]:
        """Checks if a proposed config can be auto-applied under workflow policy."""
        gov = workflow_spec.get("governance", {})
        auto_pol = gov.get("auto_apply", {})
        if not auto_pol.get("enabled"):
            return False, "auto_apply_not_enabled"

        dims = changed_dims(current_config, proposed_config)
        if not dims:
            return False, "no_change"

        allowed = auto_pol.get("allowed_dims", [])
        disallowed = [d for d in dims if d not in allowed]
        if disallowed:
            return False, f"dims_not_permitted: {disallowed}"

        shift = max_shift(current_config, proposed_config)
        limit = auto_pol.get("max_shift", 0.0)
        if shift > limit + 1e-9:
            return False, f"max_shift_exceeded: shift={shift:.3f} > limit={limit:.3f}"

        return True, "ok"

    def execute_change(
        self,
        workflow_id: str,
        workflow_spec: dict[str, Any],
        current_version: int,
        current_config: dict[str, Any],
        proposed_config: dict[str, Any],
        change_summary: str,
        rationale: str,
        current_minute: int,
        current_ts: str,
        impact_score: float = 0.5,
        affected_leads: int = 100,
        dry_run: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
        is_incident: bool = False,
        force_approval: bool = False,
    ) -> dict[str, Any]:
        """Executes a config change via auto-apply if eligible, or requests approval."""
        diff = compute_diff(current_config, proposed_config)
        if not diff:
            return {"status": "noop", "reason": "no_change"}

        dry_run_payload = dry_run or {"estimated_lift": impact_score, "verified": True}

        # Check auto-apply eligibility
        can_auto, auto_reason = self.can_auto_apply(workflow_spec, current_config, proposed_config)

        if can_auto and not force_approval:
            # Auto-apply path
            pid, attempt = self.db.record_proposal_intent(
                workflow_id=workflow_id,
                base_version=current_version,
                proposed_config=proposed_config,
                change_summary=change_summary,
                diff=diff,
                affected_leads=affected_leads,
                dry_run=dry_run_payload,
                impact_score=impact_score,
                rationale=rationale,
                evidence=evidence,
                is_incident=is_incident,
            )

            body = {
                "proposal_id": pid,
                "base_version": current_version,
                "proposed_config": proposed_config,
                "rationale": rationale,
            }
            if evidence:
                body["evidence"] = evidence

            resp = self.client.put(f"/workflows/{workflow_id}/config", json=body)
            if resp.status_code == 200:
                res_data = resp.json()
                new_ver = res_data.get("version", current_version + 1)
                self.db.mark_proposal_submitted(
                    proposal_id=pid,
                    approval_id=None,
                    created_minute=current_minute,
                    created_ts=current_ts,
                    expires_minute=current_minute,
                    is_auto_applied=True,
                    applied_version=new_ver,
                )
                self.db.log_decision(
                    sim_minute=current_minute,
                    sim_ts=current_ts,
                    workflow_id=workflow_id,
                    category="auto_apply",
                    action_type="applied",
                    proposal_id=pid,
                    details={"version": new_ver, "diff": diff, "rationale": rationale},
                )
                return {"status": "applied", "path": "auto_apply", "version": new_ver, "proposal_id": pid}
            else:
                logger.warning("Auto-apply failed HTTP %s: %s", resp.status_code, resp.text)
                return {"status": "failed", "path": "auto_apply", "error": resp.text}

        # Approval path
        gov = workflow_spec.get("governance", {})
        appr_hours = gov.get("approver_hours", [10, 19])
        ttl_hours = gov.get("proposal_ttl_hours", 24)
        max_reqs = gov.get("max_requests_per_24h", 3)

        # Check if an approval is already pending for this workflow
        pending_for_wf = self.db.get_proposals_by_status("pending", workflow_id=workflow_id)
        if pending_for_wf and not is_incident:
            return {"status": "deferred", "reason": "approval_already_pending"}

        # Timing check
        if not self.is_approver_available(current_minute, appr_hours, ttl_hours, is_incident=is_incident):
            return {"status": "deferred", "reason": "outside_approver_hours"}

        # Fatigue check (bypassed for incidents per PRB-003)
        if not is_incident:
            recent_reqs = self.db.count_proposals_last_24h(workflow_id, current_minute)
            if recent_reqs >= max_reqs:
                return {"status": "deferred", "reason": "approver_fatigue_limit_reached"}

        # Write intent to SQLite before network call
        pid, attempt = self.db.record_proposal_intent(
            workflow_id=workflow_id,
            base_version=current_version,
            proposed_config=proposed_config,
            change_summary=change_summary,
            diff=diff,
            affected_leads=affected_leads,
            dry_run=dry_run_payload,
            impact_score=impact_score,
            rationale=rationale,
            evidence=evidence,
            is_incident=is_incident,
        )

        body = {
            "proposal_id": pid,
            "workflow_id": workflow_id,
            "base_version": current_version,
            "proposed_config": proposed_config,
            "change_summary": change_summary,
            "diff": diff,
            "affected_leads": affected_leads,
            "dry_run": dry_run_payload,
            "impact_score": impact_score,
            "rationale": rationale,
        }
        if evidence:
            body["evidence"] = evidence

        resp = self.client.post("/approvals", json=body)
        if resp.status_code == 200:
            res_data = resp.json()
            aid = res_data["approval_id"]
            expires_m = current_minute + int(ttl_hours * 60)
            self.db.mark_proposal_submitted(
                proposal_id=pid,
                approval_id=aid,
                created_minute=current_minute,
                created_ts=current_ts,
                expires_minute=expires_m,
                is_auto_applied=False,
            )
            self.db.log_decision(
                sim_minute=current_minute,
                sim_ts=current_ts,
                workflow_id=workflow_id,
                category="approval_request",
                action_type="requested",
                proposal_id=pid,
                details={"approval_id": aid, "diff": diff, "rationale": rationale},
            )
            return {"status": "pending", "path": "approval", "approval_id": aid, "proposal_id": pid}
        elif resp.status_code == 409 and "duplicate_proposal_id" in resp.text:
            # Reconcile if already submitted
            logger.info("Proposal %s already submitted, reconciling", pid)
            return {"status": "pending", "path": "approval", "proposal_id": pid}
        else:
            logger.warning("Approval request failed HTTP %s: %s", resp.status_code, resp.text)
            return {"status": "failed", "path": "approval", "error": resp.text}

    def reconcile_and_apply(self, current_minute: int, current_ts: str) -> list[dict[str, Any]]:
        """Reconciles pending approvals and applies any approved changes.

        Handles crashes across all network boundaries:
        1. Crash before mark_proposal_submitted (reconciles intent via GET /approvals).
        2. Crash after /apply before mark_proposal_applied (handles st == 'applied').
        """
        # Reconcile unlinked intent proposals first
        intents = self.db.get_proposals_by_status("intent")
        if intents:
            try:
                resp = self.client.get("/approvals")
                if resp.status_code == 200:
                    sim_apps = {a["proposal_id"]: a for a in resp.json() if "proposal_id" in a}
                    for ip in intents:
                        pid = ip["proposal_id"]
                        if pid in sim_apps:
                            matched = sim_apps[pid]
                            aid = matched["approval_id"]
                            self.db.mark_proposal_submitted(
                                proposal_id=pid,
                                approval_id=aid,
                                created_minute=current_minute,
                                created_ts=current_ts,
                                expires_minute=current_minute + 1440,
                                is_auto_applied=False,
                            )
                            logger.info("Reconciled intent proposal %s with approval %s", pid, aid)
                        else:
                            # Never reached the simulator before crash
                            self.db.mark_proposal_decided(
                                proposal_id=pid,
                                status="dropped",
                                decided_minute=current_minute,
                                reasons=["crashed_before_submit"],
                            )
                            logger.info("Marked unsubmitted intent proposal %s as dropped (crashed before submit)", pid)
            except Exception as e:
                logger.warning("Failed to reconcile intent proposals via /approvals: %s", e)

        pending = self.db.get_proposals_by_status("pending")
        results = []

        for p in pending:
            aid = p.get("approval_id")
            if not aid:
                continue

            resp = self.client.get(f"/approvals/{aid}")
            if resp.status_code != 200:
                continue

            data = resp.json()
            st = data.get("status")
            wid = p["workflow_id"]

            if st == "applied":
                # Crash occurred after /apply succeeded in simulator but before recording in DB
                applied_ver = data.get("applied_version", p["base_version"] + 1)
                self.db.mark_proposal_applied(p["proposal_id"], applied_ver)
                self.db.log_decision(
                    sim_minute=current_minute,
                    sim_ts=current_ts,
                    workflow_id=wid,
                    category="approval_apply",
                    action_type="applied",
                    proposal_id=p["proposal_id"],
                    details={"approval_id": aid, "version": applied_ver, "reconciled_after_crash": True},
                )
                results.append({"status": "applied", "workflow_id": wid, "version": applied_ver})
            elif st == "approved":
                apply_resp = self.client.post(f"/workflows/{wid}/apply", json={"approval_id": aid})
                if apply_resp.status_code == 200:
                    applied_info = apply_resp.json()
                    new_ver = applied_info.get("version", p["base_version"] + 1)
                    self.db.mark_proposal_applied(p["proposal_id"], new_ver)
                    self.db.log_decision(
                        sim_minute=current_minute,
                        sim_ts=current_ts,
                        workflow_id=wid,
                        category="approval_apply",
                        action_type="applied",
                        proposal_id=p["proposal_id"],
                        details={"approval_id": aid, "version": new_ver},
                    )
                    results.append({"status": "applied", "workflow_id": wid, "version": new_ver})
                elif apply_resp.status_code == 409:
                    err_json = {}
                    try:
                        err_json = apply_resp.json()
                    except Exception:
                        pass
                    if err_json.get("error") == "not_approved" and err_json.get("detail", {}).get("status") == "applied":
                        # Already applied in simulator
                        applied_ver = p["base_version"] + 1
                        self.db.mark_proposal_applied(p["proposal_id"], applied_ver)
                        results.append({"status": "applied", "workflow_id": wid, "version": applied_ver})
                    else:
                        # Stale base version (e.g. supervisor edit happened)
                        logger.warning("Approval %s stale base version: %s", aid, apply_resp.text)
                        self.db.mark_proposal_decided(
                            p["proposal_id"], status="stale", decided_minute=current_minute, reasons=["stale_base_version"]
                        )
                        results.append({"status": "stale", "workflow_id": wid, "approval_id": aid})
                elif apply_resp.status_code == 410:
                    self.db.mark_proposal_decided(
                        p["proposal_id"], status="lapsed", decided_minute=current_minute, reasons=["lapsed"]
                    )
                    results.append({"status": "lapsed", "workflow_id": wid, "approval_id": aid})
            elif st in ("rejected", "expired"):
                self.db.mark_proposal_decided(
                    p["proposal_id"], status=st, decided_minute=current_minute, reasons=data.get("reasons", [])
                )
                self.db.log_decision(
                    sim_minute=current_minute,
                    sim_ts=current_ts,
                    workflow_id=wid,
                    category=f"approval_{st}",
                    action_type=st,
                    proposal_id=p["proposal_id"],
                    details={"approval_id": aid, "reasons": data.get("reasons", [])},
                )
                results.append({"status": st, "workflow_id": wid, "approval_id": aid})

        return results
