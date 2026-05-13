#!/usr/bin/env python3
"""E1: Batch-size ablation isolating routing wins from batch wins.

For each (src, dst, lambda, budget, N_max) scenario we run four variants:

  1. SkyHOSTop-MILP (full)              : optimal routing, optimized S_b  
  2. MILP-FixedSb1                      : optimal routing, S_b = 1 MB pinned
  3. XRON-vanilla                       : greedy routing,  S_b = 1 MB
  4. XRON-FairBatch                     : greedy routing,  S_b = MILP's S_b

Comparing 1 vs 4 gives "routing-only" benefit (same S_b).
Comparing 1 vs 2 gives "batch-only" benefit (same routing).
Comparing 1 vs 3 gives the "total" benefit (current paper claim).
"""
import argparse
import sys
import time
from math import ceil
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from solver.solver import ThroughputProblem
from solver.solver_ilp import ThroughputSolverILP
from scripts.eval_baselines import (
    eval_xron_heuristic,
    ALPHA,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--throughput", default="data/mc_throughput_real.csv")
    ap.add_argument("--latency",    default="data/mc_latency_real.csv")
    ap.add_argument("--cost",       default="data/mc_cost.csv")
    ap.add_argument("--pairs", default=(
        "aws:us-east-1:azure:brazilsouth,"
        "aws:us-east-1:gcp:europe-west1,"
        "gcp:asia-northeast1:azure:eastus,"
        "azure:eastus:aws:eu-west-1,"
        "gcp:us-east1:aws:sa-east-1,"
        "azure:japaneast:gcp:us-west1"
    ))
    ap.add_argument("--instance-limits", default="1,2,4")
    ap.add_argument("--lambdas", default="1,2,3,5,8,10,12,15")
    ap.add_argument("--budgets", default="100,200,300,500,1000")
    ap.add_argument("--vm-cost-hr", type=float, default=0.54,
                    help="$/hr per VM (matches ThroughputProblem default used by MILP).")
    ap.add_argument("--output", default="results/batch_ablation.csv")
    args = ap.parse_args()

    pairs = [p.split(":") for p in args.pairs.split(",")]
    pairs = [(":".join(p[0:2]), ":".join(p[2:4])) for p in pairs]
    instance_limits = [int(x) for x in args.instance_limits.split(",")]
    lambdas = [float(x) for x in args.lambdas.split(",")]
    budgets = [float(x) for x in args.budgets.split(",")]

    solver = ThroughputSolverILP(df_path=args.throughput,
                                 cost_df_path=args.cost,
                                 latency_df_path=args.latency)

    regions = solver.get_regions()
    idx = {r: i for i, r in enumerate(regions)}
    C_egress = solver.get_cost_grid()
    L_rtt = solver.get_latency_grid()
    LIMIT_link = solver.get_throughput_grid()

    # Per-region NIC limits (mirrors eval_baselines.main)
    p_dummy = ThroughputProblem(
        src=pairs[0][0], dst=pairs[0][1],
        instance_limit=max(instance_limits),
        min_stream_throughput_gbps=1.0,
        target_avg_batch_latency_ms=500.0,
    )
    limit_out, limit_in = [], []
    for r in regions:
        if r.startswith("aws:"):
            lim = p_dummy.aws_instance_throughput_limit
        elif r.startswith("gcp:"):
            lim = p_dummy.gcp_instance_throughput_limit
        elif r.startswith("azure:"):
            lim = p_dummy.azure_instance_throughput_limit
        else:
            lim = (5.0, 5.0)
        limit_out.append(lim[0])
        limit_in.append(lim[1])
    LIMIT_out = np.array(limit_out)
    LIMIT_in = np.array(limit_in)

    # Warmup
    print("[WARMUP] Running 3 throwaway solves...", flush=True)
    warm = ThroughputProblem(src=pairs[0][0], dst=pairs[0][1],
                             instance_limit=2, min_stream_throughput_gbps=1.0,
                             target_avg_batch_latency_ms=500.0)
    for _ in range(3):
        try:
            solver.solve_skyhost_streaming(warm, mode="COST", max_hops=2)
        except Exception:
            pass

    rows = []
    n_total = len(pairs) * len(instance_limits) * len(lambdas) * len(budgets)
    i = 0
    for (src, dst) in pairs:
        for nmax in instance_limits:
            for lam in lambdas:
                for bud in budgets:
                    i += 1
                    print(f"[{i}/{n_total}] {src}->{dst} N={nmax} λ={lam} B={bud}",
                          flush=True)

                    problem = ThroughputProblem(
                        src=src, dst=dst,
                        instance_limit=nmax,
                        min_stream_throughput_gbps=lam,
                        target_avg_batch_latency_ms=bud,
                    )

                    # --- 1. SkyHOSTop (full optimization) ---
                    t0 = time.perf_counter()
                    sol_full = solver.solve_skyhost_streaming(
                        problem, mode="COST", max_hops=2)
                    t_full = time.perf_counter() - t0
                    if sol_full.is_feasible:
                        c_full = sol_full.cost_total
                        sb_full = sol_full.extra_data.get("batch_size_mb") if sol_full.extra_data else None
                    else:
                        c_full = None
                        sb_full = None

                    # --- 2. MILP-FixedSb1 (routing only, pinned batch) ---
                    t0 = time.perf_counter()
                    sol_fix = solver.solve_skyhost_streaming(
                        problem, mode="COST", max_hops=2, fix_sb_mb=1.0)
                    t_fix = time.perf_counter() - t0
                    c_fix = sol_fix.cost_total if sol_fix.is_feasible else None

                    # --- 3. XRON-vanilla (Sb = 1 MB) ---
                    t0 = time.perf_counter()
                    xron_v = eval_xron_heuristic(
                        src, dst, lam, bud,
                        regions, idx, C_egress, L_rtt, LIMIT_link,
                        LIMIT_out, LIMIT_in, nmax, args.vm_cost_hr)
                    t_xv = time.perf_counter() - t0
                    c_xv = xron_v["cost_total_hr"] if xron_v else None

                    # --- 4. XRON-FairBatch (Sb = MILP's Sb) ---
                    if sb_full is not None:
                        t0 = time.perf_counter()
                        xron_f = eval_xron_heuristic(
                            src, dst, lam, bud,
                            regions, idx, C_egress, L_rtt, LIMIT_link,
                            LIMIT_out, LIMIT_in, nmax, args.vm_cost_hr,
                            s_b_override=sb_full)
                        t_xf = time.perf_counter() - t0
                        c_xf = xron_f["cost_total_hr"] if xron_f else None
                    else:
                        c_xf = None
                        t_xf = 0.0

                    rows.append({
                        "src": src, "dst": dst, "instance_limit": nmax,
                        "lambda_gbps": lam, "budget_ms": bud,
                        "milp_full_cost": c_full,
                        "milp_full_sb": sb_full,
                        "milp_fix1_cost": c_fix,
                        "xron_vanilla_cost": c_xv,
                        "xron_fairbatch_cost": c_xf,
                        "milp_full_time": t_full,
                        "milp_fix1_time": t_fix,
                        "xron_vanilla_time": t_xv,
                        "xron_fairbatch_time": t_xf,
                    })

    df = pd.DataFrame(rows)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"\nWrote {out} ({len(df)} rows)\n")

    # ---------- summary ----------
    print("=" * 72)
    print("BATCH-SIZE ABLATION SUMMARY")
    print("=" * 72)

    feas_full = df.milp_full_cost.notna()
    feas_xv = df.xron_vanilla_cost.notna()
    feas_xf = df.xron_fairbatch_cost.notna()
    feas_fix = df.milp_fix1_cost.notna()

    print(f"Total scenarios: {len(df)}")
    print(f"  MILP-full feasible:        {feas_full.sum()}")
    print(f"  MILP-Sb1 feasible:         {feas_fix.sum()}")
    print(f"  XRON-vanilla feasible:     {feas_xv.sum()}")
    print(f"  XRON-FairBatch feasible:   {feas_xf.sum()}")

    def saving(num_col, den_col, mask):
        sub = df[mask].copy()
        s = (sub[den_col] - sub[num_col]) / sub[den_col] * 100.0
        return s.mean(), s.median(), s.max(), len(sub)

    # Total: MILP-full vs XRON-vanilla, on mutually feasible
    m_total = feas_full & feas_xv
    if m_total.any():
        avg, med, mx, n = saving("milp_full_cost", "xron_vanilla_cost", m_total)
        print(f"\nTotal saving       (MILP-full vs XRON-vanilla, n={n}):"
              f"  avg={avg:.2f}%  med={med:.2f}%  max={mx:.2f}%")

    # Routing-only: MILP-full vs XRON-FairBatch (same Sb)
    m_routing = feas_full & feas_xf
    if m_routing.any():
        avg, med, mx, n = saving("milp_full_cost", "xron_fairbatch_cost", m_routing)
        print(f"Routing-only saving (MILP-full vs XRON-FairBatch, same Sb, n={n}):"
              f"  avg={avg:.2f}%  med={med:.2f}%  max={mx:.2f}%")

    # Routing-only via the other direction: MILP-Sb1 vs XRON-vanilla (both Sb=1)
    m_route2 = feas_fix & feas_xv
    if m_route2.any():
        avg, med, mx, n = saving("milp_fix1_cost", "xron_vanilla_cost", m_route2)
        print(f"Routing-only saving (MILP-Sb1   vs XRON-vanilla,  same Sb=1, n={n}):"
              f"  avg={avg:.2f}%  med={med:.2f}%  max={mx:.2f}%")

    # Batch-only: MILP-full vs MILP-Sb1 (same routing)
    m_batch = feas_full & feas_fix
    if m_batch.any():
        avg, med, mx, n = saving("milp_full_cost", "milp_fix1_cost", m_batch)
        print(f"Batch-only saving  (MILP-full vs MILP-Sb1,        same routing, n={n}):"
              f"  avg={avg:.2f}%  med={med:.2f}%  max={mx:.2f}%")

    # Per-Nmax breakdown for routing-only
    print("\nPer-N_max routing-only saving (MILP-full vs XRON-FairBatch):")
    for nmax in sorted(df.instance_limit.unique()):
        sub = df[(df.instance_limit == nmax) & feas_full & feas_xf]
        if len(sub):
            s = (sub.xron_fairbatch_cost - sub.milp_full_cost) / sub.xron_fairbatch_cost * 100.0
            print(f"  N_max={nmax}: avg={s.mean():5.2f}%  med={s.median():5.2f}%  n={len(sub)}")


if __name__ == "__main__":
    main()
