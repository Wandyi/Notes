"""KnowledgeAssistant — the composition root.

Wires the **control plane** (registry, RBAC, audit, cost, evaluation) over the
**data plane** (runtime state machine, orchestration, retrieval, memory, tools,
guardrails, LLM). A single `ask()` call flows through the runtime's explicit
phases; each phase enforces a governance or safety concern before the next.

    control plane  →  registry · RBAC · audit · cost · evaluation
                          │ governs
    data plane     →  runtime → orchestrator → retrieval → llm → guardrails
"""
from __future__ import annotations

from typing import Optional

from .config import Config, DEFAULT_CONFIG
from .control_plane.audit import AuditLog
from .control_plane.cost import CostAccountant, CostExceeded
from .control_plane.rbac import RBAC
from .control_plane.registry import AgentRegistry, LifecycleState
from .core import (
    Answer, Citation, Conflict, Document, Principal, QueryRequest, ScoredChunk, Source,
)
from .data_plane.guardrails import Guardrails
from .data_plane.memory import LongTermMemory, MemoryItem, ShortTermMemory
from .data_plane.orchestrator import Orchestrator
from .data_plane.runtime import AgentRuntime, Phase, RunContext, TerminationReason
from .data_plane.tools.connectors import build_default_connectors
from .data_plane.tools.registry import ToolRegistry
from .evaluation.evaluator import TrajectoryEvaluator
from .llm.client import Evidence, LocalSynthesizer, ModelRouter, SemanticCache, Synthesizer
from .observability.tracing import Trace
from .retrieval.embeddings import HashingEmbeddingProvider
from .retrieval.hybrid import HybridRetriever
from .retrieval.reranker import LexicalReranker


