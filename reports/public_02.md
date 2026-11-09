# Empirical Evaluation Report — public_02_incidents (Seed 202)

## Observed Outcomes vs Baseline

| workflow | conclusion rate | Δ (rel) | pickup | Δ (rel) | engagement | Δ (rel) | hallucinated calls (baseline) |
|---|---|---|---|---|---|---|---|
| ks_cancel | 0.332 | **+34.3%** | 0.242 | **+31.6%** | 0.472 | -0.9% | 55 (330) |
| nw_kyc | 0.370 | -1.3% | 0.243 | +0.3% | 0.589 | -0.5% | 30 (31) |
| nw_renewal | 0.434 | +1.7% | 0.265 | +1.3% | 0.549 | +0.1% | 52 (55) |
| **ALL** | **0.374** | **+12.0%** | **0.250** | **+12.7%** | **0.522** | **-1.5%** | **137 (416)** |

*Note: All Δ figures are relative percentage changes: $(k - bk)/bk \times 100\%$. Scenario-wide hallucinated calls were cut by 67.1% (137 vs 416), and on incident workflow ks_cancel by 83.3% (55 vs 330).*

## Incident Analysis & Stage-by-Stage MTTR Timestamps

### Incident 1: Flagged Caller ID (ks_cancel, D2 10:00)
- Onset: Day 2, 10:00 (Carrier flagging of DID D2 drops pickup rate from ~0.24 to ~0.17).
- Action: Controller auto-applied 3-step gradual rotation within `max_shift = 0.25` bounds (versions 4, 5, 6).
- Observed DID D2 Traffic Share:
  - Day 2: 49.4%
  - Day 3: 1.9%
  - Days 4–9: **0.0%** (traffic completely eliminated from flagged caller ID).

### Incident 2: Supervisor Prompt Edit (ks_cancel, D3 11:30)
- **Incident Onset**: Day 3, 11:30 (`sim_minute = 5010`). Supervisor replaces prompt with aggressive P2, inducing 7.0% hallucination rate.
- **Controller Ingestion & Detection**: Day 3, 11:45 (`sim_minute = 5025`). **Detection Lag: 15 minutes** (immediate 15m simulation tick).
- **Validation Run & Fix Generation**: Day 3, 11:45 (`sim_minute = 5025`). Proactive validation on P2 yields pass rate < 0.97. LLM generates fix candidate P5; confirmation validation yields 0.9933 pass rate on $n=300$.
- **Emergency Proposal Submitted**: Day 3, 11:45 (`sim_minute = 5025`). Proposal `3c0f91ebd9459327` submitted during approver working hours. **Dispatch Lag: < 1 minute**.
- **Approver Decision**: Day 3, 13:30 (`sim_minute = 5130`). Simulated human approver review duration: **105 minutes**.
- **Controller Application**: Day 3, 13:30 (`sim_minute = 5130`). **Apply Lag: < 1 minute**.
- **Total Controller MTTR**: **120 minutes** (from injection at 11:30 to application at 13:30).

### Stage-by-Stage MTTR Comparison & Attribution

| Phase | Unassisted Baseline | Controller Run | Difference | Attribution / Finding |
|---|---|---|---|---|
| **Incident Injection** | D3 11:30 (Min 5010) | D3 11:30 (Min 5010) | 0 min | Identical scenario event |
| **Detection Lag** | None (unattended) | 15 min (D3 11:45) | - | Immediate tick detection via proactive validation |
| **Proposal Dispatch** | None | < 1 min (D3 11:45) | - | Emergency proposal dispatched at approver open hours |
| **Approver Review Latency** | None | 105 min (11:45–13:30) | - | External simulated human review latency |
| **Application Lag** | None | < 1 min (D3 13:30) | - | Instantaneous HTTP config mutation |
| **Resolution Time** | Unresolved (active to end) | D3 13:30 (Min 5130) | - | Resolved by `controller_approved` |
| **Total Incident Duration** | > 9,000 min (sim end) | **120 min** | > 8,880 min saved | Controlled intervention averted ongoing hallucination |

*Attribution Note*: In the unassisted baseline, prompt P2 was never reverted, remaining active for the remainder of the scenario. In our earlier coarse-tick run (Pass 1), MTTR was 390 minutes (90m detection lag + 300m human review wait). Controller proactive validation directly reduced detection and proposal dispatch lag from 90 min to 15 min (75 min controller engineering savings). Human approver review latency (105 min) is an external simulated environment variable. Beyond early detection and immediate proposal submission, further MTTR attribution is inconclusive.

## Configuration Updates Applied (13 Total)

- 3 DID rotation updates (`ks_cancel v4, v5, v6`): didΔ = +0.2452, +0.1955, +0.0813 (restored carrier pickup rates).
- 1 Emergency prompt update (`ks_cancel v8`): engageΔ = -0.0505, hallucination rate 0.070 -> 0.000 (intentional safety trade-off).
- 9 Routine exploration and decay rollback updates across `ks_cancel`, `nw_renewal`, and `nw_kyc`.

## Decaying Variant Exposure

Observed share of calls on `nw_renewal:welcome_message=W3` across days:

| Day | Observed W3 Traffic Share |
|---|---|
| Day 2 | 8.1% |
| Day 3 | 6.9% |
| Day 4 | 7.4% |
| Day 5 | 8.3% |
| Day 6 | 7.4% |
| Day 7 | 7.5% |
| Day 8 | 7.9% |
| Day 9 | 8.1% |

*Held strictly between 6.9% and 8.3% across all post-onset days by the 15% exploration cap (an 84% reduction compared to unconstrained 50% allocation).*

## Resource Consumption
- Credits used: 13.0 (187.0 remaining)
- Validation runs: 4 runs (1 screening, 3 confirmation)
- LLM fix calls: 1 call
