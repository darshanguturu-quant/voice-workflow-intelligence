# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.351 | +5.4% | 0.244 | +2.6% | 0.499 | +1.7% | -1% | 53 (47) |
| nw_kyc | 0.394 | +8.4% | 0.244 | +0.9% | 0.631 | +6.8% | 28% | 26 (21) |
| nw_renewal | 0.431 | +2.7% | 0.265 | +0.6% | 0.560 | +2.7% | 14% | 40 (42) |
| ALL | 0.387 | +5.0% | 0.252 | +1.5% | 0.546 | +3.1% | 11% | 119 (110) |

## Changes applied by the controller

3 applied — neutral: 1, beneficial: 2

- ks_cancel v2 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.001 didΔ=+0.000 → **neutral**
- nw_renewal v2 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.051 didΔ=+0.000 → **beneficial**
- nw_kyc v2 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.082 didΔ=+0.000 → **beneficial**

## Governance

```
{
  "approval_requests": 3,
  "decisions": {
    "approved": 3,
    "expired": 0
  },
  "rejection_reasons": {},
  "applied_via_approval": 3,
  "auto_applied": 0,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 1,
    "nw_kyc": 1,
    "ks_cancel": 1
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "3": 0.249,
    "4": 0.261,
    "5": 0.242,
    "6": 0.241,
    "7": 0.245
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0