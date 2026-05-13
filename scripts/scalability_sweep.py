#!/usr/bin/env python3
"""Measure MILP solve time as a function of topology size |V|.

Methodology: extend the 18-region measured topology to |V| > 18 by
bootstrapping new region properties (throughput, RTT, cost) from the
empirical joint distribution of measured cross-region pairs, preserving
intra-cloud vs cross-cloud structure.

We do NOT report feasibility or cost on synthetic topologies (those are
only meaningful on real measurements). The purpose is purely to
characterize solver runtime vs |V|.
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from solver.solver import ThroughputProblem, ThroughputSolver
from solver.solver_ilp import ThroughputSolverILP


def bootstrap_region_csvs(real_thr_csv, real_lat_csv, real_cost_csv,
                          target_v, seed=42, out_dir="data/_synthetic"):
    """Create extended topology CSVs by replicating real regions with
    bootstrapped names. Each new synthetic region inherits a real
    region's identity (provider:region) so cost lookups still work,
    but with a "_dup{i}" suffix so it's a distinct node in the graph."""
    rng = np.random.default_rng(seed)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    thr = pd.read_csv(real_thr_csv)
    lat = pd.read_csv(real_lat_csv)
    cost = pd.read_csv(real_cost_csv)

    real_regions = sorted(set(thr.src_region.unique()) | set(thr.dst_region.unique()))
    n_real = len(real_regions)
    if target_v <= n_real:
        # subset case
        keep = list(rng.choice(real_regions, size=target_v, replace=False))
        thr_sub = thr[thr.src_region.isin(keep) & thr.dst_region.isin(keep)].copy()
        lat_sub = lat[lat.src_region.isin(keep) & lat.dst_region.isin(keep)].copy()
        thr_path = out_dir / f"thr_V{target_v}.csv"
        lat_path = out_dir / f"lat_V{target_v}.csv"
        thr_sub.to_csv(thr_path, index=False)
        lat_sub.to_csv(lat_path, index=False)
        return str(thr_path), str(lat_path), str(real_cost_csv), keep

    # extension case: replicate real regions with synthetic suffixes
    n_extra = target_v - n_real
    extra_names = []
    template_map = {}  # synthetic_name -> real_region (for cost lookup)
    cost_rows_extra = []
    real_cost_df = cost.set_index(["src", "dst"]).copy()
    cost_records = cost.to_dict("records")

    for i in range(n_extra):
        template = rng.choice(real_regions)
        # Build synthetic name: keep the provider prefix so the solver's
        # provider-based NIC limits still resolve correctly.
        provider, region = template.split(":", 1)
        synthetic_name = f"{provider}:{region}__dup{i+1}"
        extra_names.append(synthetic_name)
        template_map[synthetic_name] = template

    all_regions = list(real_regions) + extra_names

    # Throughput CSV: add rows for (synthetic, *) and (*, synthetic) by
    # cloning the template region's measurements.
    new_thr_rows = []
    real_thr_map = thr.set_index(["src_region", "dst_region", "src_tier", "dst_tier"])
    for s in all_regions:
        for d in all_regions:
            if s == d:
                continue
            s_template = template_map.get(s, s)
            d_template = template_map.get(d, d)
            if s_template == d_template:
                # Same real region cloned twice: use median intra-cloud
                same_prov = thr[thr.src_region.str.startswith(s_template.split(":")[0]) &
                                thr.dst_region.str.startswith(s_template.split(":")[0])]
                if len(same_prov):
                    base = same_prov.sample(1, random_state=int(rng.integers(0, 1e9))).iloc[0]
                else:
                    continue
            else:
                key = (s_template, d_template, "PREMIUM", "PREMIUM")
                if key in real_thr_map.index:
                    base = real_thr_map.loc[key]
                    if isinstance(base, pd.DataFrame):
                        base = base.iloc[0]
                else:
                    continue
            new_thr_rows.append({
                "src_region": s, "dst_region": d,
                "src_tier": "PREMIUM", "dst_tier": "PREMIUM",
                "throughput_sent": float(base["throughput_sent"]),
            })

    new_lat_rows = []
    real_lat_map = lat.set_index(["src_region", "dst_region"])
    for s in all_regions:
        for d in all_regions:
            if s == d:
                continue
            s_template = template_map.get(s, s)
            d_template = template_map.get(d, d)
            if s_template == d_template:
                same_prov_lat = lat[lat.src_region.str.startswith(s_template.split(":")[0]) &
                                    lat.dst_region.str.startswith(s_template.split(":")[0])]
                if len(same_prov_lat):
                    base = same_prov_lat.sample(1, random_state=int(rng.integers(0, 1e9))).iloc[0]
                else:
                    continue
            else:
                if (s_template, d_template) in real_lat_map.index:
                    base = real_lat_map.loc[(s_template, d_template)]
                    if isinstance(base, pd.DataFrame):
                        base = base.iloc[0]
                else:
                    continue
            new_lat_rows.append({
                "src_region": s, "dst_region": d,
                "latency_rtt_ms": float(base["latency_rtt_ms"]),
            })

    # Extend cost CSV: ensure cost lookup works for new regions
    # Cost lookup splits provider:region and uses (region, dst_region_or_internet)
    # New synthetic regions reuse the original region name for cost lookup, but
    # because src and dst with "__dup" appear with different region parts, we
    # need entries for those region parts too.
    cost_idx = set(cost.set_index(["src","dst"]).index)
    new_cost_rows = []
    for s in extra_names:
        s_provider, s_region = s.split(":", 1)
        s_template_region = template_map[s].split(":", 1)[1]
        # For every existing dst, mirror the template's cost
        for orig_idx, row in cost.iterrows():
            if row["src"] == s_template_region:
                new_cost_rows.append({"src": s_region, "dst": row["dst"], "cost": row["cost"]})
            if row["dst"] == s_template_region:
                new_cost_rows.append({"src": row["src"], "dst": s_region, "cost": row["cost"]})
        # Also need cost from synthetic to other synthetics: use "internet" intra-cloud
    # Add internet egress entries for synthetic region_parts
    for s in extra_names:
        s_template_region = template_map[s].split(":", 1)[1]
        # Find the template's "internet" cost
        tpl_internet = cost[(cost["src"] == s_template_region) & (cost["dst"] == "internet")]
        if len(tpl_internet):
            s_region = s.split(":", 1)[1]
            new_cost_rows.append({"src": s_region, "dst": "internet", "cost": tpl_internet.iloc[0]["cost"]})

    cost_extended = pd.concat([cost, pd.DataFrame(new_cost_rows)], ignore_index=True).drop_duplicates(["src","dst"])
    thr_extended = pd.concat([thr, pd.DataFrame(new_thr_rows)], ignore_index=True).drop_duplicates(["src_region","dst_region","src_tier","dst_tier"])
    lat_extended = pd.concat([lat, pd.DataFrame(new_lat_rows)], ignore_index=True).drop_duplicates(["src_region","dst_region"])

    thr_path = out_dir / f"thr_V{target_v}.csv"
    lat_path = out_dir / f"lat_V{target_v}.csv"
    cost_path = out_dir / f"cost_V{target_v}.csv"
    thr_extended.to_csv(thr_path, index=False)
    lat_extended.to_csv(lat_path, index=False)
    cost_extended.to_csv(cost_path, index=False)
    return str(thr_path), str(lat_path), str(cost_path), all_regions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--throughput", default="data/mc_throughput_real.csv")
    parser.add_argument("--latency", default="data/mc_latency_real.csv")
    parser.add_argument("--cost", default="data/mc_cost.csv")
    parser.add_argument("--sizes", default="6,12,18,24,36,50")
    parser.add_argument("--probes-per-size", type=int, default=6,
                        help="Number of MILP solves per topology size (random src-dst pairs)")
    parser.add_argument("--hops", default="1,2")
    parser.add_argument("--lambda-rate", type=float, default=8.0)
    parser.add_argument("--budget-ms", type=float, default=500.0)
    parser.add_argument("--instance-limit", type=int, default=2)
    parser.add_argument("--output", default="results/scalability.csv")
    args = parser.parse_args()

    sizes = [int(x) for x in args.sizes.split(",")]
    hops_list = [int(x) for x in args.hops.split(",")]
    rng = np.random.default_rng(123)
    rows = []

    for V in sizes:
        print(f"\n=== |V| = {V} ===", flush=True)
        thr_p, lat_p, cost_p, regions = bootstrap_region_csvs(
            args.throughput, args.latency, args.cost, target_v=V, seed=V)
        solver = ThroughputSolverILP(df_path=thr_p, cost_df_path=cost_p, latency_df_path=lat_p)
        all_regions = solver.get_regions()
        if len(all_regions) < 2:
            print(f"  [SKIP] only {len(all_regions)} regions resolved")
            continue

        # pick probe pairs
        pairs = []
        seen = set()
        attempts = 0
        while len(pairs) < args.probes_per_size and attempts < 200:
            s, d = rng.choice(all_regions, size=2, replace=False)
            if (s, d) in seen:
                attempts += 1; continue
            seen.add((s, d))
            pairs.append((s, d))
            attempts += 1

        for hops in hops_list:
            # warmup
            warm = ThroughputProblem(
                src=pairs[0][0], dst=pairs[0][1],
                instance_limit=args.instance_limit,
                min_stream_throughput_gbps=1.0,
                target_avg_batch_latency_ms=1000.0,
            )
            try:
                solver.solve_skyhost_streaming(warm, mode="COST", max_hops=hops)
            except Exception:
                pass

            for s, d in pairs:
                problem = ThroughputProblem(
                    src=s, dst=d,
                    instance_limit=args.instance_limit,
                    min_stream_throughput_gbps=args.lambda_rate,
                    target_avg_batch_latency_ms=args.budget_ms,
                )
                t0 = time.perf_counter()
                try:
                    sol = solver.solve_skyhost_streaming(problem, mode="COST", max_hops=hops)
                    dt = time.perf_counter() - t0
                    n_paths = (sol.extra_data.get("num_candidate_paths")
                               if sol.extra_data else None)
                    feasible = bool(sol.is_feasible)
                except Exception as e:
                    dt = time.perf_counter() - t0
                    n_paths = None
                    feasible = False
                rows.append({
                    "V": V, "max_hops": hops, "src": s, "dst": d,
                    "num_candidate_paths": n_paths,
                    "feasible": feasible,
                    "solve_time_s": dt,
                })
                print(f"  V={V} hops={hops}  {s}->{d}  paths={n_paths}  "
                      f"feas={feasible}  t={dt*1000:.0f}ms", flush=True)

    df = pd.DataFrame(rows)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"\nWrote {out} ({len(df)} rows)\n")

    print("=" * 80)
    print("SCALABILITY SUMMARY")
    print("=" * 80)
    print(f"{'V':<5}{'hops':<6}{'#paths(med)':<14}{'solve_med(ms)':<16}{'solve_p95(ms)':<16}")
    for V in sizes:
        for h in hops_list:
            sub = df[(df.V == V) & (df.max_hops == h)]
            if len(sub) == 0:
                continue
            n = int(sub.num_candidate_paths.median()) if sub.num_candidate_paths.notna().any() else 0
            med = sub.solve_time_s.median() * 1000
            p95 = sub.solve_time_s.quantile(0.95) * 1000
            print(f"{V:<5}{h:<6}{n:<14}{med:<16.0f}{p95:<16.0f}")


if __name__ == "__main__":
    main()
