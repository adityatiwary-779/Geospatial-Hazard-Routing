"""Command-line entry point.

Examples
  python run.py --demo
  python run.py --input data/input --output outputs/my_run
  python run.py --roads r.geojson --flood f.tif --landslide l.tif --slope s.tif ^
                --settlements s.geojson --hospitals h.geojson --shelters sh.geojson
"""
import argparse
import logging
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from hazard_dss.config import load_config  # noqa: E402
from hazard_dss.demo import generate_demo  # noqa: E402
from hazard_dss.io_utils import ALL_KEYS, InputError, discover_inputs  # noqa: E402
from hazard_dss.pipeline import run_pipeline  # noqa: E402

ROOT = Path(__file__).resolve().parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Multi-hazard geospatial decision-support system")
    ap.add_argument("--demo", action="store_true", help="generate synthetic demo data and run on it")
    ap.add_argument("--input", help="folder containing the input files (see README for naming)")
    ap.add_argument("--output", help="output folder (default: outputs/<name>)")
    ap.add_argument("--config", help="YAML file overriding the defaults (see config.example.yaml)")
    for k in ALL_KEYS:
        ap.add_argument(f"--{k}", help=f"path to the {k} file (overrides auto-discovery)")
    ap.add_argument("--risk-aversion", type=float, help="routing K (0 = shortest route)")
    ap.add_argument("--aggregation", choices=["wlc", "topsis"])
    ap.add_argument("--weights", choices=["ahp", "equal", "entropy", "manual"])
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    overrides = {}
    if args.risk_aversion is not None:
        overrides.setdefault("routing", {})["risk_aversion"] = args.risk_aversion
    if args.aggregation:
        overrides.setdefault("mcdm", {})["aggregation"] = args.aggregation
    if args.weights:
        overrides.setdefault("mcdm", {}).setdefault("weights", {})["method"] = args.weights
    cfg = load_config(args.config, overrides)

    try:
        if args.demo:
            folder = ROOT / "data" / "demo"
            generate_demo(str(folder))
            paths = discover_inputs(str(folder))
            name = "demo"
        else:
            paths = discover_inputs(args.input) if args.input else {}
            name = Path(args.input).name if args.input else "run"
        for k in ALL_KEYS:
            if getattr(args, k):
                paths[k] = getattr(args, k)
        if not paths:
            ap.error("give --demo, --input FOLDER, or individual file options")
        out = args.output or str(ROOT / "outputs" / name)
        print("Inputs used:")
        for k, v in paths.items():
            print(f"  {k:12s} {v}")
        result = run_pipeline(paths, out, cfg)
    except InputError as exc:
        print(f"\nInput problem: {exc}", file=sys.stderr)
        return 2

    s = result.summary
    print("\n=== Done in %.1f s ===" % s["runtime_seconds"])
    print("Weights:", s["weights"])
    print("High-risk zones: %d (%.2f km2); high-risk settlements: %d of %d" % (
        s["n_zones"], s["high_risk_area_km2"], s["settlements"]["high_risk"], s["settlements"]["total"]))
    for w in s["warnings"]:
        print("Note:", w)
    print("Outputs written to:", result.out_dir)
    print("Open this in a browser:", Path(result.out_dir) / "viewer_standalone.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
