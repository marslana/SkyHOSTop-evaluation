#!/usr/bin/env python3
"""Generate publication-quality evaluation figures from mc_eval_results.csv.

Figures (numbered to match LaTeX):
  1. Feasibility vs instance limit  (fig1_feasibility_vs_instances.pdf)
  2. MILP vs XRON cost comparison   (fig2_milp_vs_xron.pdf)

All figures use COST mode only.
"""
import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

# ── Baseline renaming and ordering ──────────────────────────────────
RENAME = {
    "MILP": "SkyHOSTop (MILP)",
    "Heuristic": "Single-path overlay",
    "Single-path": "Single-path overlay",
    "Direct": "Direct transfer",
    "XRON": "XRON heuristic",
}
BL_ORDER = ["SkyHOSTop (MILP)", "XRON heuristic", "Single-path overlay", "Direct transfer"]

# Colorblind-safe palette (Wong / Tol bright)
COLORS = {
    "SkyHOSTop (MILP)":      "#0173B2",
    "XRON heuristic":      "#DE8F05",
    "Single-path overlay":  "#029E73",
    "Direct transfer":      "#D55E00",
}
EDGE_COLORS = {
    "SkyHOSTop (MILP)":      "#014F7C",
    "XRON heuristic":      "#9C6504",
    "Single-path overlay":  "#017A4E",
    "Direct transfer":      "#943F00",
}
HATCHES = {
    "SkyHOSTop (MILP)":      "",
    "XRON heuristic":      "",
    "Single-path overlay":  "",
    "Direct transfer":      "",
}

# ── Global matplotlib styling ───────────────────────────────────────
plt.rcParams.update({
    "font.family":        "serif",
    "font.serif":         ["Times New Roman", "Times", "DejaVu Serif"],
    "mathtext.fontset":   "dejavuserif",
    "font.size":          9,
    "axes.labelsize":     10,
    "axes.titlesize":     10,
    "xtick.labelsize":    8,
    "ytick.labelsize":    8,
    "legend.fontsize":    7.5,
    "legend.framealpha":  0.9,
    "legend.edgecolor":   "#cccccc",
    "figure.dpi":         600,
    "savefig.dpi":        600,
    "savefig.bbox":       "tight",
    "savefig.pad_inches": 0.02,
    "axes.linewidth":     0.6,
    "xtick.major.width":  0.5,
    "ytick.major.width":  0.5,
    "xtick.major.size":   3,
    "ytick.major.size":   3,
    "axes.grid":          True,
    "grid.alpha":         0.25,
    "grid.linewidth":     0.4,
    "grid.linestyle":     "--",
    "axes.axisbelow":     True,
    "axes.spines.top":    False,
    "axes.spines.right":  False,
})


def load(csv_path):
    df = pd.read_csv(csv_path)
    df["baseline"] = df["baseline"].map(lambda b: RENAME.get(b, b))
    df["feas"] = df["feasible"].astype(str).str.lower().isin(["true", "1", "yes"])
    return df[df["mode"] == "COST"].copy()


def _bar_kwargs(bl):
    return dict(
        color=COLORS[bl],
        edgecolor=EDGE_COLORS[bl],
        hatch=HATCHES[bl],
        linewidth=0.5,
        zorder=3,
    )


