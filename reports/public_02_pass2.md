# Empirical Evaluation Report — public_02_incidents (Pass 2 Benchmark)

Refer to [public_02.md](file:///c:/Users/sreej/Downloads/formi-outcome-loop/reports/public_02.md) for the complete, canonical empirical report.

## Summary of Observed Outcomes
- Incident MTTR: 120–135 minutes (average 125m) across all seeds.
- Hallucinated calls on `ks_cancel`: cut by 78%–83% (55–69 vs 320–330 baseline).
- Scenario-wide hallucinated calls: cut by 60%–67% (137–159 vs 387–416 baseline).
- DID D2 traffic: reduced to 0.0% from Day 4 through Day 9.
- Decaying W3 exposure: contained between 6.9% and 8.3% across all post-onset days (84% reduction).
- Credits used: 13.0 (187.0 remaining).
