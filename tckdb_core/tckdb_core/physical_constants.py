"""Physical constants shared by the numerics in this package.

``E_h_kJmol`` is the one definition of the hartree-to-kJ/mol factor every producer
adapter must use: a hand-typed ``2625.4996...`` elsewhere drifts from it (the
difference between CODATA vintages is ~1e-7 relative, enough to change a reported
barrier in the sixth digit).

Ported from ARC's ``arc/constants.py`` (the producer this core was extracted from);
the values are kept identical so every number an ARC upload already produced is
reproduced bit for bit.
"""

#: The Hartree energy E_h in J.
E_h = 4.35974434e-18
#: The Avogadro constant N_A in mol^-1.
Na = 6.02214179e23
#: The Hartree energy in kJ/mol (1 Hartree = 2625.5 kJ/mol).
E_h_kJmol = E_h * Na / 1000
