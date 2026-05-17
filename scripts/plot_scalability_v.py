#!/usr/bin/env python3
"""Single-panel scalability figure: median MILP solve time vs |V|, with a
p95 line and a shaded band showing the min-max range across probes."""
import argparse
from pathlib import Path
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="results/scalability_v18_60_p50.csv")
    ap.add_argument("--output", default="fig_scalability.pdf")
    ap.add_argument("--max-v", type=int, default=None,
                    help="Restrict plot to |V| <= max-v (CSV is unchanged)")
    args = ap.parse_args()

    df = pd.read_csv(args.input)
    if args.max_v is not None:
        df = df[df["V"] <= args.max_v]
    agg = (df.groupby("V")
             .agg(med=("solve_time_s", "median"),
                  p95=("solve_time_s", lambda s: s.quantile(0.95)),
                  mn=("solve_time_s", "min"),
                  mx=("solve_time_s", "max"),
                  paths=("num_candidate_paths", "median"),
                  n=("solve_time_s", "size"))
             .reset_index()
             .sort_values("V"))

    fig, ax = plt.subplots(figsize=(3.4, 2.4))

    ax.fill_between(agg.V, agg.mn, agg.mx, color="#a6171a", alpha=0.15,
                    label="min--max range")
    ax.plot(agg.V, agg.p95, color="#a6171a", marker="^", linewidth=1.2,
            markersize=4.5, linestyle="--", label="p95")
    ax.plot(agg.V, agg.med, color="#a6171a", marker="o", linewidth=1.8,
            markersize=5.5, label="median")

    ax.set_xlabel(r"Topology size $|V|$ (regions)")
    ax.set_ylabel("MILP solve time (s)")
    ax.grid(True, which="both", alpha=0.3)
    ax.set_xticks(list(agg.V))
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper left", fontsize=7, frameon=False)

    plt.tight_layout()
    out = Path(args.output)
    plt.savefig(out, bbox_inches="tight")
    plt.savefig(out.with_suffix(".png"), bbox_inches="tight", dpi=200)
    print(f"Wrote {out}")
    print()
    print("Per-V summary (n probes per size):")
    for _, r in agg.iterrows():
        print(f"  V={int(r.V):3d}  n={int(r.n):3d}  paths={int(r.paths):4d}  "
              f"min={r.mn:5.2f}s  med={r.med:5.2f}s  p95={r.p95:5.2f}s  max={r.mx:5.2f}s")


if __name__ == "__main__":
    main()
