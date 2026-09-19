# LangGraph Primer — read this before the rest of `LangChain/`

A ground-up introduction to LangChain and LangGraph. Written for someone who knows Python and has
called an LLM API once, and knows nothing else.

**Every term is defined the first time it appears.** If you hit an undefined term, that is a bug in
this primer, not something you were supposed to already know.

## Read in order

| # | File | What you'll be able to do afterwards |
|---|---|---|
| 00 | [Start Here](00-start-here.md) | Say what these libraries are for, and whether you need them |
| 01 | [Graphs and State](01-graphs-and-state.md) | Build and run a multi-step graph; reason about shared data and reducers |
| 02 | [Deciding What Runs Next](02-control-flow.md) | Route on data, fan out in parallel, terminate loops safely |
| 03 | [Pausing and Resuming](03-persistence.md) | Survive restarts, resume a two-day-old conversation, avoid double-charging on replay |
| 04 | [Asking a Human](04-human-in-the-loop.md) | Stop for approval and pick up correctly days later |
| 05 | [Agents and Tools](05-agents-and-tools.md) | Understand exactly what an "agent" is; write good tools; keep the loop bounded |
| 06 | [When One Agent Isn't Enough](06-multi-agent.md) | Choose between the multi-agent patterns, with real costs |
| 07 | [Making It Real](07-production.md) | Trace, test, and cost an agent you'd put in front of users |

Roughly 4,000 lines. Files 02–07 run long because they explain rather than assert — skim the
headings and read the parts you need.

## Then: the worked designs

The primer teaches mechanisms. These take one problem each and go all the way down to engineering
judgment — which is where the interesting decisions are.

- **[Supervisor vs. Swarm](../../AISystemDesign/SupportAgent/EXPLAINED.md)** — how to structure a
  support agent covering five specialist areas. Start with this one; it's the most complete, and
  Parts 2 and 3 continue it into state/memory and safety.
- **[Model Tiering](../../AISystemDesign/ModelTiering/EXPLAINED.md)** — which model to run on which
  step of a pipeline, and why the intuitive answer is backwards. Its arithmetic is executable:
  `python3 ../../AISystemDesign/ModelTiering/reference_impl/economics.py`.

## And: 25 design problems

[SCENARIOS.md](../../AISystemDesign/SCENARIOS.md) — one brief per scenario, each with the trap most
people miss, the approach, the decisions you'd have to defend, and how it fails in production. Useful
for design-interview practice.

## What about the other 30 files in `LangChain/`?

[`../00-README.md`](../00-README.md) indexes them. They are **dense reference notes** — accurate,
good for looking something up, and written assuming you already know everything in this primer. Read
them after, not instead.

## A caveat on API details

This reflects LangChain v1.x and LangGraph 1.2+. The concepts are stable; exact argument names move.
Check `https://docs.langchain.com/llms.txt` before depending on a signature.
