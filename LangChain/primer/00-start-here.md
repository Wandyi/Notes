# 00 — Start Here

This primer teaches LangChain and LangGraph from zero.

**What you need to know already:** Python, and you've called an LLM API at least once (any provider,
even just `curl`).

**What you don't need:** any prior exposure to LangChain, LangGraph, "agents", "chains", or
"orchestration frameworks".

Every term gets defined the first time it appears. If you hit an undefined term, that's a bug in
this primer — the other files in `LangChain/` are terse reference notes that assume you already know
this material, and they're the ones to read *after* this.

---

## 1. The problem these libraries exist to solve

Start with something that works. Here's a program that answers a question:

```python
import anthropic
client = anthropic.Anthropic()

def answer(question: str) -> str:
    reply = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": question}],
    )
    return reply.content[0].text

print(answer("What is the capital of France?"))
```

That's fine. No framework needed. **If your problem looks like this, you do not need LangChain, and
adding it will make your life worse.** Genuinely — a lot of people reach for a framework at this
stage and regret it.

Now let's make it a real product and watch it fall apart.

### Requirement 1: it should be able to look things up

The model doesn't know your customer's order status. So you give it a function it can ask you to
call:

```python
def get_order(order_id: str) -> dict:
    return db.query("SELECT * FROM orders WHERE id = ?", order_id)
```

Now your program needs a loop: ask the model, and if the model says "please call `get_order` with
88213", call it, feed the result back, and ask again. Maybe it wants another lookup. Loop until it
stops asking.

Still manageable. Maybe 40 lines.

### Requirement 2: some things need a human to approve

The model wants to refund $49. Your company's rule is that a human approves refunds.

So your loop has to *stop*, show something to a human, and wait. How long? Could be 30 seconds.
Could be Friday afternoon to Monday morning. Your web request cannot stay open for three days, so
"wait" has to mean *"save everything and come back later"*.

This is where the 40 lines start to hurt. What exactly do you save? Where? How do you pick back up
in the middle of a loop?

### Requirement 3: it has to survive a deploy

You ship code on Tuesday. There are 200 conversations in flight, six of them waiting on a human
approval. When the new process starts, all of them need to still work.

That means the conversation's state cannot live in your process's memory. It has to be in a
database — and something has to know how to reconstruct "where were we?" from that database.

### Requirement 4: five teams need to own five pieces

Finance owns refund rules. Logistics owns shipping rules. They ship on different schedules and
neither can test without touching the other's code, because it's all one prompt in one file.

### Requirement 5: someone asks what happened

Six months later: *"conversation #4471 refunded a customer $2,000. Walk me through how that
happened."*

You have logs. They say the model was called 14 times. They don't say what it was thinking, which
tools it used, what it saw when it decided, or which version of your prompt was live that day.

### The pattern

Look at requirements 2, 3, and 5. They're all the same underlying problem:

> **Your program's progress needs to be inspectable, savable, and resumable — from the outside.**

A `while` loop can't do that. Not because loops are bad, but because a loop's position lives in the
Python interpreter's stack, and you can't serialize that to Postgres, or draw it, or ask it "where
are you?"

**So you have to describe your program as data instead of as control flow.** That's the trade
LangGraph asks you to make. You stop writing a loop and start describing a *graph* — a set of steps
and the rules for moving between them. A graph is a data structure. You can save your position in
it, resume from that position, inspect it, and draw it.

That's it. That's the whole pitch. Everything else follows from it.

---

## 2. LangChain vs. LangGraph — the genuinely confusing part

Two names, one ecosystem, and the boundary moved over time. Here's the current shape.

**LangGraph is the execution engine.** It runs your graph. It owns:

- the graph itself — steps and the transitions between them
- the shared data those steps read and write (called **state**)
- saving state after every step so you can pause and resume (**checkpointing**)
- stopping to ask a human, and picking up later (**interrupts**)
- running steps in parallel

**LangChain is the model-and-tool layer that sits on top.** It owns:

- a uniform way to call any provider's model, so swapping providers isn't a rewrite
- turning your Python functions into things a model can call (**tools**)
- `create_agent` — a prebuilt "call the model, run the tools it asks for, repeat" loop, so you
  don't write requirement 1 yourself
- getting typed objects out of a model instead of prose (**structured output**)

**A rough analogy, imperfect but useful:** LangGraph is the web framework and LangChain is the
library of request handlers. You can use LangGraph without LangChain — plenty of people run graphs
whose nodes call provider SDKs directly. Using LangChain's `create_agent` without LangGraph is
harder, because `create_agent` is built on it.

