"""`python -m bench.cli` — run the CityWalk-style experiment end to end.

Four subcommands, each usable on its own:

    discover  list the focal methods that would be benchmarked, and why
    gen       run the agent once per focal method, writing test files
    eval      compile/run/measure whatever `gen` produced
    run       gen followed by eval

`gen` and `eval` are separate because generation is the expensive half: a
scoring change only requires re-running `eval` over the test files already on
disk.
"""

import argparse
import itertools
import json
import random
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from bench import harness, report, runner
from bench.discover import FocalMethod, discover
from bench.schema import Row, append_jsonl, read_jsonl, write_csv

MANIFEST_NAME = "manifest.json"
ROWS_NAME = "rows.jsonl"
CSV_NAME = "results.csv"
GENERATED_DIRNAME = "generated"


def _select(
    methods: list[FocalMethod],
    *,
    file_pattern: str,
    method_pattern: str,
    min_cc: int,
    max_per_file: int,
    limit: int,
    sample: int,
    seed: int,
) -> list[FocalMethod]:
    """Narrow the focal set. Filters compose; sampling happens last."""
    selected = methods
    if file_pattern:
        regex = re.compile(file_pattern)
        selected = [m for m in selected if regex.search(m.file)]
    if method_pattern:
        regex = re.compile(method_pattern)
        selected = [m for m in selected if regex.search(m.name)]
    if min_cc > 1:
        selected = [m for m in selected if m.complexity >= min_cc]
    if max_per_file > 0:
        per_file: dict[str, int] = {}
        capped = []
        for method in selected:
            count = per_file.get(method.file, 0)
            if count >= max_per_file:
                continue
            per_file[method.file] = count + 1
            capped.append(method)
        selected = capped
    if sample > 0 and sample < len(selected):
        selected = sorted(
            random.Random(seed).sample(selected, sample),
            key=lambda m: (m.file, m.start_line),
        )
    if limit > 0:
        selected = selected[:limit]
    return selected


def _add_selection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--filter-file", default="", help="Regex on the focal file path.")
    parser.add_argument("--filter-method", default="", help="Regex on the focal method name.")
    parser.add_argument("--min-cc", type=int, default=1,
                        help="Skip methods below this cyclomatic complexity.")
    parser.add_argument("--max-per-file", type=int, default=0,
                        help="Cap methods taken from any one file (0 = no cap).")
    parser.add_argument("--limit", type=int, default=0, help="Keep only the first N methods.")
    parser.add_argument("--sample", type=int, default=0,
                        help="Randomly sample N methods (applied before --limit).")
    parser.add_argument("--seed", type=int, default=42, help="Seed for --sample.")
    parser.add_argument("--citywalk-focal", action="store_true",
                        help="Restrict the focal set the way CITYWALK does: drop "
                             "private/protected members and the files its evaluator "
                             "skips by name. Free functions are kept.")
    parser.add_argument("--paper-sample", action="store_true",
                        help="Draw as many methods as Table 3 of the paper reports "
                             "for this project. The paper does not publish which "
                             "methods it chose, so the draw is seeded instead.")


def _selection_from(args) -> list[FocalMethod]:
    project = Path(args.project)
    methods, strategy = discover(project, min_lines=args.min_lines)
    print(f"discovery: {len(methods)} methods via {strategy}", file=sys.stderr)

    if getattr(args, "citywalk_focal", False):
        from bench.discover import citywalk_excluded_file, class_access_index, is_citywalk_focal

        access = class_access_index(project)
        before = len(methods)
        methods = [
            m for m in methods
            if not citywalk_excluded_file(project.resolve().name, m.file)
            and is_citywalk_focal(m.name, access)
        ]
        print(f"citywalk focal set: {len(methods)}/{before}", file=sys.stderr)

    sample = args.sample
    if getattr(args, "paper_sample", False):
        from bench import projects as project_config

        count = project_config.paper_focal_count(project.resolve().name)
        if count is None:
            print(f"warning: no paper count for {project.name}; sampling disabled",
                  file=sys.stderr)
        else:
            sample = count
            print(f"paper sample: drawing {count} methods (Table 3)", file=sys.stderr)

    selected = _select(
        methods,
        file_pattern=args.filter_file,
        method_pattern=args.filter_method,
        min_cc=args.min_cc,
        max_per_file=args.max_per_file,
        limit=args.limit,
        sample=sample,
        seed=args.seed,
    )
    print(f"selected:  {len(selected)} methods", file=sys.stderr)
    return selected


