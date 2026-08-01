"""The orchestrator: plan → dependency-aware parallel fan-out → synthesize.

Ties the stages together and owns the three staff-level decisions:

1. Task decomposition        — delegated to the planner (structured output).
2. The interface contract    — ``ResearchFinding`` in `contracts.py`.
3. Failure / re-delegation   — bounded retries per researcher, dependency-cycle
                               breaking, and failed sub-questions surfaced to
                               the synthesizer instead of crashing the run.

Scheduling is wave-based: every sub-question whose dependencies are satisfied
runs concurrently (bounded by a semaphore); dependents run in the next wave with
their upstream findings injected as context.
"""

from __future__ import annotations

import asyncio
import time

from anthropic import AsyncAnthropic

from . import planner, researcher, synthesizer
from .config import Settings
from .contracts import FindingStatus, ResearchFinding, SubQuestion
from .scratchpad import Scratchpad
from .tracing import Span, Tracer


class Orchestrator:
    def __init__(
        self,
        settings: Settings | None = None,
        client: AsyncAnthropic | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.client = client or AsyncAnthropic()
        self.tracer = Tracer(self.settings)
        self.scratchpad = Scratchpad()
        self._sem = asyncio.Semaphore(self.settings.max_concurrency)

    async def run(self, question: str, *, verbose: bool = True) -> str:
        wall_start = time.monotonic()

        def log(msg: str) -> None:
            if verbose:
                print(msg, flush=True)

        # 1. Plan --------------------------------------------------------------
        log(f"▸ Planning: {question!r}")
        plan = await planner.plan(self.client, self.settings, self.tracer, question)
        log(f"  interpretation: {plan.interpretation}")
        for sq in plan.sub_questions:
            dep = f" (depends on {sq.depends_on})" if sq.depends_on else ""
            log(f"  Q{sq.id}: {sq.question}{dep}")

        # 2. Dependency-aware parallel fan-out --------------------------------
        pending: dict[int, SubQuestion] = {sq.id: sq for sq in plan.sub_questions}
        done: set[int] = set()
        wave = 0

        while pending:
            ready = [sq for sq in pending.values() if set(sq.depends_on) <= done]
            if not ready:
                # No sub-question is runnable: a dependency cycle or a reference
                # to a failed/absent id. Break it by running everything left —
                # a stuck schedule must not deadlock the run.
                log("  ! dependency cycle / unresolved deps — breaking, running remainder")
                ready = list(pending.values())

            wave += 1
            log(f"▸ Wave {wave}: dispatching {len(ready)} researcher(s) "
                f"(max {self.settings.max_concurrency} concurrent)")
            results = await asyncio.gather(*(self._run_one(sq) for sq in ready))

            for finding in results:
                await self.scratchpad.put(finding)
                done.add(finding.sub_question_id)
                pending.pop(finding.sub_question_id, None)
                status = "ok" if finding.ok else f"FAILED ({finding.error})"
                log(f"  Q{finding.sub_question_id} {status}, "
                    f"{len(finding.sources)} source(s), {finding.attempts} attempt(s)")

        # 3. Synthesize --------------------------------------------------------
        findings = await self.scratchpad.all()
        n_ok = sum(1 for f in findings if f.ok)
        log(f"▸ Synthesizing {n_ok}/{len(findings)} successful findings")
        report = await synthesizer.synthesize(
            self.client, self.settings, self.tracer, question, plan, findings
        )

        if verbose:
            print("\n" + self.tracer.summary(time.monotonic() - wall_start) + "\n")
        return report

    async def _run_one(self, sub_question: SubQuestion) -> ResearchFinding:
        """Run one researcher with bounded retries; never raises."""
        async with self._sem:
            upstream = await self.scratchpad.get(sub_question.depends_on)
            context = self.scratchpad.context_for(upstream)

            last_error = "unknown error"
            total_attempts = self.settings.max_researcher_retries + 1
            for attempt in range(1, total_attempts + 1):
                try:
                    return await researcher.research(
                        self.client,
                        self.settings,
                        self.tracer,
                        sub_question,
                        context,
                        attempt,
                    )
                except Exception as exc:  # noqa: BLE001 — deliberate: isolate subagent failure
                    last_error = f"{type(exc).__name__}: {exc}"
                    if attempt <= self.settings.max_researcher_retries:
                        await asyncio.sleep(2 ** (attempt - 1))  # 1s, 2s, 4s backoff

            # Exhausted retries — record a failed span and return a FAILED
            # finding so the run continues and the synthesizer sees the gap.
            await self.tracer.record(
                Span(
                    name=f"researcher[{sub_question.id}]",
                    model=self.settings.researcher_model,
                    ok=False,
                )
            )
            return ResearchFinding(
                sub_question_id=sub_question.id,
                question=sub_question.question,
                status=FindingStatus.FAILED,
                summary="",
                attempts=total_attempts,
                error=last_error,
            )