There's also **LangSmith**, which is a separate hosted product for tracing and evaluation — seeing
what your agent actually did. It's not required to run anything, and it's covered in
[07 — Making It Real](07-production.md).

### One historical note that will save you confusion

If you search for LangChain help, you'll find a lot of material about `LLMChain`, `SequentialChain`,
and `AgentExecutor`. **That's the older generation of the API.** It still appears in tutorials and
Stack Overflow answers. Modern code uses graphs and `create_agent` instead.

If a snippet you found uses `LLMChain`, it's not wrong, it's just old — and mixing the two styles is
a reliable way to confuse yourself. Prefer what's in this primer.

> **The API surface moves quickly.** Everything here reflects LangChain v1.x / LangGraph 1.2+. Check
> the current docs at `https://docs.langchain.com/llms.txt` before depending on an exact signature.
> The *concepts* below are stable; argument names occasionally aren't.

---

## 3. Should you use this at all?

An honest section, because the answer is often no.

**You probably don't need LangGraph if:**

- One model call answers the question. Just call the API.
- Your steps are fixed and known in advance and nothing needs to pause. A plain Python function is
  clearer, easier to test, and easier to hire for.
- Your whole interaction finishes inside one HTTP request and you don't need to resume it.

**You probably do need it if:**

- Work has to survive a restart, or pause for a human, or run for longer than a request.
- Something must decide *at runtime* which step comes next, and you can't enumerate the paths.
- Several teams need to own several pieces behind stable interfaces.
- You need to answer "what happened in run #4471" months later.

**The cost is real.** You're adopting an abstraction. Stack traces get longer. Debugging means
understanding the framework's execution model as well as your own code. Your team has to learn it.
That's worth paying for durable, inspectable, resumable execution — and it's a bad deal for a
question-answering endpoint.

---

## 4. How to read this primer

In order. Each file assumes the previous ones.

| File | What you'll be able to do after it |
|---|---|
| **00 — Start Here** (you are here) | Explain what these libraries are for and whether you need them |
| [01 — Graphs and State](01-graphs-and-state.md) | Write and run a real multi-step graph, and reason about its shared data |
| [02 — Deciding What Runs Next](02-control-flow.md) | Route dynamically, fan out to run things in parallel, and terminate loops safely |
| [03 — Pausing and Resuming](03-persistence.md) | Survive restarts, resume a two-day-old conversation, and avoid double-charging on replay |
| [04 — Asking a Human](04-human-in-the-loop.md) | Stop for approval and pick up correctly days later |
| [05 — Agents and Tools](05-agents-and-tools.md) | Understand exactly what an "agent" is, write good tools, and keep the loop under control |
| [06 — When One Agent Isn't Enough](06-multi-agent.md) | Choose between the multi-agent patterns, with the costs |
| [07 — Making It Real](07-production.md) | Trace, test, and cost an agent you'd put in front of users |

**Then read the two worked designs.** They take one problem each and go all the way down, and
they're where the primer's concepts turn into engineering judgment:

- [Supervisor vs. Swarm](../../AISystemDesign/SupportAgent/EXPLAINED.md) — how to structure a
  support agent with five specialist areas. Start here; it's the most complete.
- [Model Tiering](../../AISystemDesign/ModelTiering/EXPLAINED.md) — which model to use on which
  step of a pipeline, and why the intuitive answer is backwards.

**And there's a scenario collection** — [SCENARIOS.md](../../AISystemDesign/SCENARIOS.md) — with 25
design problems in this space, each with the trap, the approach, and the decisions you'd have to
defend. Good for interview practice.

The other 30 files in [`LangChain/`](../00-README.md) are dense reference notes. They're accurate
and useful for looking things up, but they assume everything this primer teaches. Read them
afterwards, not instead.

---

## 5. Getting set up

```bash
pip install langgraph langchain langchain-anthropic
export ANTHROPIC_API_KEY=sk-...
```

Every code sample in this primer is meant to run. Where a sample is a fragment rather than a
complete program, it's marked as such.

---

## What to take away

1. **The real problem is not "calling an LLM".** It's that a program whose progress must be saved,
   inspected, and resumed can't keep that progress in a Python call stack. That's why the framework
   asks you to describe your program as a graph.
2. **LangGraph runs your graph and owns state, persistence, and pausing. LangChain provides the
   model and tool layer on top.** LangSmith is a separate product for seeing what happened.
3. **`LLMChain` and `AgentExecutor` are the older API.** You'll find them in search results. Don't
   mix them with what's here.
4. **The framework is not free.** If one model call answers your question, don't adopt it.

Next: [01 — Graphs and State](01-graphs-and-state.md), where you'll build one.
