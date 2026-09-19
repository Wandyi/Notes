# 01 — Graphs and State

In [00](00-start-here.md) you saw *why* LangGraph asks you to describe your program as a graph
instead of a loop. This file is the *how*.

By the end you'll have built and run a working graph, and you'll understand the one concept that
causes more confusion than everything else combined: **state and its reducers**.

Everything here runs. The first examples deliberately use no LLM at all, because when you're
learning graph mechanics an LLM in the middle is just noise.

---

## 1. Your first graph

Three ideas, and they're smaller than they sound.

- A **node** is a Python function.
- An **edge** says which node runs after which.
- A **graph** is nodes plus edges — an executable flowchart.

Here's a complete program. Copy it and run it.

```python
from typing import TypedDict
from langgraph.graph import StateGraph, START, END

# The shape of the data our nodes share. More on this in section 2.
class State(TypedDict):
    text: str

# A node is a plain function. It takes the state, returns what changed.
def shout(state: State) -> dict:
    return {"text": state["text"].upper()}

def punctuate(state: State) -> dict:
    return {"text": state["text"] + "!"}

builder = StateGraph(State)          # declare the data shape
builder.add_node("shout", shout)     # register the functions as nodes
builder.add_node("punctuate", punctuate)

builder.add_edge(START, "shout")         # START is where execution begins
builder.add_edge("shout", "punctuate")   # then this
builder.add_edge("punctuate", END)       # then stop

graph = builder.compile()            # turn the description into something runnable

print(graph.invoke({"text": "hello"}))
# {'text': 'HELLO!'}
```

That's a graph. `START` and `END` are built-in markers for the entry and exit points.

**Why this instead of `punctuate(shout("hello"))`?** For this program, no reason at all — the
function call is better. The graph earns its keep once you want to route dynamically
([02](02-control-flow.md)), pause and resume ([03](03-persistence.md)), or ask a human
([04](04-human-in-the-loop.md)). None of those are possible with nested function calls, and all of
them are nearly free once your program is a graph.

You can also see the graph, which is occasionally very useful:

```python
print(graph.get_graph().draw_ascii())
```

---

## 2. State: the shared data

Every node receives the same object — the **state**. It's a dictionary whose shape you declare up
front with `TypedDict`.

```python
from typing import TypedDict

class SupportState(TypedDict):
    customer_id: str
    question: str
    area: str          # "billing", "orders", ...
    answer: str
```

Declaring the shape isn't ceremony. LangGraph uses it to know which keys exist and — crucially, in
section 4 — how to merge updates to each one.

### Nodes return only what changed

This is the part people get wrong first. A node does **not** return the new state. It returns a
**partial update**, and the framework merges it in.

```python
def classify(state: SupportState) -> dict:
    q = state["question"].lower()
    area = "billing" if "charge" in q or "refund" in q else "orders"
    return {"area": area}       # ONLY the key we touched
```

`customer_id`, `question`, and `answer` are untouched and carry through automatically.

Two mistakes worth naming:

```python
# ❌ Mutating the state object instead of returning an update.
def bad_mutate(state):
    state["area"] = "billing"     # may appear to work; do not rely on it
    return {}

# ❌ Returning a key you never declared.
def bad_key(state):
    return {"aera": "billing"}    # typo — no such channel
```

The second is the one that will cost you an afternoon. Declare your keys, and let your type checker
help you.

---

## 3. Putting a model in it

Now a real node. `ChatAnthropic` is LangChain's wrapper around the provider's API — the same code
works against other providers by swapping this one line, which is most of why the wrapper exists.

