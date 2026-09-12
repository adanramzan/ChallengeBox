You write a SLOW, LITERAL reference implementation and a small random input generator for testing. You never see any other solution. Follow the statement sentence by sentence; loop where it loops; build what it describes. Inputs will be tiny. Efficiency is irrelevant. Python 3.11, standard library only.

{{oracle_signature}}

Also define `gen(seed: int, mode: str)` using `random.Random(seed)` only. `mode == "small"`: sizes at most 6, values at most 20, every validity constraint of the statement honored, include boundary shapes (empty where allowed, single element, equal values, adjacent positions). `mode == "medium"`: sizes at most 12 but numeric counts/repetitions/demands around 10^4 to 10^5 so that closed-form arithmetic in a fast solution is exercised while your literal loops still finish in a few seconds. {{gen_returns}}

Respond with exactly one block:

===ORACLE===
python source defining reference and gen
===END===

Problem statement:

{{statement}}
