# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.348 | +4.5% | 0.242 | +1.6% | 0.502 | +2.3% | 0% | 48 (47) |
| nw_kyc | 0.368 | +1.3% | 0.242 | -0.1% | 0.601 | +1.8% | 19% | 22 (21) |
| nw_renewal | 0.424 | +0.9% | 0.267 | +1.5% | 0.543 | -0.4% | -0% | 34 (42) |
| ALL | 0.378 | +2.5% | 0.251 | +1.2% | 0.536 | +1.1% | 4% | 104 (110) |

## Changes applied by the controller

8 applied (6 parity / 2 lift)

- nw_renewal v2 (controller_approved) dims=['voice'] engageΔ=+0.002 didΔ=+0.000 → parity
- nw_kyc v2 (controller_approved) dims=['voice'] engageΔ=+0.035 didΔ=+0.000 → +eng lift
- nw_renewal v3 (controller_approved) dims=['voice'] engageΔ=+0.006 didΔ=+0.000 → parity
- nw_renewal v4 (controller_approved) dims=['voice'] engageΔ=-0.007 didΔ=+0.000 → parity
- nw_kyc v3 (controller_approved) dims=['voice'] engageΔ=+0.034 didΔ=+0.000 → +eng lift
- nw_renewal v5 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → parity
- nw_renewal v6 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → parity
- nw_renewal v7 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → parity

## Governance

```
{
  "approval_requests": 17,
  "decisions": {
    "approved": 8,
    "expired": 7
  },
  "rejection_reasons": {},
  "applied_via_approval": 8,
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
{}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0