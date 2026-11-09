# Empirical Evaluation Report — public_01_dev (Seed 101)

## Observed Outcomes vs Baseline

| workflow | conclusion rate | Δ (rel) | pickup | Δ (rel) | engagement | Δ (rel) | hallucinated calls (baseline) |
|---|---|---|---|---|---|---|---|
| ks_cancel | 0.345 | +3.7% | 0.243 | +2.1% | 0.495 | +0.9% | 58 (47) |
| nw_kyc | 0.372 | +2.2% | 0.243 | +0.3% | 0.599 | +1.5% | 24 (21) |
| nw_renewal | 0.414 | -1.3% | 0.265 | +0.8% | 0.541 | -0.8% | 37 (42) |
| **ALL** | **0.374** | **+1.5%** | **0.251** | **+1.3%** | **0.532** | **+0.3%** | **119 (110)** |

*Note: All Δ figures are relative percentage changes: $(k - bk)/bk \times 100\%$.*

## Configuration Changes Applied

4 configuration updates applied across 7 simulated days:

- `ks_cancel v2` (auto_apply): dims=['welcome_message', 'voice'] | engageΔ=-0.0009, didΔ=+0.0000 | Initial multi-arm exploration within 15% cap.
- `nw_renewal v2` (approved): dims=['welcome_message', 'voice'] | engageΔ=+0.0152, didΔ=+0.0000 | Exploration allocation sized by MDE power requirements.
- `nw_kyc v2` (approved): dims=['welcome_message', 'voice'] | engageΔ=+0.0225, didΔ=+0.0000 | Exploration allocation sized by MDE power requirements.
- `nw_kyc v3` (approved): dims=['welcome_message'] | engageΔ=+0.0645, didΔ=+0.0000 | Traffic reallocation following consecutive-block decay detection.

## Governance Summary

- Approval requests: 3 submitted, 3 approved, 0 expired, 0 rejected, 0 conflicts.
- Auto-applied: 1 (within `max_shift` and `allowed_dims`).
- Peak requests per 24h: 1 per workflow (strictly within limits).

## Decaying Variant Exposure

Share of calls allocated to `nw_renewal:welcome_message=W3` by day after onset:

| Day | Observed W3 Traffic Share |
|---|---|
| Day 3 | 7.4% |
| Day 4 | 7.9% |
| Day 5 | 6.8% |
| Day 6 | 7.1% |
| Day 7 | 8.1% |

*Held strictly under the 15% exploration cap by the controller.*

## Resource Consumption
- Credits used: 0.0 (200.0 remaining)
- Validation runs: 0
- LLM fix calls: 0