```python
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langchain_anthropic import ChatAnthropic

model = ChatAnthropic(model="claude-sonnet-4-6")

class SupportState(TypedDict):
    question: str
    area: str
    answer: str

def classify(state: SupportState) -> dict:
    reply = model.invoke(
        f"Which area handles this? Answer with one word — "
        f"billing, orders, or technical.\n\nQuestion: {state['question']}"
    )
    return {"area": reply.content.strip().lower()}

def answer(state: SupportState) -> dict:
    reply = model.invoke(
        f"You are a {state['area']} specialist. Answer this customer.\n\n"
        f"{state['question']}"
    )
    return {"answer": reply.content}

builder = StateGraph(SupportState)
builder.add_node("classify", classify)
builder.add_node("answer", answer)
builder.add_edge(START, "classify")
builder.add_edge("classify", "answer")
builder.add_edge("answer", END)
graph = builder.compile()

result = graph.invoke({"question": "Why was I charged twice in March?"})
print(result["area"])     # billing
print(result["answer"])
```

Notice what `classify` did: it wrote a *decision* into state. The next node read it. **State is how
nodes communicate** — not return values, not globals.

---

## 4. Reducers: the concept that causes all the confusion

So far every update has *replaced* the old value. `{"area": "billing"}` overwrites whatever `area`
was.

That's right for `area`. It's badly wrong for a conversation.

### The bug you'd get without reducers

A chatbot needs the messages so far. Try it the obvious way:

```python
class ChatState(TypedDict):
    messages: list           # plain list — replace on write

def respond(state: ChatState) -> dict:
    reply = model.invoke(state["messages"])
    return {"messages": [reply]}      # ⚠️
```

Turn 1 works. On turn 2, `messages` becomes `[reply]` — a list of exactly one item. **Every previous
message is gone.** Your bot has amnesia, and the symptom is confusing: it answers turn 1 perfectly
and then behaves as if it just woke up.

You could fix it in the node:

```python
    return {"messages": state["messages"] + [reply]}   # works, but...
```

...now every node that touches `messages` has to remember to do that, and forgetting is silent. Move
the rule to the *channel* instead, where it can't be forgotten.

### The fix

A **reducer** is a function attached to a state key that says how to combine the old value with an
update. You attach it with `Annotated`:

```python
from typing import Annotated
from langgraph.graph.message import add_messages

class ChatState(TypedDict):
    messages: Annotated[list, add_messages]   # APPEND
    area: str                                  # REPLACE (no reducer = replace)
```

Now the naive node is correct:

```python
def respond(state: ChatState) -> dict:
    reply = model.invoke(state["messages"])
    return {"messages": [reply]}      # ✅ appended, not replaced
```

**The rule is short: a key with a reducer combines; a key without one replaces.**

`add_messages` is the one you'll use most. It appends, and it also handles message IDs so that
re-sending a message with the same ID updates it instead of duplicating it — which matters when you
resume from a checkpoint ([03](03-persistence.md)).

For plain accumulation, `operator.add` concatenates lists:

```python
import operator

class ResearchState(TypedDict):
    findings: Annotated[list, operator.add]     # appends
    sources_checked: Annotated[int, operator.add]  # sums
```

And you can write your own — it's just a two-argument function:

```python
def keep_highest_confidence(old: dict, new: dict) -> dict:
    """Merge fact dicts, preferring whichever version we're more sure of."""
    merged = dict(old)
    for key, value in new.items():
        if key not in merged or value["confidence"] > merged[key]["confidence"]:
            merged[key] = value
    return merged

class State(TypedDict):
    facts: Annotated[dict, keep_highest_confidence]
```

### Choosing between them

Ask: **if two updates to this key arrived, would I want both or just the newer one?**

| Key | Want | Reducer |
|---|---|---|
| `messages` | both | `add_messages` |
| `findings` | both | `operator.add` |
| `tokens_used` | the sum | `operator.add` |
| `area` | the newer one | none — replace |
| `active_agent` | the newer one | none — replace |
| `current_step` | the newer one | none — replace |

---

## 5. The third case, which is where reducers get interesting

There's a situation the table above doesn't cover: **two nodes writing the same key at the same
time.**

You'll hit this properly in [02](02-control-flow.md) when you fan out to run things in parallel, but
the concept belongs here because it's a property of the channel, not of the routing.

Suppose three specialists run simultaneously and all three write:

- **`findings`, with `operator.add`** — fine. Both contributions get appended. Order between them
  isn't guaranteed, but nothing is lost.
- **`active_agent`, with no reducer** — LangGraph raises `InvalidUpdateError`.

