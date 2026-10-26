# CityWalk-style benchmark harness

Measures the agent the way the CITYWALK paper measures C++ unit-test
generators: one agent run per focal method, then compile / execute / cover the
result and report per-method numbers that line up column-for-column with the
paper's tables.

Everything runs inside a container so the toolchain — g++ 13.3, gcovr 8.3,
GoogleTest, gmock — is identical on every machine. Coverage numbers that depend
on the host compiler are not comparable across runs, so the container is not
optional.

## Quick start

```bash
# 1. Build the image (once, or after dependencies change)
docker build -t deepagents-bench -f bench/Dockerfile .

# 2. See what would be benchmarked — no LLM, no build
bench/run-docker.sh discover --project data/re2-<sha> --min-cc 5 --limit 20

# 3. Generate tests, then score them
bench/run-docker.sh gen  --project data/re2-<sha> --out runs/re2 --sample 30
bench/run-docker.sh eval --out runs/re2

# Or both at once
bench/run-docker.sh run --project data/re2-<sha> --out runs/re2 --sample 30
```

On Windows, `bench\run-docker.ps1` takes the same arguments.

## Why gen and eval are separate

Generation is the expensive half — roughly 60k tokens and 40 s per method. A
change to how a metric is computed only requires re-running `eval` over the
test files already on disk; the sweep never has to be regenerated to fix a
scoring bug. `gen` checkpoints its manifest after every method, so an
interrupted sweep keeps everything it had finished.

## Output

`--out <dir>` receives:

| Path | Contents |
| --- | --- |
| `results.csv` | the result table, one row per focal method |
| `rows.jsonl` | the same rows plus a `detail` object (compile attempts, per-case verdicts, raw coverage counts) |
| `manifest.json` | what was generated, and what it cost — the input to `eval` |
| `generated/` | the test files the agent wrote |
| `work/` | instrumented build and compiled test binaries |

### Columns

| Column | Meaning |
| --- | --- |
| `project`, `file`, `method`, `line` | the focal method's identity |
| `model` | the model id the endpoint actually served (a proxy may route elsewhere than `LLM_MODEL` asked for) |
| `n_cases` | GoogleTest cases the agent wrote |
| `n_cases_compiled` | cases that survived compilation |
| `n_cases_passed` | cases that also passed at run time |
| `csr` | compile success rate = `n_cases_compiled / n_cases` |
| `epr` | execution pass rate = `n_cases_passed / n_cases_compiled` |
| `line_coverage`, `branch_coverage` | gcov, restricted to the focal method's line range |
| `touched_focal` | whether any test actually executed the method |
| `total_tokens`, `duration_s` | cost of the generation run |
| `fix_phase` | build/fix rounds the agent went through after its first build |
| `error_category`, `error_first_line` | first failure, classified (see `bench/metrics.py`) |
| `focal_cc` | cyclomatic complexity of the focal method |
| `test_loc`, `test_cc`, `uses_mock` | size, complexity, and mock usage of the generated test file |

## How the per-case rates are obtained

A C++ compiler rejects the whole translation unit, not one case, so a naive
whole-file compile can only produce 0 % or 100 %. The harness instead maps each
diagnostic back to the `TEST(...)` block that contains it, removes only those
cases, and recompiles — up to three rounds. Cases that had to be removed did not
compile; the rest did. Execution is then per-case via
`--gtest_output=json`.

Coverage is attributed the same way: the project is built once with
`--coverage`, and the accumulated `.gcda` counters are deleted before each test
binary runs, so one method's coverage never leaks into the next row.

## Which code counts as "production"

Path heuristics get this wrong on real projects — re2 keeps its own tests in
`re2/testing/`, a helper library in `util/`, and stale object files in
`build_eval/`. So `bench/discover.py` reads the build files instead: the
sources CMake compiles into a non-test library are the focal set. For re2 that
is exactly the 22 files of `RE2_SOURCES`. Projects with no parseable CMake fall
back to path/name heuristics, and `discover` reports which strategy it used.

## Selecting a subset

469 focal methods at ~60k tokens each is a large sweep. All selection flags
compose, and `discover` previews the result without spending anything:

```
--filter-file <regex>    restrict to matching source paths
--filter-method <regex>  restrict to matching method names
--min-cc <n>             skip trivial methods below this complexity
--max-per-file <n>       cap how many methods come from any one file
--sample <n> --seed <n>  reproducible random subset
--limit <n>              take the first n after every other filter
```

## Configuration

The LLM endpoint comes from `.env` at the repo root, which is mounted into the
container and parsed by python-dotenv. Do **not** pass it with docker's
`--env-file`: docker treats the quotes in `LLM_API_KEY="sk-..."` as part of the
value, and `load_dotenv()` will not override an already-set variable, so every
request would fail authentication.

## Requirements

Docker with a running daemon. Nothing else — no host compiler, no host Python.
