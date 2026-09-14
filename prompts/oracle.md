You write a SLOW, LITERAL reference implementation and a random input generator, for testing a solution you will never see. Follow the statement sentence by sentence: loop where it loops, build what it describes, and take the obvious reading over the clever one. Inputs are tiny and efficiency is irrelevant — only faithfulness to the text matters. Python 3.11, standard library only.

{{io_rules}}

{{oracle_signature}}

Also define `gen(seed: int, mode: str)` using only `random.Random(seed)` for randomness. {{gen_returns}}

* `mode == "small"`: sizes at most 6, values at most 20. Include boundary shapes — the empty case where the statement allows one, a single element, repeated or tied values, and first, last and adjacent positions.
* `mode == "medium"`: sizes at most 12, but push any count, repetition, capacity, demand or multiplier that the statement bounds by a huge number up to roughly 10^4–10^5, so closed-form arithmetic gets exercised while your literal loops still finish in seconds.

Also define `validate(...)`, taking exactly the same arguments as `reference` above, returning `True` if this input satisfies every precondition the statement states and `False` otherwise. This is what protects the whole test suite from meaningless comparisons — every generated input is checked against it before anything is compared, so a `validate` that says `True` to something the statement forbids defeats the entire point. Check the same precondition list you enumerate below: ranges, distinctness, must-currently-exist, never-appeared-before, ordering, and any limit expressed against the current size or state. It must be a pure predicate — never print, never mutate its arguments, never raise on ordinary invalid input, just return `False`. If it genuinely cannot decide, return `True` rather than guess; a `validate` that rejects too eagerly is worse than one that lets a few bad inputs through.

## Generate only inputs the statement calls valid

`validate()` and `gen()` must agree: every input `gen()` returns must make `validate()` return `True`. Write the precondition list once, write `validate()` from it first, then write `gen()` from the same list, and before answering trace one `gen()` output through `validate()` by hand. A `validate()` that models the state (which identifiers exist, what is currently open, what has been used) must apply every state change the statement describes — the most common failure is a `validate()` that forgets to record what an earlier operation created and then rejects every later reference to it.

This is the most common way this task is failed, and an invalid input makes every later comparison meaningless. Before writing `gen`, list the preconditions the statement states — value ranges, distinctness, "must currently exist", "never appeared before", ordering, and any limit expressed against the current size or state — and honor every one.

* Build operation sequences from a running model of the state, never by sampling blindly. At each step choose only from the operations that are legal in the state at that moment, apply the chosen one to your model, then choose the next. If nothing is legal, stop early and return a shorter input.
* Get distinctness by construction: draw from a pool without replacement, rather than sampling and hoping for no collision.
* Assert the invariants you relied on just before returning. A generator that raises is far better than one that quietly returns something invalid.

When the statement describes a state machine, initialize the complete initial state before the first operation, and apply every transition the statement describes in BOTH `reference()` and `validate()`. An operation the statement calls invalid is still a valid test input: `reference()` must return the result the statement specifies for an invalid operation, while `validate()` accepts the input as long as its argument shapes and stated limits are valid.

Before answering, hand-trace one sequence that covers the initial state, each kind of operation once, and one invalid operation, and check the trace against both functions.

## Python pitfalls that have broken this exact task

* Never assign to a name you also read inside the same function, and never to an imported module name. Both `random = random.Random(seed)` and `items = [x for x in items if ...]` raise `UnboundLocalError`. Bind the generator to a fresh name such as `rng`.
* Never draw from a possibly-empty range. Guard every `randrange`, `randint`, `choice` and `sample` so the range or sequence is non-empty, and skip that step when it is not.
* Return exactly the shape described above and nothing else.

Respond with exactly one block:

===ORACLE===
python source defining reference, gen, and validate
===END===

Problem statement:

{{statement}}
