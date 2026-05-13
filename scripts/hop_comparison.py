#!/usr/bin/env python3
"""Compare MILP cost/feasibility/solve-time for max_hops in {1, 2, 3}.

Runs a representative subset of scenarios for each hop limit and reports:
  - Number of candidate paths
  - Feasibility rate
  - Average cost (across both-feasible scenarios for fair comparison)
  - Median and p95 solve time

Saves results to results/hop_comparison.csv.
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from solver.solver import ThroughputProblem
from solver.solver_ilp import ThroughputSolverILP


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--throughput", default="data/mc_throughput_real.csv")
    parser.add_argument("--latency", default="data/mc_latency_real.csv")
    parser.add_argument("--cost", default="data/mc_cost.csv")
    parser.add_argument("--pairs", default=(
        "aws:us-east-1:azure:brazilsouth,"
        "aws:us-east-1:gcp:europe-west1,"
        "gcp:asia-northeast1:azure:eastus,"
        "azure:japaneast:gcp:us-west1"
    ))
    parser.add_argument("--instance-limits", default="2,4")
    parser.add_argument("--lambdas", default="3,5,10")
    parser.add_argument("--budgets", default="200,500,1000")
    parser.add_argument("--hops", default="1,2,3")
    parser.add_argument("--output", default="results/hop_comparison.csv")
    args = parser.parse_args()

    lambda_list = [float(x) for x in args.lambdas.split(",")]
    budget_list = [float(x) for x in args.budgets.split(",")]
    nmax_list = [int(x) for x in args.instance_limits.split(",")]
    hops_list = [int(x) for x in args.hops.split(",")]

    pairs = []
    for ps in args.pairs.split(","):
        parts = ps.strip().split(":")
        pairs.append((f"{parts[0]}:{parts[1]}", f"{parts[2]}:{parts[3]}"))

    solver = ThroughputSolverILP(
        df_path=str(Path(args.throughput)),
        cost_df_path=str(Path(args.cost)),
        latency_df_path=str(Path(args.latency)),
    )

    rows = []
    total = len(pairs) * len(nmax_list) * len(lambda_list) * len(budget_list) * len(hops_list)
    i = 0
    for hops in hops_list:
        # Warm-up for this hop limit
        warm = ThroughputProblem(
            src=pairs[0][0], dst=pairs[0][1],
            instance_limit=2, min_stream_throughput_gbps=1.0,
            target_avg_batch_latency_ms=500.0,
        )
        for _ in range(2):
            solver.solve_skyhost_streaming(warm, mode="COST", max_hops=hops)

        for src, dst in pairs:
            for nmax in nmax_list:
                for lam in lambda_list:
                    for bud in budget_list:
                        i += 1
                        if i % 10 == 1 or i == total:
                            print(f"[{i}/{total}] hops={hops} {src}->{dst} "
                                  f"N={nmax} λ={lam} B={bud}", flush=True)
                        problem = ThroughputProblem(
                            src=src, dst=dst,
                            instance_limit=nmax,
                            min_stream_throughput_gbps=lam,
                            target_avg_batch_latency_ms=bud,
                        )
                        t0 = time.perf_counter()
                        sol = solver.solve_skyhost_streaming(problem, mode="COST", max_hops=hops)
                        dt = time.perf_counter() - t0
                        rows.append({
                            "max_hops": hops,
                            "src": src, "dst": dst,
                            "instance_limit": nmax,
                            "lambda_gbps": lam,
                            "latency_budget_ms": bud,
                            "feasible": bool(sol.is_feasible),
                            "cost_total_hr": sol.cost_total if sol.is_feasible else None,
                            "batch_size_mb": (sol.extra_data.get("batch_size_mb")
                                              if sol.is_feasible and sol.extra_data else None),
                            "num_candidate_paths": (sol.extra_data.get("num_candidate_paths")
                                                    if sol.extra_data else None),
                            "num_active_paths": (len(sol.extra_data.get("active_paths", []))
                                                 if sol.is_feasible and sol.extra_data else 0),
                            "solve_time_s": dt,
                        })

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_csv(out, index=False)
    print(f"\nWrote {out} ({len(df)} rows)\n")

    print("=" * 76)
    print("HOP-COMPARISON SUMMARY")
    print("=" * 76)
    print(f"{'hops':<5}{'#paths(med)':<14}{'feas':<10}{'solve(med)':<14}{'solve(p95)':<14}{'avg_cost($/hr)':<16}")
    for h in hops_list:
        s = df[df["max_hops"] == h]
        n_paths = int(s["num_candidate_paths"].median()) if s["num_candidate_paths"].notna().any() else 0
        feas = s["feasible"].mean() * 100
        med = s["solve_time_s"].median() * 1000
        p95 = s["solve_time_s"].quantile(0.95) * 1000
        feas_s = s[s["feasible"]]
        avg_c = feas_s["cost_total_hr"].mean() if len(feas_s) else float("nan")
        print(f"{h:<5}{n_paths:<14}{feas:<10.1f}{med:<14.0f}{p95:<14.0f}{avg_c:<16.2f}")

    print("\n--- Cost comparison on mutually-feasible scenarios ---")
    key = ["src", "dst", "instance_limit", "lambda_gbps", "latency_budget_ms"]
    by_hops = {h: df[df["max_hops"] == h].set_index(key) for h in hops_list}
    if 1 in by_hops and 2 in by_hops:
        m12 = by_hops[1].join(by_hops[2], lsuffix="_h1", rsuffix="_h2", how="inner")
        both = m12[m12["feasible_h1"] & m12["feasible_h2"]]
        if len(both):
            saved = ((both["cost_total_hr_h1"] - both["cost_total_hr_h2"])
                     / both["cost_total_hr_h1"] * 100)
            print(f"  hops=2 vs hops=1 ({len(both)} both-feasible): "
                  f"avg cost reduction = {saved.mean():.2f}%  max = {saved.max():.2f}%")
    if 2 in by_hops and 3 in by_hops:
        m23 = by_hops[2].join(by_hops[3], lsuffix="_h2", rsuffix="_h3", how="inner")
        both = m23[m23["feasible_h2"] & m23["feasible_h3"]]
        if len(both):
            saved = ((both["cost_total_hr_h2"] - both["cost_total_hr_h3"])
                     / both["cost_total_hr_h2"] * 100)
            print(f"  hops=3 vs hops=2 ({len(both)} both-feasible): "
                  f"avg cost reduction = {saved.mean():.2f}%  max = {saved.max():.2f}%")


if __name__ == "__main__":
    main()
