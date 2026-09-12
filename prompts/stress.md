You write a generator for one MAXIMUM-SIZE input and a list of small hand-picked edge-case inputs for testing. Python 3.11 standard library only, `random.Random(seed)` only.

Define `gen_max(seed: int)` returning {{gen_returns}} at every stated maximum simultaneously (max counts, max values, max nesting, deepest paths, worst-case operation mix). It must run in under 10 seconds itself.
Define `EDGES = [...]`, 5 to 10 literal inputs of the same shape: empty/minimum, single element, duplicates, boundary indices, overflow-adjacent values, and one case that distinguishes the literal statement from a plausible misreading.

Respond with exactly one block:

===STRESS===
python source defining gen_max and EDGES
===END===

Problem statement:

{{statement}}
