#!/usr/bin/env python3
"""Sensitivity test: what happens if measured throughput drops uniformly
by X% across all links at runtime (e.g., a network-wide degradation
event)? We re-solve the MILP on a representative sample of scenarios
with throughput * (1 - drop) and report the change in cost and
feasibility relative to the nominal (un-degraded) plan."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from solver.solver import ThroughputProblem
from solver.solver_ilp import ThroughputSolverILP


def make_dropped_csv(real_thr_csv, drop_frac, out_dir):
    df = pd.read_csv(real_thr_csv).copy()
    col = "throughput_sent" if "throughput_sent" in df.columns else df.columns[-1]
    df[col] = df[col].astype(float) * (1.0 - drop_frac)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"thr_drop{int(drop_frac*100):02d}.csv"
    df.to_csv(out, index=False)
    return str(out)


def solve_one(solver, src, dst, nmax, lam, budget):
    p = ThroughputProblem(
        src=src, dst=dst, instance_limit=nmax,
        min_stream_throughput_gbps=lam,
        target_avg_batch_latency_ms=budget,
    )
    try:
        sol = solver.solve_skyhost_streaming(p, mode="COST", max_hops=2)
        return (bool(sol.is_feasible),
                float(sol.cost_total) if sol.is_feasible else None)
    except Exception:
        return False, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--throughput", default="data/mc_throughput_real.csv")
    ap.add_argument("--latency", default="data/mc_latency_real.csv")
    ap.add_argument("--cost", default="data/mc_cost.csv")
    ap.add_argument("--drops", default="0.10,0.20")
    ap.add_argument("--lambdas", default="3,5,8")
    ap.add_argument("--budgets", default="500,1000")
    ap.add_argument("--instance-limits", default="2,4")
    ap.add_argument("--pairs", default="aws:us-east-1:azure:brazilsouth,aws:us-east-1:gcp:europe-west1,gcp:asia-northeast1:azure:eastus,azure:eastus:aws:eu-west-1,gcp:us-east1:aws:sa-east-1,azure:japaneast:gcp:us-west1")
    ap.add_argument("--output", default="results/uniform_drop.csv")
    args = ap.parse_args()

    drops = [float(x) for x in args.drops.split(",")]
    lams = [float(x) for x in args.lambdas.split(",")]
    budgets = [float(x) for x in args.budgets.split(",")]
    nmaxes = [int(x) for x in args.instance_limits.split(",")]
    pair_strs = args.pairs.split(",")
    pairs = []
    for p in pair_strs:
        parts = p.split(":")
        src = ":".join(parts[:2])
        dst = ":".join(parts[2:])
        pairs.append((src, dst))

    print("[Phase 1] Nominal solve...", flush=True)
    nominal_solver = ThroughputSolverILP(
        df_path=args.throughput, cost_df_path=args.cost,
        latency_df_path=args.latency,
    )
    nominal = {}
    for src, dst in pairs:
        for nmax in nmaxes:
            for lam in lams:
                for bud in budgets:
                    feas, cost = solve_one(nominal_solver, src, dst, nmax, lam, bud)
                    nominal[(src, dst, nmax, lam, bud)] = (feas, cost)
    print(f"  {sum(1 for v in nominal.values() if v[0])}/{len(nominal)} feasible", flush=True)

    out_dir = Path("data/_drop")
    rows = []
    for d in drops:
        print(f"[Phase 2] uniform drop = {d*100:.0f}%", flush=True)
        thr_p = make_dropped_csv(args.throughput, d, out_dir)
        dropped_solver = ThroughputSolverILP(
            df_path=thr_p, cost_df_path=args.cost,
            latency_df_path=args.latency,
        )
        for (src, dst, nmax, lam, bud), (feas_n, cost_n) in nominal.items():
            feas_d, cost_d = solve_one(dropped_solver, src, dst, nmax, lam, bud)
            rel = None
            if feas_n and feas_d and cost_n and cost_d:
                rel = (cost_d - cost_n) / cost_n * 100.0
            rows.append({
                "drop": d, "src": src, "dst": dst,
                "instance_limit": nmax, "lambda_gbps": lam, "budget_ms": bud,
                "feasible_nominal": feas_n, "feasible_dropped": feas_d,
                "cost_nominal": cost_n, "cost_dropped": cost_d,
                "cost_rel_change_pct": rel,
            })

    df = pd.DataFrame(rows)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"\nWrote {out} ({len(df)} rows)\n")

    print("=" * 70)
    print("UNIFORM-DROP SENSITIVITY SUMMARY")
    print("=" * 70)
    for d in drops:
        sub = df[df["drop"] == d]
        flips_to_infeas = sub[sub.feasible_nominal & (~sub.feasible_dropped)]
        common = sub[sub.feasible_nominal & sub.feasible_dropped]
        print(f"\nDROP = {d*100:.0f}%  (total scenarios n={len(sub)})")
        print(f"  feasibility flips nominal->infeas: "
              f"{len(flips_to_infeas)}/{len(sub)} "
              f"({100*len(flips_to_infeas)/len(sub):.1f}%)")
        if len(common) > 0:
            ch = common.cost_rel_change_pct
            print(f"  cost change (mutually feasible n={len(common)}):"
                  f"  mean=+{ch.mean():.2f}%  median=+{ch.median():.2f}%"
                  f"  max=+{ch.max():.2f}%")


if __name__ == "__main__":
    main()
