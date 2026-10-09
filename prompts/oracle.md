You write a SLOW, LITERAL reference implementation and a random input generator, for testing a solution you will never see. Follow the statement sentence by sentence: loop where it loops, build what it describes, and take the obvious reading over the clever one. Inputs are tiny and efficiency is irrelevant — only faithfulness to the text matters. Python 3.11, standard library only.

{{io_rules}}

{{oracle_signature}}

`reference()` must `raise ValueError` the moment the input violates any precondition the statement states — a value out of its stated range, a repeat where the statement says distinct, a name or position that must currently exist and does not, one that must never have appeared before and has, an ordering the statement requires, a limit expressed against the current size or state. Raise it at the point the violation happens, while you already hold the state that proves it; do not pre-scan. This is the only place preconditions are enforced, so a precondition you do not raise on is a precondition nothing in the system checks.

Careful: an operation the statement *itself* calls invalid and gives a result for is NOT a precondition violation. The statement says what that operation does (returns 0, is ignored, prints an error line); `reference()` returns exactly that. Only an input the statement says cannot occur is a `ValueError`.

Never raise on the container *type* of an argument — list versus tuple: the harness may hand you either spelling of the same input, so treat every sequence as a sequence and never `isinstance`-check the container an argument arrived in. A container type is not a precondition unless the statement makes it one.

Any other exception out of `reference()` — `IndexError`, `KeyError`, `TypeError`, `UnboundLocalError` — is a bug in your reference, not an invalid input, and it is counted as one.

Then define `validate(...)` as exactly this, copied verbatim, with no changes and nothing added:

```
def validate(*args):
    try:
        reference(*args)
    except ValueError:
        return False
    return True
```

One reading of the preconditions, in one function. Do not write a second parser.

Then define `bounds(...)`, taking exactly the same arguments as `reference`, as a cheap check of the **stated bounds of the input** and nothing else. It exists because `reference()` is far too slow to finish on a maximum-size input, so without it nothing in the system can say whether such an input is legal at all.

* Return `None` when every numeric and structural bound the statement states holds: counts and sizes, sequence and name lengths, value ranges, nesting or reference depth, and any other quantity the statement bounds by a number.
* Otherwise return a short string naming the first bound that is violated, for example `"there are 300000 items, the stated maximum is 200000"`.
* It must be **linear** in the size of the input: look at the input, never simulate the operations, never rebuild the state, never call `reference()`. It is run on inputs far larger than anything `reference()` can process.
* It must **never raise**. Guard every lookup; a `bounds()` that raises gives no verdict at all and the input goes unjudged.
* Say nothing about preconditions that depend on evolving state — "must currently exist", "must never have appeared before", an ordering that depends on earlier operations. Those need the simulation `reference()` does, and this function is deliberately the part that does not.

Also define `gen(seed: int, mode: str)` using only `random.Random(seed)` for randomness. {{gen_returns}}

* `mode == "small"`: sizes at most 6, values at most 20. Include boundary shapes — the empty case where the statement allows one, a single element, repeated or tied values, and first, last and adjacent positions.
* `mode == "medium"`: sizes at most 12, but push any count, repetition, capacity, demand or multiplier that the statement bounds by a huge number up to roughly 10^4–10^5, so closed-form arithmetic gets exercised while your literal loops still finish in seconds.
* Both modes: **at least half of the inputs you return must stay valid all the way to the end and drive every bounded quantity the statement names — a depth, a count of live items, a repetition, a capacity, a length — to its bound at least once.** An input that keeps the structure small, or that goes invalid before the deep state is reached, tests nothing: a solution that is wrong about a whole clause of the statement then disagrees on one input in a hundred and reads as a boundary bug. If reaching the bound needs a specific order of operations, build that order deliberately instead of sampling and hoping. The one mode of the statement you think is most likely to be implemented wrongly must appear within the first few seeds, not somewhere in the tail.
* `medium` must include at least one input where the largest bounded quantity is at the maximum that mode allows.
* **The answers must vary.** Every identifier, name, key, index or position an operation refers to must be drawn from what you generated earlier in that same input, never invented independently — otherwise almost every operation is invalid on arrival, every input returns the same answer, and a hundred comparisons assert one and the same fact. Before returning, check that your inputs do not all produce the same result.
* `mode == "large"`: one input at full scale, built from the same state model as the other two. Sizes and counts go to the smaller of the statement's stated maximum and what you can build in **about three seconds**, and every value goes to its stated maximum. Check a clock as you build and stop growing once you approach that budget, returning what you have — a generator that never returns contributes nothing. Prefer bulk construction (comprehensions and slicing over whole ranges) to a per-element loop. Never exceed a stated limit: an input larger than the statement allows is invalid and is thrown away, so stop growing at the stated maximum even when time remains. Every bounded quantity the statement names must reach its bound, and the input must still be one `reference()` accepts. This input exists only to time a solution — `reference()` is far too slow to run on it, and nothing will ask it to.

## Generate only inputs the statement calls valid

`reference()` and `gen()` must agree: every input `gen()` returns must run through `reference()` without raising, or the input is thrown away and the comparison it would have produced is lost. Write the precondition list once, raise on every entry of it in `reference()`, and build `gen()` from that same list.

This is the most common way this task is failed, and an invalid input makes every later comparison meaningless. Before writing `gen`, list the preconditions the statement states — value ranges, distinctness, "must currently exist", "never appeared before", ordering, and any limit expressed against the current size or state — and honor every one.

* Build operation sequences from a running model of the state, never by sampling blindly. At each step choose only from the operations that are legal in the state at that moment, apply the chosen one to your model, then choose the next. If nothing is legal, stop early and return a shorter input.
* Get distinctness by construction: draw from a pool without replacement, rather than sampling and hoping for no collision.
* Assert the invariants you relied on just before returning. A generator that raises is far better than one that quietly returns something invalid.

When the statement describes a state machine, initialize the complete initial state before the first operation and apply every transition the statement describes. An operation the statement calls invalid is still a valid test input: `reference()` returns the result the statement specifies for it and does not raise.

Before answering, hand-trace one sequence that covers the initial state, each kind of operation once, and one operation the statement calls invalid, and check the trace against `reference()`.

## Python pitfalls that have broken this exact task

* Never assign to a name you also read inside the same function, and never to an imported module name. Both `random = random.Random(seed)` and `items = [x for x in items if ...]` raise `UnboundLocalError`. Bind the generator to a fresh name such as `rng`.
* Never draw from a possibly-empty range. Guard every `randrange`, `randint`, `choice` and `sample` so the range or sequence is non-empty, and skip that step when it is not.
* Return exactly the shape described above and nothing else.

Respond with exactly one block:

===ORACLE===
python source defining reference, gen, validate, and bounds
===END===

Problem statement:

{{statement}}
