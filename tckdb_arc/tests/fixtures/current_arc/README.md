This fixture copies `arc/testing/parser_evidence/golden/output.yml` and
`parser_evidence.json` from the local ARC checkout at commit
`db0934d5973e7dda0facd0c99f4080b4243166ae`.

ARC's golden output is the evidence builder's input; the test adds the
generation descriptor that ARC's output writer normally adds afterward.
No calculation logs are needed. The regression checks the ORCA Hessian's
own coordinate frame, both IRC branches and their numerical quantities,
and GSM energies attached by geometry matching while unmatched archived
invocations remain unattached.
