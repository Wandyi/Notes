# reference_impl — executable contracts

Dependency-free Python that makes the design's key invariants *executable* rather than
aspirational. This is not a running system; it is the typed skeleton the docs refer to, so a
reviewer can see the safety posture enforced in code.

| File | What it pins down | Docs |
|---|---|---|
| [contracts.py](contracts.py) | Core schemas (Incident, Evidence, Hypothesis, Runbook, ExecutionGrant, budgets). Encodes two invariants structurally. | [13](../docs/13-data-model.md) |
| [tools.py](tools.py) | Read-only investigation-agent interface + manifest; dynamic `select`; resilient concurrent `gather` (timeouts → `degraded`, never a crash). No write capability exists in the manifest. | [05](../docs/05-tool-integration.md) |
| [state_machine.py](state_machine.py) | The supervisor: transitions are code; budget-checked + checkpointed per step; `APPROVE` waits for free; `REMEDIATE` is idempotency-keyed. | [02](../docs/02-agent-runtime.md), [11](../docs/11-remediation-and-verification.md) |

## Invariants you can run

```bash
python3 -c "
import contracts as c
# 1) 'No uncited claims' is unrepresentable — a Hypothesis with no evidence raises.
try: c.Hypothesis(id='H0', statement='x', evidence=[]); print('unexpected')
except ValueError: print('OK: uncited hypothesis rejected by construction')
# 2) Only narrow+reversible+no-data-impact runbooks are auto-remediation eligible.
print('narrow/reversible auto-eligible:',
      c.BlastRadius(c.ResourceScope.SERVICE, c.Reversibility.REVERSIBLE,
                    c.DataSafety.NO_DATA_IMPACT).auto_remediation_eligible)
"
```

## What is deliberately stubbed

Everything with real-world side effects is behind an interface (`Deps` in
`state_machine.py`, `InvestigationAgent` in `tools.py`): live source connectors, the LLM
hypothesis/debate/score behavior, the runbook Executor + dry-run backends, and the durable
checkpoint store. Production supplies these without touching the control-plane, orchestration,
or safety logic. See the "Honest limitations" section of
[docs/design-principles.md](../docs/design-principles.md).
