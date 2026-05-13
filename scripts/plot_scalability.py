#!/usr/bin/env python3
"""Generate Fig: solve time vs topology size, hops=1 vs hops=2."""
import argparse
from pathlib import Path
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="results/scalability.csv")
    ap.add_argument("--output", default="fig3_scalability.pdf")
    args = ap.parse_args()

    df = pd.read_csv(args.input)
    agg = (df.groupby(["V", "max_hops"])
             .agg(solve_med=("solve_time_s", "median"),
                  solve_p95=("solve_time_s", lambda s: s.quantile(0.95)),
                  paths_med=("num_candidate_paths", "median"))
             .reset_index())

    fig, axes = plt.subplots(1, 2, figsize=(8.2, 3.1))

    ax = axes[0]
    for h, color, marker, label in [
        (1, "#1f77b4", "o", r"$H_{\max}=1$"),
        (2, "#d62728", "s", r"$H_{\max}=2$"),
    ]:
        sub = agg[agg.max_hops == h].sort_values("V")
        ax.plot(sub.V, sub.solve_med * 1000, color=color, marker=marker, linewidth=1.8,
                markersize=6, label=label)
        ax.fill_between(sub.V, sub.solve_med * 1000, sub.solve_p95 * 1000,
                        color=color, alpha=0.15)
    ax.set_xlabel(r"Topology size $|V|$ (regions)")
    ax.set_ylabel("MILP solve time (ms)")
    ax.set_yscale("log")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(loc="upper left", fontsize=9, frameon=False)
    ax.set_title("(a) Solve time vs. topology size", fontsize=10)

    ax = axes[1]
    for h, color, marker, label in [
        (1, "#1f77b4", "o", r"$H_{\max}=1$"),
        (2, "#d62728", "s", r"$H_{\max}=2$"),
    ]:
        sub = agg[agg.max_hops == h].sort_values("V")
        ax.plot(sub.V, sub.paths_med, color=color, marker=marker, linewidth=1.8,
                markersize=6, label=label)
    ax.set_xlabel(r"Topology size $|V|$ (regions)")
    ax.set_ylabel("# candidate paths")
    ax.set_yscale("log")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(loc="upper left", fontsize=9, frameon=False)
    ax.set_title("(b) Candidate-path count vs. topology size", fontsize=10)

    plt.tight_layout()
    out = Path(args.output)
    plt.savefig(out, bbox_inches="tight")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
