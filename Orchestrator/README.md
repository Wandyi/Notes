# Fan-out Research Agent

A multi-agent **orchestrator** built on the Claude API. One broad question in,
one cited report out — via a planner that decomposes the question, researcher
subagents that investigate the pieces **in parallel** (each in its own isolated
context, with server-side web search), a shared scratchpad, and a synthesizer
that merges everything.

This is project #7 (*fan-out research/analyst orchestrator*) from the
multi-agent orchestration set. It's built to show the three decisions that make
orchestration non-trivial rather than to be a toy demo:

- **Task decomposition** — prefer independent sub-questions; model real
  dependencies explicitly.
- **The interface contract** — a narrow, structured boundary (`ResearchFinding`)
  is what lets each subagent hold an isolated context.
- **Failure / re-delegation policy** — bounded retries, dependency-cycle
  breaking, and failed sub-questions surfaced (not swallowed) to the synthesizer.

See **[ARCHITECTURE.md](ARCHITECTURE.md)** for the diagram and a stage-by-stage
walkthrough (or open [architecture.svg](architecture.svg)).

## Layout

```
Orchestrator/
├── main.py                     # CLI entry point
├── architecture.svg            # rendered architecture diagram
├── ARCHITECTURE.md             # diagram (Mermaid) + design walkthrough
├── requirements.txt
├── .env.example
└── fanout_research/
    ├── config.py               # Settings + model pricing (all tunables)
    ├── contracts.py            # the orchestrator↔subagent interface (Pydantic + dataclasses)
    ├── scratchpad.py           # asyncio-safe shared finding store (dedupe)
    ├── tracing.py              # per-span tokens / latency / cost, run summary
    ├── planner.py              # decompose → ResearchPlan (structured output)
    ├── researcher.py           # subagent: research ONE sub-question (web_search loop)
    ├── synthesizer.py          # merge findings → cited report (streamed)
    └── orchestrator.py         # plan → wave-scheduled parallel fan-out → synthesize
```

## Setup

Requires Python 3.10+.

```bash
cd Orchestrator
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...      # or cp .env.example .env and fill it in
```

## Run

```bash
python main.py "What are the security and performance tradeoffs of DNS-over-HTTPS for enterprise networks?"
```

Useful flags:

```bash
# Run the leaf researchers on a cheaper/faster model, keep planning + synthesis on Opus
python main.py --researcher-model claude-sonnet-5 --max-concurrency 6 "..."

# Just the report, no progress log or trace table
python main.py --quiet "..."
```

## Use as a library

```python
import asyncio
from fanout_research import Orchestrator, Settings

async def main():
    orch = Orchestrator(Settings(max_concurrency=6))
    report = await orch.run("Compare WireGuard and IPsec for site-to-site VPNs.")
    print(report)

asyncio.run(main())
```

## How it works (short version)

1. **Plan** — the planner returns a validated `ResearchPlan`: an interpretation
   plus a list of `SubQuestion`s, each with a `depends_on` list. Independent
   sub-questions have empty deps and fan out in parallel.
2. **Fan out** — the orchestrator schedules sub-questions in **waves**: every
   sub-question whose dependencies are satisfied runs concurrently (bounded by a
   semaphore). Each researcher runs an agentic `web_search` loop in an isolated
   context and returns exactly one `ResearchFinding`.
3. **Collect** — findings land in a shared, asyncio-safe scratchpad. Dependent
   researchers read their upstream findings from it as context, so they build on
   prior work instead of re-discovering it.
4. **Synthesize** — a separate model call reads the whole finding set and
   produces one report with inline numbered citations, flagging any
   sub-questions that failed.

Every model call is traced; at the end you get a cost/latency table and the
parallel speedup (summed model time ÷ wall clock).

## Notes on the Claude API usage

- Model: defaults to `claude-opus-4-8` for every role, with **adaptive
  thinking** on. Researchers and synthesizer set `effort` explicitly.
- Web search uses the server-side `web_search_20260209` tool; the researcher
  loop handles `pause_turn` (the server-tool iteration cap) with a bounded
  number of continuations.
- The planner uses **structured outputs** (`messages.parse`) so the
  decomposition comes back schema-validated.
- The synthesizer **streams** its response (`messages.stream` +
  `get_final_message`) since a report can be long.
```
