#!/usr/bin/env python3
"""Single-panel scalability figure: median MILP solve time vs |V|, with a
shaded band showing the min-max range across probes at each topology size."""
import argparse
from pathlib import Path
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="results/scalability_v18_50_p15.csv")
    ap.add_argument("--output", default="fig_scalability.pdf")
    args = ap.parse_args()

    df = pd.read_csv(args.input)
    agg = (df.groupby("V")
             .agg(med=("solve_time_s", "median"),
                  mn=("solve_time_s", "min"),
                  mx=("solve_time_s", "max"),
                  paths=("num_candidate_paths", "median"))
             .reset_index()
             .sort_values("V"))

    fig, ax = plt.subplots(figsize=(3.4, 2.4))

    ax.fill_between(agg.V, agg.mn, agg.mx, color="#a6171a", alpha=0.18,
                    label="min--max range")
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
    print("Per-V summary:")
    for _, r in agg.iterrows():
        print(f"  V={int(r.V):3d}  paths(med)={int(r.paths):4d}  "
              f"min={r.mn*1000:6.0f}ms  med={r.med*1000:6.0f}ms  max={r.mx*1000:6.0f}ms")


if __name__ == "__main__":
    main()
