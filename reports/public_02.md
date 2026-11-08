# Grade report — public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.312 | +26.0% | 0.238 | +29.7% | 0.453 | -4.8% | -12% | 90 (330) |
| nw_kyc | 0.366 | -2.2% | 0.239 | -1.2% | 0.590 | -0.4% | -13% | 36 (31) |
| nw_renewal | 0.420 | -1.6% | 0.262 | -0.1% | 0.543 | -1.0% | -8% | 45 (55) |
| ALL | 0.359 | +7.6% | 0.247 | +11.1% | 0.512 | -3.4% | -11% | 171 (416) |

## Changes applied by the controller

16 applied — neutral: 1, harmful: 6, beneficial: 9

- ks_cancel v2 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.007 didΔ=+0.000 → **neutral**
- nw_kyc v2 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.016 didΔ=+0.000 → **harmful**
- nw_renewal v2 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.029 didΔ=+0.000 → **beneficial**
- nw_renewal v3 (controller_approved) dims=['welcome_message'] engageΔ=+0.042 didΔ=+0.000 → **beneficial**
- nw_renewal v4 (controller_approved) dims=['welcome_message'] engageΔ=+0.080 didΔ=+0.000 → **beneficial**
- nw_renewal v5 (controller_approved) dims=['welcome_message'] engageΔ=-0.072 didΔ=+0.000 → **harmful**
- ks_cancel v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.245 → **beneficial**
- ks_cancel v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.196 → **beneficial**
- ks_cancel v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.081 → **beneficial**
- nw_kyc v3 (controller_approved) dims=['welcome_message'] engageΔ=+0.041 didΔ=+0.000 → **beneficial**
- ks_cancel v7 (controller_approved) dims=['prompt_version'] engageΔ=-0.051 didΔ=+0.000 → **harmful**
- nw_kyc v4 (controller_approved) dims=['welcome_message'] engageΔ=-0.039 didΔ=+0.000 → **harmful**
- nw_renewal v7 (controller_approved) dims=['voice'] engageΔ=-0.036 didΔ=+0.000 → **harmful**
- nw_kyc v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.041 didΔ=+0.000 → **beneficial**
- nw_kyc v6 (controller_approved) dims=['welcome_message'] engageΔ=-0.039 didΔ=+0.000 → **harmful**
- nw_renewal v8 (controller_approved) dims=['welcome_message'] engageΔ=+0.053 didΔ=+0.000 → **beneficial**

## Governance

```
{
  "approval_requests": 15,
  "decisions": {
    "rejected": 2,
    "approved": 13,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 2
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
      "2": 0.482,
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
    "mttr_minutes": 390,
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
    "2": 0.66,
    "3": 0.492,
    "4": 0.486,
    "5": 0.497,
    "6": 0.507,
    "7": 0.493,
    "8": 0.25,
    "9": 0.0
  }
}
```

Credits used: 24 · validation runs: 6 · LLM fix calls: 2