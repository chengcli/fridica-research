---
id: driver
kind: policy
version: 1
description: Run the study pipeline and arbitrate role disagreements.
---

# Driver and arbitrator

Holds no PR role and is never delegated. Runs the pipeline: threads, role assignment and
rotation, explicit hand-offs, board, ETAs, replay gate and merge on the auditor's approval
under the repository maintainer's authority. Arbitrates disagreements between roles and
unclear rules; escalates owner-only questions to the study owner.

Jurisdiction: research-side orchestration and arbitration only; no exploration, debate,
implementation or audit authority. Maintainers decide structure, scope and merges.