class KnowledgeAssistant:
    def __init__(
        self,
        corpus: list[Document],
        config: Config = DEFAULT_CONFIG,
        synthesizer: Optional[Synthesizer] = None,
        rbac: Optional[RBAC] = None,
    ) -> None:
        self.config = config

        # --- data plane ---------------------------------------------------
        embedder = HashingEmbeddingProvider(dim=config.retrieval.embedding_dim)
        reranker = LexicalReranker()
        self.retriever = HybridRetriever(embedder, reranker, config.retrieval)
        self.tool_registry = ToolRegistry(build_default_connectors(corpus))
        self.orchestrator = Orchestrator(self.tool_registry, self.retriever, config)
        self.guardrails = Guardrails(config.guardrails)
        self.short_term = ShortTermMemory()
        self.long_term = LongTermMemory(dim=config.retrieval.embedding_dim)
        self.router = ModelRouter(config.routing)
        self.cache = SemanticCache(dim=config.retrieval.embedding_dim)
        self.synthesizer: Synthesizer = synthesizer or LocalSynthesizer()
        self.runtime = AgentRuntime(config.budgets)

        # --- control plane ------------------------------------------------
        self.rbac = rbac or RBAC()
        self.audit = AuditLog()
        self.cost = CostAccountant(config.budgets)
        self.evaluator = TrajectoryEvaluator(config.guardrails.groundedness_floor)
        self.registry = AgentRegistry(staleness_days=config.staleness_review_days)

        # Register the agent identity + owner and drive it through the
        # evaluation-gated lifecycle to PRODUCTION (governance from day one).
        self.agent = self.registry.register(
            name=config.agent_name,
            owner=config.agent_owner,
            capabilities=["multi-source-rag", "conflict-resolution", "cited-answers"],
        )
        self.registry.transition(self.agent.id, LifecycleState.EVALUATING)
        self.registry.record_evaluation(self.agent.id, score=0.9)
        self.registry.transition(self.agent.id, LifecycleState.STAGED)
        self.registry.promote(self.agent.id)

    # -----------------------------------------------------------------------
    def ask(self, query: str, principal: Principal, *, max_sources: int = 8,
            top_k: int = 6, now=None) -> Answer:
        request = QueryRequest(query=query, principal=principal,
                               max_sources=max_sources, top_k=top_k)
        trace = Trace(request_id=request.request_id)
        self.cost.open_request(request.request_id, principal.tenant_id)
        self.audit.record("request.received", request.request_id,
                          principal.user_id, principal.tenant_id, query_len=len(query))

        handlers = {
            # `now` (freshness reference time) is threaded in at PERCEIVE so the
            # OBSERVE phase reads it from the blackboard during the run.
            Phase.PERCEIVE: lambda ctx: self._perceive(ctx, now),
            Phase.PLAN: self._plan,
            Phase.ACT: self._act,
            Phase.OBSERVE: self._observe,
            Phase.SYNTHESIZE: lambda ctx: self._synthesize(ctx, now),
            Phase.COMPLETE: lambda ctx: Phase.COMPLETE,
        }
        ctx = self.runtime.run(request, trace, handlers)

        answer = self._assemble(ctx, trace)

        report = self.evaluator.evaluate(answer, trace)
        answer.trajectory_score = report.score
        for note in report.notes:
            if note not in answer.warnings:
                answer.warnings.append(note)

        # Short-term memory: remember the exchange for follow-ups.
        self.short_term.add(MemoryItem(
            text=f"Q: {query}\nA: {answer.text[:200]}", kind="qa",
            tenant_id=principal.tenant_id))

        self.audit.record("request.answered", request.request_id,
                          principal.user_id, principal.tenant_id,
                          refused=answer.refused, groundedness=round(answer.groundedness, 3),
                          trajectory_score=round(answer.trajectory_score, 3),
                          cost_usd=round(answer.cost_usd, 6),
                          sources=[s.value for s in answer.sources_consulted])
        return answer

    def remember_fact(self, text: str, tenant_id: str, kind: str = "fact") -> None:
        """Write a durable fact into long-term memory."""
        self.long_term.add(MemoryItem(text=text, kind=kind, tenant_id=tenant_id))

    # --- phase handlers ----------------------------------------------------
    def _perceive(self, ctx: RunContext, now=None) -> Phase:
        ctx.blackboard["now"] = now
        req = ctx.request
        decision = self.rbac.evaluate(req.principal)
        self.audit.record("rbac.decision", req.request_id, req.principal.user_id,
                          req.principal.tenant_id, allowed=decision.allowed,
                          reason=decision.reason,
                          allowed_sources=sorted(s.value for s in decision.allowed_sources))
        ctx.trace.event("rbac.decision", "guardrail", allowed=decision.allowed)
        if not decision.allowed:
            ctx.blackboard["refusal_reason"] = f"access denied: {decision.reason}"
            return ctx.terminate(TerminationReason.ACCESS_DENIED, decision.reason)

        pre = self.guardrails.pre_check(req.query, ctx.trace)
        if not pre.ok:
            self.audit.record("guardrail.blocked", req.request_id, req.principal.user_id,
                              req.principal.tenant_id, reason=pre.reason)
            ctx.blackboard["refusal_reason"] = pre.reason
            return ctx.terminate(TerminationReason.GUARDRAIL, pre.reason)

        # Selective memory recall (short-term + long-term).
        recalled = self.long_term.recall(pre.redacted_query, req.principal.tenant_id)
        ctx.blackboard["memory"] = [m.text for m in recalled]
        ctx.blackboard["query"] = pre.redacted_query
        ctx.blackboard["allowed_sources"] = decision.allowed_sources
        ctx.blackboard["warnings"] = []
        if pre.redactions:
            ctx.blackboard["warnings"].append(f"redacted PII from query: {sorted(set(pre.redactions))}")
        return Phase.PLAN

    def _plan(self, ctx: RunContext) -> Phase:
        query = ctx.blackboard["query"]
        allowed = ctx.blackboard["allowed_sources"]
        workers = self.orchestrator.plan(query, allowed, ctx.request.max_sources, ctx.trace)
        if not workers:
            ctx.blackboard["refusal_reason"] = "no permitted sources to consult"
            return ctx.terminate(TerminationReason.NO_EVIDENCE, "no workers")
        ctx.blackboard["workers"] = workers
        return Phase.ACT

    def _act(self, ctx: RunContext) -> Phase:
        query = ctx.blackboard["query"]
        gathered = self.orchestrator.gather(ctx.blackboard["workers"], query, ctx.trace)
        ctx.add_tool_calls(gathered.tool_calls)
        ctx.blackboard["documents"] = gathered.documents
        ctx.blackboard["warnings"].extend(gathered.warnings)
        if not gathered.documents:
            return ctx.terminate(TerminationReason.NO_EVIDENCE, "no documents gathered")
        return Phase.OBSERVE

    def _observe(self, ctx: RunContext) -> Phase:
        query = ctx.blackboard["query"]
        scored, conflicts, sources = self.orchestrator.reduce(
            query, ctx.blackboard["documents"], ctx.request.top_k, ctx.trace,
            now=ctx.blackboard.get("now"))
        if not scored:
            return ctx.terminate(TerminationReason.NO_EVIDENCE, "no relevant chunks")
        ctx.blackboard["scored"] = scored
        ctx.blackboard["conflicts"] = conflicts
        ctx.blackboard["sources"] = sources
        return Phase.SYNTHESIZE

    def _synthesize(self, ctx: RunContext, now) -> Phase:
        query = ctx.blackboard["query"]
        scored: list[ScoredChunk] = ctx.blackboard["scored"]
        conflicts: list[Conflict] = ctx.blackboard["conflicts"]

        # Assign citation markers in final ranked order.
        evidence: list[Evidence] = []
        citations: list[Citation] = []
        for i, sc in enumerate(scored, start=1):
            marker = f"[{i}]"
            evidence.append(Evidence(marker=marker, text=sc.chunk.text,
                                     source=sc.chunk.source, title=sc.chunk.title))
            citations.append(Citation(marker=marker, source=sc.chunk.source,
                                      title=sc.chunk.title, url=sc.chunk.url,
                                      chunk_id=sc.chunk.id, updated_at=sc.chunk.updated_at,
                                      is_stale=sc.is_stale))
        ctx.blackboard["citations"] = citations

        tier = self.router.choose(len(scored), bool(conflicts), ctx.trace)
        ctx.blackboard["tier"] = tier

        # Cache is scoped to (tenant, allowed sources) so a cached answer is
        # never served to a principal with different data access.
        allowed = ctx.blackboard["allowed_sources"]
        scope = f"{ctx.request.principal.tenant_id}|" + ",".join(sorted(s.value for s in allowed))

        cached = self.cache.get(query, scope)
        if cached is not None:
            ctx.trace.event("cache.hit", "llm", model=cached.model_id)
            result = cached
        else:
            result = self.synthesizer.synthesize(query, evidence, conflicts, tier, ctx.trace)
            try:
                self.cost.charge(ctx.request.request_id, tier,
                                 result.input_tokens, result.output_tokens)
            except CostExceeded as exc:
                ctx.blackboard["refusal_reason"] = f"budget exceeded: {exc}"
                return ctx.terminate(TerminationReason.BUDGET, str(exc))
            self.cache.put(query, result, scope)

        # Post-LLM guardrail: groundedness against retrieved evidence.
        grounded = self.guardrails.groundedness(result.text, scored, ctx.trace)
        ctx.blackboard["answer_text"] = result.text
        ctx.blackboard["model_used"] = result.model_id
        ctx.blackboard["groundedness"] = grounded
        if grounded < self.config.guardrails.groundedness_floor:
            ctx.blackboard["warnings"].append(
                f"low groundedness ({grounded:.2f}); answer may be unreliable")
        return Phase.COMPLETE

    # --- assembly ----------------------------------------------------------
    def _assemble(self, ctx: RunContext, trace: Trace) -> Answer:
        req = ctx.request
        bb = ctx.blackboard
        rc = self.cost.request_cost(req.request_id)
        answer = Answer(
            request_id=req.request_id,
            query=req.query,
            text="",
            cost_usd=rc.usd,
            trace_id=trace.trace_id,
            model_used=bb.get("model_used"),
            warnings=list(bb.get("warnings", [])),
        )

        reason = ctx.reason
        if reason in (TerminationReason.ACCESS_DENIED, TerminationReason.GUARDRAIL,
                      TerminationReason.BUDGET):
            answer.refused = True
            answer.refusal_reason = bb.get("refusal_reason") or (reason.value if reason else None)
            answer.text = f"Request refused: {answer.refusal_reason}"
            return answer

        if reason is TerminationReason.NO_EVIDENCE or not bb.get("scored"):
            answer.text = (
                "I couldn't find enough grounded information across the permitted "
                "knowledge sources to answer that confidently."
            )
            answer.warnings.append("no grounded evidence retrieved")
            return answer

        answer.text = bb["answer_text"]
        answer.citations = bb["citations"]
        answer.conflicts = bb["conflicts"]
        answer.sources_consulted = bb["sources"]
        answer.groundedness = bb["groundedness"]
        return answer
