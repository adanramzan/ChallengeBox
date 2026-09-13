You write a generator for one MAXIMUM-SIZE input, plus a list of small hand-picked edge cases, for testing a solution you will never see. Python 3.11, standard library only, randomness only from `random.Random(seed)`.

{{io_rules}}

Define `gen_max(seed: int)` returning {{gen_returns}}. Its purpose is to separate a solution of the intended complexity from a slower one, so it needs scale and the operation mix that is worst for a plausible implementation — not literally the largest legal input.

**`gen_max` must finish in about five seconds.** This is a hard requirement and it outranks size: a generator that never returns contributes nothing, and the whole timing check is then skipped. Measure, don't guess: build the input incrementally, checking elapsed time as you go, and stop growing it once you approach the budget — return what you have. A smaller input that actually comes back beats a larger one computed from a guessed fixed size that never returns. Two rules follow.

* Push every *value* to its stated maximum immediately — maximum integers, deepest nesting, longest individual paths — because large values cost nothing to produce and are where overflow and precision bugs live.
* Size the *counts* to whatever you can build in that budget, checked against a clock, not assumed from the statement's stated limit. Prefer bulk construction (comprehensions and slicing over whole ranges) to a per-element loop. If honoring the statement's preconditions requires stepping through a state model one operation at a time, that loop is your real constraint: pick a count it can finish, in the tens of thousands rather than the stated maximum. Tens of thousands already separates a linear solution from a quadratic one.
* If the statement bounds a quantity that only grows through operations — a length, a depth, a count of live items — the worst case is where that quantity actually reaches its bound. Bias the operation mix so it does; a long sequence of operations that keeps the structure small tests nothing.
* Never build a nested, linked, or tree-shaped input with recursion: its depth can be proportional to the input's size, and Python's recursion limit is small. Build it iteratively instead — an explicit stack, or linking nodes in a loop.

Allocate memory proportional to the input you return, never to a quantity the statement merely describes, such as a repetition count or capacity that can reach 10^18.

Define `EDGES = [...]`, 5 to 10 literal inputs of the same shape: the minimum or empty case where one is allowed, a single element, repeats and ties, first and last positions, values sitting exactly at the stated numeric limits, and at least one case that separates the statement's literal wording from a plausible misreading of it.

Every entry in `EDGES` must satisfy the statement's preconditions exactly as `gen_max`'s output does. A hand-typed literal is the single easiest place to break them, and an invalid edge case is worse than no edge case, because the comparison it produces is meaningless and it burns a repair attempt chasing a bug that is not there. Walk each entry against the preconditions before you emit it. In particular, never name an identifier, key or position the input itself never created, and never repeat a value in a collection the statement calls distinct.

## Generate only inputs the statement calls valid

An invalid input makes every later comparison meaningless, and it is the most common way this task is failed. Before writing either generator, list the preconditions the statement states — value ranges, distinctness, "must currently exist", "never appeared before", ordering, and any limit expressed against the current size or state — and honor every one.

* Build operation sequences from a running model of the state, never by sampling blindly. At each step choose only from the operations legal in the state at that moment, apply the chosen one to your model, then choose the next.
* Get distinctness by construction: draw from a pool without replacement, rather than sampling and hoping for no collision.
* Assert the invariants you relied on just before returning. Raising beats quietly returning something invalid.

## Python pitfalls that have broken this exact task

* Never assign to a name you also read inside the same function, and never to an imported module name. `random = random.Random(seed)` raises `UnboundLocalError`. Bind the generator to a fresh name such as `rng`.
* Never draw from a possibly-empty range. Guard **every** `randrange`, `randint`, `choice` and `sample` call site — not just the obvious first one — so the range or sequence is non-empty at that exact point, and skip that step when it is not. An `IndexError` or `ValueError` from one unguarded draw buried deep in a loop is just as fatal as one in the first line.
* `EDGES` must hold literal values, not calls that build them.
* Never indent the contents of a multi-line string literal (e.g. a `"""..."""` block spanning several lines) to match the surrounding code. Every line after the first becomes part of the value verbatim, so indenting it adds leading whitespace the real input never has. Start continuation lines at column 0, or build the string with `"\n".join([...])` instead.

Respond with exactly one block:

===STRESS===
python source defining gen_max and EDGES
===END===

Problem statement:

{{statement}}
