#!/usr/bin/env python3
"""Run the whole pipeline end to end.

    python pipeline.py                 # all steps
    python pipeline.py --from load     # start at a later step (earlier outputs must exist)
    python pipeline.py --only profile  # run a single step

Steps:
  1. profile    profiler.py               data/raw/*.csv   -> output/profiling_report.json
  2. clean      notebooks/data_cleaning.ipynb  data/raw/*.csv   -> data/validated/*.csv
  3. load       notebooks/load_warehouse.ipynb data/validated   -> output/warehouse.db
  4. analytics  scripts/generate_analytics.py  warehouse.db     -> output/analytics.json

The notebooks are run as they are and are not modified; a copy of each executed
notebook, with its output, is saved to output/notebooks/ for review.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

STEPS = ["profile", "clean", "load", "analytics"]
PROFILE_KEYS = ["transactions=transaction_id", "stores=store_id", "products=product_id"]


def run_profile() -> None:
    import profiler

    args = ["--data-dir", str(ROOT / "data" / "raw"), "--out", str(ROOT / "output" / "profiling_report.json")]
    for key in PROFILE_KEYS:
        args += ["--key", key]
    if profiler.main(args) != 0:
        raise RuntimeError("profiling failed for at least one file (see the messages above)")


KERNEL_NAME = "retail-pipeline"


def use_this_python_for_notebooks() -> None:
    """Make the notebooks run with the same Python (and packages) as this script.

    Without this, Jupyter may pick a "python3" kernel from another install
    (e.g. Anaconda) and run the notebooks in that environment instead.
    """
    import json
    import os

    kernels_root = ROOT / "output" / ".jupyter"
    spec_dir = kernels_root / "kernels" / KERNEL_NAME
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "kernel.json").write_text(json.dumps({
        "argv": [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
        "display_name": "Retail pipeline (this Python)",
        "language": "python",
    }), encoding="utf-8")
    os.environ["JUPYTER_PATH"] = os.pathsep.join(filter(None, [str(kernels_root), os.environ.get("JUPYTER_PATH")]))


def run_notebook(name: str) -> None:
    import nbformat
    from nbclient import NotebookClient
    from nbclient.exceptions import CellExecutionError

    src = ROOT / "notebooks" / name
    executed = ROOT / "output" / "notebooks" / name
    executed.parent.mkdir(parents=True, exist_ok=True)

    use_this_python_for_notebooks()
    nb = nbformat.read(src, as_version=4)
    client = NotebookClient(nb, timeout=600, kernel_name=KERNEL_NAME,
                            resources={"metadata": {"path": str(src.parent)}})  # run from notebooks/
    try:
        client.execute()
    except CellExecutionError as exc:
        # The notebook's checks (STRICT = True) stop it here; show the reason, not a kernel traceback.
        text = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))  # drop terminal colour codes
        lines = [line for line in text.splitlines() if "CHECK FAILED" in line or "Error" in line]
        raise RuntimeError(f"{name} stopped: " + (lines[-1] if lines else str(exc)[-500:])) from None
    finally:
        nbformat.write(nb, executed)
        print(f"  executed copy: {executed.relative_to(ROOT)}")


def run_analytics() -> None:
    import generate_analytics

    if generate_analytics.main([]) != 0:
        raise RuntimeError("analytics failed (see the message above)")


RUNNERS = {
    "profile": run_profile,
    "clean": lambda: run_notebook("data_cleaning.ipynb"),
    "load": lambda: run_notebook("load_warehouse.ipynb"),
    "analytics": run_analytics,
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the retail data pipeline end to end.")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--from", dest="start", choices=STEPS, help="start at this step")
    group.add_argument("--only", choices=STEPS, help="run just this step")
    args = ap.parse_args(argv)

    steps = [args.only] if args.only else STEPS[STEPS.index(args.start or STEPS[0]):]
    (ROOT / "output").mkdir(exist_ok=True)

    total = time.perf_counter()
    for i, step in enumerate(steps, 1):
        print(f"\n[{i}/{len(steps)}] {step}")
        t0 = time.perf_counter()
        try:
            RUNNERS[step]()
        except Exception as exc:
            print(f"\nFAILED at step '{step}': {exc}", file=sys.stderr)
            return 1
        print(f"  done in {time.perf_counter() - t0:.1f}s")

    print(f"\nPipeline finished in {time.perf_counter() - total:.1f}s. Outputs are in output/ and data/validated/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
