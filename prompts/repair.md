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

First hand-trace the statement on this input. Then respond with:

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
