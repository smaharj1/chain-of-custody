"""Infrastructure CI helpers (Phase 6, report §8): the pre-plan source policy, the human-gate
checks the workflow templates call, and the extra ci_lint rules for infrastructure workflows.

Standard library only, Python 3.9+. Nothing in this package applies, plans or deploys anything.

    hcl            a careful HCL2 scanner (native syntax and JSON syntax); no terraform binary
    source_policy  the pre-plan bans on Terraform source (RC-3, §8.1 Layer 5, IMP-25)
    gate           small checks the workflows call: approver, main sha, plan digest, plan summary
    ci_rules       infrastructure rules for ci_lint.py (apply gate, saved plan, policy before init)
"""
from __future__ import annotations