# ────────────────────────────────────────────────────────────────────
# Fig 1: Feasibility vs Instance Limit (grouped bars, all baselines)
# ────────────────────────────────────────────────────────────────────
def plot_feasibility_vs_instances(df, out):
    inst_limits = sorted(df["instance_limit"].unique())
    n_bl = len(BL_ORDER)
    bar_w = 0.18
    x = np.arange(len(inst_limits))

    fig, ax = plt.subplots(figsize=(3.4, 2.8))

    for j, bl in enumerate(BL_ORDER):
        rates = []
        for inst in inst_limits:
            s = df[(df["instance_limit"] == inst) & (df["baseline"] == bl)]
            rates.append(s["feas"].mean() * 100 if len(s) > 0 else 0)
        offset = (j - (n_bl - 1) / 2) * bar_w
        ax.bar(x + offset, rates, bar_w, label=bl, **_bar_kwargs(bl))

    ax.set_xlabel("Instance limit $N_{\\mathrm{max}}$")
    ax.set_ylabel("Feasibility (%)")
    ax.set_ylim(0, 100)
    ax.yaxis.set_major_locator(mticker.MultipleLocator(20))
    ax.set_xticks(x)
    ax.set_xticklabels([str(i) for i in inst_limits])
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=2,
              columnspacing=0.8, handletextpad=0.4, borderpad=0.4,
              handlelength=1.5, fontsize=7, frameon=False)

    fig.tight_layout()
    fig.subplots_adjust(bottom=0.28)
    fig.savefig(str(out) + ".pdf")
    fig.savefig(str(out) + ".png", dpi=300)
    plt.close(fig)
    print(f"  -> {out}.pdf/.png")


# ────────────────────────────────────────────────────────────────────
# Fig 2: MILP vs XRON cost comparison (3 panels by N_max)
# ────────────────────────────────────────────────────────────────────
def plot_milp_vs_xron(df, out):
    milp_lbl = "SkyHOSTop (MILP)"
    xron_lbl = "XRON heuristic"
    scen_cols = ["src", "dst", "instance_limit", "lambda_gbps", "latency_budget_ms"]

    milp = df[df["baseline"] == milp_lbl].set_index(scen_cols)[
        ["cost_total_hr", "feas"]].rename(
        columns={"cost_total_hr": "cost_milp", "feas": "feas_milp"})
    xron = df[df["baseline"] == xron_lbl].set_index(scen_cols)[
        ["cost_total_hr", "feas"]].rename(
        columns={"cost_total_hr": "cost_xron", "feas": "feas_xron"})

    both = milp.join(xron, how="inner")
    both = both[both["feas_milp"] & both["feas_xron"]].reset_index()

    inst_limits = sorted(both["instance_limit"].unique())
    n_panels = len(inst_limits)
    lambdas = sorted(both["lambda_gbps"].unique())
    x = np.arange(len(lambdas))
    bar_w = 0.30

    fig, axes = plt.subplots(n_panels, 1, figsize=(3.45, 1.7 * n_panels), sharey=False)
    if n_panels == 1:
        axes = [axes]

    global_max = 0

    for ax, inst in zip(axes, inst_limits):
        sub = both[both["instance_limit"] == inst]

        milp_costs, xron_costs = [], []
        for lam in lambdas:
            s = sub[sub["lambda_gbps"] == lam]
            milp_costs.append(s["cost_milp"].mean() if len(s) > 0 else 0)
            xron_costs.append(s["cost_xron"].mean() if len(s) > 0 else 0)

        bars_m = ax.bar(x - bar_w / 2, milp_costs, bar_w, label=milp_lbl,
                        **_bar_kwargs(milp_lbl))
        bars_x = ax.bar(x + bar_w / 2, xron_costs, bar_w, label=xron_lbl,
                        **_bar_kwargs(xron_lbl))

        for bar_m, mc, xc in zip(bars_m, milp_costs, xron_costs):
            if mc > 0 and xc > 0:
                saving = (xc - mc) / xc * 100
                ax.text(bar_m.get_x() + bar_m.get_width() / 2,
                        bar_m.get_height(),
                        f"{saving:.0f}%",
                        ha="center", va="bottom",
                        fontsize=5, fontweight="bold", color="#006400",
                        zorder=5)

        local_max = max(xron_costs) if any(c > 0 for c in xron_costs) else 0
        global_max = max(global_max, local_max)

        ax.set_title(f"$N_{{\\mathrm{{max}}}}$ = {inst}", fontsize=9)
        ax.set_xticks(x)
        ax.set_xticklabels([f"{l:.0f}" for l in lambdas])
        ax.set_ylabel("Avg cost (\\$/hr)")

    for ax in axes:
        ax.set_ylim(0, global_max * 1.25)
    axes[-1].set_xlabel("Stream rate $\\lambda$ (Gbps)")
    axes[0].legend(loc="upper left", fontsize=7, borderpad=0.3,
                   handletextpad=0.3, handlelength=1.2)

    fig.tight_layout(h_pad=0.8)
    fig.savefig(str(out) + ".pdf")
    fig.savefig(str(out) + ".png", dpi=300)
    plt.close(fig)
    print(f"  -> {out}.pdf/.png")


