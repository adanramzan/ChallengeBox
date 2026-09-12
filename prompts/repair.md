A solution failed a concrete check. Make the smallest change that fixes the demonstrated failure. Keep the public contract. Do not switch algorithms unless the failure proves the algorithm wrong. {{language_rules}}

Failure kind: {{kind}}
Input:
{{input}}
Expected (from an independent literal reference, which may itself be wrong):
{{expected}}
Actual:
{{actual}}
Details:
{{details}}
{{previous_attempt}}
First, in one sentence, name the specific expression or line that produces the wrong value on this
input. Then change only what that sentence names. A rewrite of the whole approach is almost never the
right response to a single failing input; if you believe the approach itself is fundamentally wrong,
say so plainly before ===VERDICT=== instead of silently rewriting it.

Then hand-trace the statement on this input. Respond with:

===VERDICT===
candidate   (if the solution is wrong)  or  oracle   (if the reference is wrong and the solution is right)
===END===
===CODE===
the full corrected solution (repeat the current one unchanged if VERDICT is oracle)
===END===

Statement:

{{statement}}

Current solution:

{{code}}
