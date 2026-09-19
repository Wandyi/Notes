# 16 — Time Travel & Debugging

## 1. Concepts

Because every super-step is checkpointed, a thread is an append-only log of `StateSnapshot`s. That
gives you two operations that most systems don't have:

- **Replay** — re-execute from a prior checkpoint with the same state.
- **Fork** — modify state at a prior checkpoint (`update_state`) and execute a *new* branch.

Both are the same call — `invoke`/`stream` with a `checkpoint_id` in the config — the difference is
whether you changed state first.

### What replays and what doesn't

Nodes **before** the target checkpoint are skipped (their results are already saved). Nodes
**after** it re-execute — including LLM calls, API requests, and `interrupt()`s. Replay is not free
and it is not side-effect-free.

### `update_state` and `as_node`

`update_state(config, values, as_node="node_x")` creates a **new checkpoint** attributed to
`node_x`. Execution then resumes at `node_x`'s successors. It never mutates history; the original
checkpoint remains, and `metadata["source"] == "update"` marks the fork.

Values pass through reducers — so a reduced channel accumulates. Use `Overwrite` to replace.

## 2. How to implement

### Inspect history

```python
config = {"configurable": {"thread_id": "conv-123"}}
history = list(graph.get_state_history(config))       # newest first

for s in history:
    print(s.metadata["step"], s.next, s.metadata.get("source"),
          [t.name for t in s.tasks], [t.error for t in s.tasks])
```

Useful filters:

```python
before_write = next(s for s in history if s.next == ("write_joke",))
forks        = [s for s in history if s.metadata["source"] == "update"]
paused       = next(s for s in history if s.tasks and any(t.interrupts for t in s.tasks))
failed       = [s for s in history if any(t.error for t in s.tasks)]
```

### Replay

```python
target = next(s for s in history if s.next == ("write_joke",))
graph.invoke(None, target.config)     # re-runs write_joke onward with the same state
```

### Fork

```python
forked = graph.update_state(target.config, {"topic": "cats and dogs"})
graph.invoke(None, forked)            # new branch from the modified state
```

### Fork "as if" a node produced it

```python
forked = graph.update_state(config, {"topic": "space"}, as_node="generate_topic")
graph.invoke(None, forked)            # resumes at generate_topic's successor
```

### Forking around interrupts

Replaying to a point before an `interrupt()` re-pauses there, waiting for a **new**
`Command(resume=...)`. You can therefore replay a human decision with a different answer — extremely
useful for reproducing "what would have happened if the approver said no".

With multiple sequential interrupts, forking from between them preserves the first answer and
re-asks the second.

### Debugging in Studio

LangSmith Studio (`langgraph dev` locally, or the deployed Studio) gives you: the graph
visualisation, live state at each step, editing state and re-running from a node, and interrupt
inspection. It's the fastest loop for "why did it take that edge".

### Debug stream modes

```python
for part in graph.stream(inputs, stream_mode=["tasks", "checkpoints"], version="v2"):
    ...   # task start/finish with results and errors, plus checkpoint events
```

Both require a checkpointer.

## 3. Scenarios

| Scenario | Technique |
|---|---|
| "The agent gave a wrong answer at 14:32" | Load the thread, walk `get_state_history`, find the step where context or routing went wrong |
| Prompt regression triage | Fork the thread at the pre-model checkpoint, change the prompt via context, replay, diff outputs |
| Reproducing a rejected approval | Replay to the interrupt, resume with the opposite decision |
| Recovering a stuck production thread | Inspect `next` and `tasks[].error`; `update_state` to a safe state; resume |
| Building an eval dataset | Extract `(state_at_checkpoint, node_output)` pairs from real threads into a LangSmith dataset |
| "Which node failed?" | `tasks` stream mode or `snapshot.tasks[].error` |

## 4. Staff-level considerations

- **Replay re-issues side effects.** Never replay a production thread that will send emails, move
  money, or write to prod. Either replay in an environment with stubbed tools
  (`LLMToolEmulatorMiddleware` or a tool registry swap), or fork onto a copied thread.
- **Give yourself a "copy thread" operation.** The Agent Server has a copy-thread API; self-managed
  deployments should have an equivalent. Debugging on the customer's live thread is how you turn one
  incident into two.
- **Time travel is a support capability, not just a dev tool.** Build a small internal console:
  find thread → view history → inspect state at step → replay in sandbox. It pays for itself the
  first week.
- **History has a retention conflict with TTLs.** `keep_latest` prunes old checkpoints, which
  destroys time travel for that thread. Decide which threads keep full history (regulated flows,
  sampled traffic) and which get pruned.
- **The checkpoint history is PII-bearing.** Anyone who can time-travel can read everything the user
  said. Gate the console with real authz and audit its use.
- **Determinism debt shows up here first.** If replaying the same checkpoint produces a different
  path for non-model reasons (timestamps, uuids, dict ordering, non-associative reducers), you have
  a correctness bug that will also break resume ([04](04-pregel-runtime-and-execution-model.md)).

## 5. Anti-patterns

- Replaying live customer threads with real tools enabled.
- Using `update_state` as a "fix the data" hammer in production without an audit record.
- Assuming `update_state` overwrites — reduced channels accumulate.
- Relying on time travel when the checkpointer is `InMemorySaver` (no history after restart).
- Debugging by adding `print` statements instead of reading traces + state history.
- Pruning all checkpoints via TTL and then needing forensics.

## 6. Design-review questions

1. Can we replay a production thread safely? What stubs the side effects?
2. Is there a copy-thread path so we never debug on live data?
3. Who can view thread history, and is that access audited?
4. What is our checkpoint retention for threads that may need forensic review?
5. Have we verified that replaying the same checkpoint twice yields the same path?

## References

- `/oss/python/langgraph/use-time-travel`
- `/oss/python/langgraph/checkpointers` (get_state, get_state_history, update_state, replay)
- `/oss/python/langgraph/studio`, `/langsmith/studio`
- `/oss/python/langgraph/interrupts#debugging-with-interrupts`