That error is easy to read as a framework annoyance. It isn't. "Replace" has no sensible answer when
two things replace at once — *which* one should win? By raising, the framework is telling you that
two branches are fighting over a value that only makes sense as a single value.

**The right fix is almost always to change your design so only one node writes that key.** The
tempting fix — adding a reducer to make the error stop — silences the alarm without fixing the
problem, and you get a nondeterministic winner instead of a crash. A crash you can debug.

And one genuinely nasty case to know about now: **parallel writes to `messages` raise nothing.**
`add_messages` appends happily, so two branches both writing produce an interleaved transcript in
nondeterministic order. There's no error because, as far as the channel is concerned, appending twice
is exactly what it's for. This one has to be prevented by design.

---

## 6. A complete example you can run

Everything so far, in one working program.

```python
import operator
from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, START, END
from langchain_anthropic import ChatAnthropic

model = ChatAnthropic(model="claude-sonnet-4-6")

class State(TypedDict):
    messages: Annotated[list, operator.add]   # grows
    area: str                                  # replaced
    lookups: Annotated[int, operator.add]      # counts up

def classify(state: State) -> dict:
    question = state["messages"][-1]["content"]
    reply = model.invoke(
        f"One word — billing, orders, or technical. Question: {question}"
    )
    return {"area": reply.content.strip().lower()}

def look_up(state: State) -> dict:
    # Pretend this is a database call.
    return {"lookups": 1, "messages": [{"role": "system", "content": "Order 88213: held at Reno."}]}

def respond(state: State) -> dict:
    context = "\n".join(m["content"] for m in state["messages"])
    reply = model.invoke(f"You are a {state['area']} specialist.\n\n{context}")
    return {"messages": [{"role": "assistant", "content": reply.content}]}

builder = StateGraph(State)
for name, fn in [("classify", classify), ("look_up", look_up), ("respond", respond)]:
    builder.add_node(name, fn)
builder.add_edge(START, "classify")
builder.add_edge("classify", "look_up")
builder.add_edge("look_up", "respond")
builder.add_edge("respond", END)
graph = builder.compile()

out = graph.invoke({
    "messages": [{"role": "user", "content": "Where is order 88213?"}],
    "area": "",
    "lookups": 0,
})

print("area:    ", out["area"])
print("lookups: ", out["lookups"])       # 1
print("messages:", len(out["messages"]))  # 3 — user, system, assistant
```

Trace the counts yourself: `messages` went 1 → 2 → 3 because it appends. `lookups` went 0 → 1
because it sums. `area` was replaced once. Three keys, three behaviours, all declared in the
`TypedDict`.

---

## 7. Mistakes to expect

| Symptom | Cause |
|---|---|
| Bot forgets everything each turn | `messages` has no reducer, so each write replaces |
| A key is mysteriously empty | Typo in a returned key — it isn't a declared channel |
| `InvalidUpdateError` on a key | Two parallel nodes writing a replace-channel. Fix the design, don't add a reducer |
| Transcript out of order, no error | Two parallel nodes appending to `messages` |
| Value is a list where you expected a string | A reducer on a key that should replace |
| Changes to state vanish | Node mutated `state` in place instead of returning an update |

---

## What to take away

1. **A node is a function, an edge says what's next, a graph is both.** The value isn't in this
   file's examples — it's that a graph can be saved, resumed, and inspected, which a loop cannot.
2. **Nodes return partial updates, not the new state.** Return only the keys you changed.
3. **State is how nodes talk to each other.** Not return values, not globals.
4. **A key with a reducer combines updates; a key without one replaces.** Decide per key by asking:
   if two updates arrived, do I want both or the newer one?
5. **`InvalidUpdateError` is the framework catching a design bug.** Two branches are fighting over a
   single-valued key. Change which node writes it rather than adding a reducer to quiet it.
6. **Parallel appends to `messages` fail silently.** No error, interleaved transcript. Prevent it by
   design.

Next: [02 — Deciding What Runs Next](02-control-flow.md), where the edges stop being fixed.
