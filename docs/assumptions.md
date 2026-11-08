# Assumptions

1. **Simulator is outside world**: All interactions occur strictly through the HTTP endpoints documented in `docs/SIMULATOR.md`. Controller never touches `sim/`, `runs/`, or hidden ground truth in scenario files.
2. **Deterministic Time**: Simulated time advances strictly on calls to `POST /sim/advance`. Wall-clock time between advances does not affect simulator state.
3. **Approver Working Hours**: Simulated approvers only review requests during `governance.approver_hours`. Requests pending outside these hours will expire if `TTL` elapses before the next opening hour.
4. **Credit Allocation**: Scenario credits (`credits_remaining`) are shared across all workflows. A local bookkeeping reservation must ensure critical incidents have credits even if routine exploration runs first.
5. **Auto-Apply Constraints**: Auto-apply is strictly governed by `governance.auto_apply` (`enabled`, `allowed_dims`, `max_shift`). Total-variation distance limit must be respected per step.
6. **Cohort Evaluation**: Leads match the first cohort whose `where` criteria are satisfied (AND across features, OR within feature value list). Unmatched leads belong to `default`.
7. **Single Source of Truth for State**: Controller state must be fully reconstructible from SQLite on crash/restart; the simulator's internal state is assumed to remain waiting at the current simulated minute upon controller restart.