# ────────────────────────────────────────────────────────────────────
# Verification: print all data values that feed each figure
# ────────────────────────────────────────────────────────────────────
def verify_data(df):
    print("\n" + "=" * 70)
    print("DATA VERIFICATION")
    print("=" * 70)

    # Fig 1 verification
    print("\n--- Fig 1: Feasibility (%) vs N_max ---")
    for inst in sorted(df["instance_limit"].unique()):
        sub = df[df["instance_limit"] == inst]
        vals = []
        for bl in BL_ORDER:
            s = sub[sub["baseline"] == bl]
            rate = s["feas"].mean() * 100 if len(s) > 0 else 0
            vals.append(f"{bl}={rate:.1f}%")
        print(f"  N_max={inst}: {'  '.join(vals)}")

    # Fig 2 verification
    milp_lbl, xron_lbl = "SkyHOSTop (MILP)", "XRON heuristic"
    scen = ["src", "dst", "instance_limit", "lambda_gbps", "latency_budget_ms"]
    m = df[df["baseline"] == milp_lbl].set_index(scen)[["cost_total_hr", "feas"]].rename(
        columns={"cost_total_hr": "cm", "feas": "fm"})
    x = df[df["baseline"] == xron_lbl].set_index(scen)[["cost_total_hr", "feas"]].rename(
        columns={"cost_total_hr": "cx", "feas": "fx"})
    both = m.join(x, how="inner")
    both = both[both["fm"] & both["fx"]].reset_index()

    print(f"\n--- Fig 2: MILP vs XRON (both-feasible: {len(both)} scenarios) ---")
    for inst in sorted(both["instance_limit"].unique()):
        sub = both[both["instance_limit"] == inst]
        print(f"  N_max={inst} ({len(sub)} scenarios):")
        for lam in sorted(sub["lambda_gbps"].unique()):
            s = sub[sub["lambda_gbps"] == lam]
            mc_v = s["cm"].mean()
            xc_v = s["cx"].mean()
            saving = (xc_v - mc_v) / xc_v * 100 if xc_v > 0 else 0
            print(f"    λ={lam:.0f}: MILP=${mc_v:.2f}  XRON=${xc_v:.2f}  saving={saving:.1f}%")
        inst_saving = (sub["cx"].mean() - sub["cm"].mean()) / sub["cx"].mean() * 100
        print(f"    Overall N_max={inst} saving: {inst_saving:.1f}%")

    avg_saving = (both["cx"].mean() - both["cm"].mean()) / both["cx"].mean() * 100
    print(f"  Overall average saving: {avg_saving:.1f}%")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="results/mc_eval_results.csv")
    parser.add_argument("--outdir", default="figures")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    df = load(args.csv)
    print(f"Loaded {len(df)} COST-mode rows from {args.csv}")

    for bl in BL_ORDER:
        sub = df[df["baseline"] == bl]
        nf = int(sub["feas"].sum())
        print(f"  {bl}: {nf}/{len(sub)} feasible ({100 * nf / len(sub):.1f}%)")

    verify_data(df)

    print("\nFig 1: Feasibility vs Instance Limit")
    plot_feasibility_vs_instances(df, outdir / "fig1_feasibility_vs_instances")

    print("\nFig 2: MILP vs XRON Cost Comparison")
    plot_milp_vs_xron(df, outdir / "fig2_milp_vs_xron")

    print("\nDone. All figures saved to:", outdir)


if __name__ == "__main__":
    main()
