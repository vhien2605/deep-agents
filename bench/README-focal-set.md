# The focal-method set

`focal-set.json` (and the same rows as `focal-set.csv`) fixes the population
every experiment is measured over. Two runs are comparable only if they cover
the same methods, and a fresh discovery pass does not guarantee that: a source
edit, a parser change, or a different `min_lines` threshold silently moves the
boundary.

A method is identified by **project, file, name, start line**. The end line,
cyclomatic complexity and branch count are stored alongside so a later run can
check it is looking at the same code rather than a shifted copy of it.

Each project pins the upstream commit the sources were taken from, so the line
numbers mean something:

| project | methods | upstream commit |
|---|---:|---|
| hjson-cpp | 25 | `0c40199c` |
| tinyxml2 | 158 | `1dee28e5` |
| yaml-cpp | 204 | `b591d8ae` |
| re2 | 144 | `bc423653` |
| ninja | 238 | `b51a1e37` |
| leveldb | 220 | `7ee830d0` |
| json.cpp | 34 | `ccb9195e` |
| glomap | 32 | `99806d08` |
| papy | 25 | `15ada226` |
| mlx | 206 | `dcf4b2a4` |
| **total** | **1286** | |

`checksum_sha256` covers the identity tuples in file order. Compare it before
and after any change to the set; if it moves, the numbers from before and after
are not comparable and should not appear in the same table.

## The 1288 in the September 2026 sweep

That sweep ran 1288 manifest entries. Two re2 methods --
`CharClassBuilder::Negate` at regexp.cc:884 and `RE2::Set::Add` at set.cc:55 --
appear twice in the manifest and were scored twice. This file lists each method
once, so it holds 1286, and `duplicates_dropped` names the two.

The effect on re2's published figures is below the run-to-run variance of the
repair round: CSR unchanged at 100.00, EPR 88.75 against 88.68, line coverage
31.84 against 32.02, branch coverage 28.34 against 28.50. The sweep's numbers
are reported as measured; a future sweep driven from this file will differ by
about that much on re2 for this reason alone.
