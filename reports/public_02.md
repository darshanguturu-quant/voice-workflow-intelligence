# Grade report — public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.320 | +29.1% | 0.242 | +31.6% | 0.457 | -4.2% | -10% | 51 (330) |
| nw_kyc | 0.384 | +2.6% | 0.246 | +1.9% | 0.596 | +0.7% | -13% | 29 (31) |
| nw_renewal | 0.414 | -3.0% | 0.259 | -0.9% | 0.540 | -1.5% | -19% | 49 (55) |
| ALL | 0.365 | +9.2% | 0.249 | +12.1% | 0.513 | -3.2% | -14% | 129 (416) |

## Changes applied by the controller

16 applied — neutral: 2, harmful: 6, beneficial: 8

- ks_cancel v2 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.007 didΔ=+0.000 → **neutral**
- nw_kyc v2 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.016 didΔ=+0.000 → **harmful**
- ks_cancel v3 (controller_approved) dims=['voice'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- nw_renewal v2 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.029 didΔ=+0.000 → **beneficial**
- nw_kyc v3 (controller_approved) dims=['welcome_message'] engageΔ=-0.041 didΔ=+0.000 → **harmful**
- nw_kyc v4 (controller_approved) dims=['welcome_message'] engageΔ=+0.043 didΔ=+0.000 → **beneficial**
- ks_cancel v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.245 → **beneficial**
- ks_cancel v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.197 → **beneficial**
- ks_cancel v6 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.082 → **beneficial**
- nw_renewal v3 (controller_approved) dims=['welcome_message'] engageΔ=+0.017 didΔ=+0.000 → **beneficial**
- ks_cancel v8 (controller_approved) dims=['prompt_version'] engageΔ=-0.051 didΔ=+0.000 → **harmful**
- nw_kyc v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.041 didΔ=+0.000 → **beneficial**
- nw_renewal v5 (controller_approved) dims=['voice'] engageΔ=-0.037 didΔ=+0.000 → **harmful**
- nw_kyc v6 (controller_approved) dims=['welcome_message'] engageΔ=-0.039 didΔ=+0.000 → **harmful**
- nw_renewal v6 (controller_approved) dims=['welcome_message'] engageΔ=+0.053 didΔ=+0.000 → **beneficial**
- nw_renewal v7 (controller_approved) dims=['welcome_message'] engageΔ=-0.025 didΔ=+0.000 → **harmful**

## Governance

```
{
  "approval_requests": 14,
  "decisions": {
    "rejected": 1,
    "approved": 13,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 1
  },
  "applied_via_approval": 13,
  "auto_applied": 3,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 2,
    "nw_kyc": 2,
    "ks_cancel": 2
  }
}
```

## Incidents

```
[
  {
    "type": "did_flagged",
    "workflow": "ks_cancel",
    "at": "D2 10:00",
    "flagged_did_share_by_day": {
      "2": 0.501,
      "3": 0.0,
      "4": 0.0,
      "5": 0.0,
      "6": 0.0,
      "7": 0.0,
      "8": 0.0,
      "9": 0.0
    }
  },
  {
    "type": "supervisor_edit",
    "workflow": "ks_cancel",
    "at": "D3 11:30",
    "mttr_minutes": 120,
    "fixed_by": "controller_approved"
  },
  {
    "type": "supervisor_edit",
    "workflow": "nw_renewal",
    "at": "D5 15:00"
  }
]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "2": 0.246,
    "3": 0.441,
    "4": 0.499,
    "5": 0.502,
    "6": 0.514,
    "7": 0.488,
    "8": 0.496,
    "9": 0.302
  }
}
```

Credits used: 13 · validation runs: 4 · LLM fix calls: 1