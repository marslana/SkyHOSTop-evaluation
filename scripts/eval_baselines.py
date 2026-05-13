#!/usr/bin/env python3
"""
Evaluate SkyHOST Path-Based MILP vs Direct vs Single-path Overlay baselines.

Latency model matches conference paper (4-component per-path):
  L_total(p) = L_prop(p) + L_tx(p) + L_proc(p) + L_batch
  - L_prop  = sum of RTT/2 along edges            (ms, fixed)
  - L_tx    = sum of 8*S_b / LIMIT_link per edge   (ms, scales with S_b)
  - L_proc  = alpha * S_b * (k+1) nodes            (ms, scales with S_b)
  - L_batch = 8*S_b / lambda                        (ms, worst-case)
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

# Constants matching solver_ilp.py and paper
ALPHA = 4.0   # ms/MB per node (empirically measured processing rate coefficient)
S_MIN = 1.0
S_MAX = 32.0


def compute_required_vms(lambda_rate, limit_out, limit_in, limit_link):
    """Compute minimum VMs needed to carry lambda_rate given per-VM limits."""
    if lambda_rate <= 0:
        return 0
    denom = min(x for x in [limit_out, limit_in, limit_link] if x > 0)
    if denom <= 0:
        return None
    return int(ceil(lambda_rate / denom))


def eval_path(path, lambda_rate, lat_budget, mode, regions, idx,
              C_egress, L_rtt, LIMIT_link, LIMIT_out, LIMIT_in,
              instance_limit, cost_per_instance_hr):
    """
    Evaluate a single path using the 4-component latency model.
    Returns dict with cost, batch size, latency, and path string, or None if infeasible.
    """
    edges = list(zip(path[:-1], path[1:]))

    # Check link availability
    for u, v in edges:
        if LIMIT_link[idx[u], idx[v]] <= 0:
            return None

    # Required VMs per region (generalized for any path length)
    n_required = {}
    src, dst = path[0], path[-1]

    for i, node in enumerate(path):
        if i == 0:
            link_limit = LIMIT_link[idx[node], idx[path[i + 1]]]
            n = compute_required_vms(lambda_rate, LIMIT_out[idx[node]], float("inf"), link_limit)
        elif i == len(path) - 1:
            n = int(ceil(lambda_rate / LIMIT_in[idx[node]])) if LIMIT_in[idx[node]] > 0 else None
        else:
            link_limit = LIMIT_link[idx[node], idx[path[i + 1]]]
            n = compute_required_vms(lambda_rate, LIMIT_out[idx[node]], LIMIT_in[idx[node]], link_limit)
        if n is None:
            return None
        n_required[node] = max(n_required.get(node, 0), n)

    # Instance limit cap
    if any(n > instance_limit for n in n_required.values()):
        return None

    # Costs ($/hr)
    egress_rate = 0.0
    for u, v in edges:
        egress_rate += lambda_rate * C_egress[idx[u], idx[v]] / 8.0
    cost_egress_hr = egress_rate * 3600.0
    vm_cost_hr = sum(n_required.values()) * cost_per_instance_hr
    cost_total_hr = cost_egress_hr + vm_cost_hr

    # --- 4-component per-path latency model ---
    # L_total(p) = L_prop(p) + S_b * [tx_coeff + alpha*(k+1) + 8/lambda]

    # 1. Propagation delay: sum of OWD = RTT/2 (ms) -- fixed, independent of S_b
    prop_delay = sum(L_rtt[idx[u], idx[v]] / 2.0 for u, v in edges)

    # 2. Processing delay coefficient: alpha * num_nodes (ms per MB)
    proc_coeff = ALPHA * len(path)

    # 3. Transmission delay coefficient: sum of 8/LIMIT_link per edge (ms per MB)
    tx_coeff = sum(8.0 / LIMIT_link[idx[u], idx[v]]
                   for u, v in edges if LIMIT_link[idx[u], idx[v]] > 0)

    # 4. Batching delay coefficient: 8/lambda (ms per MB, worst-case)
    batch_coeff = 8.0 / lambda_rate

    # Total: fixed_lat (propagation only) + sb_coeff * S_b
    sb_coeff = tx_coeff + proc_coeff + batch_coeff
    fixed_lat = prop_delay

    # L_total = fixed_lat + sb_coeff * Sb
    if mode == "COST":
        budget = lat_budget if lat_budget is not None else 10000.0
        # Sb <= (budget - fixed_lat) / sb_coeff
        slack = budget - fixed_lat
        if slack < sb_coeff * S_MIN:
            return None  # Infeasible even with minimum batch size
        sb = min(S_MAX, max(S_MIN, slack / sb_coeff))
        est_latency = fixed_lat + sb_coeff * sb
        if est_latency > budget + 1e-6:
            return None
    else:
        # LATENCY mode: minimize latency → use smallest batch
        sb = S_MIN
        est_latency = fixed_lat + sb_coeff * sb

    return {
        "cost_total_hr": cost_total_hr,
        "batch_size_mb": sb,
        "est_latency_ms": est_latency,
        "path": " -> ".join(path),
    }


def eval_direct_and_heuristic(src, dst, lambda_rate, lat_budget, mode,
                               regions, idx, C_egress, L_rtt, LIMIT_link,
                               LIMIT_out, LIMIT_in, instance_limit,
                               cost_per_instance_hr):
    """Evaluate direct path and best single-path overlay (1-hop + 2-hop relays)."""
    # Direct path
    direct = eval_path(
        [src, dst], lambda_rate, lat_budget, mode, regions, idx,
        C_egress, L_rtt, LIMIT_link, LIMIT_out, LIMIT_in,
        instance_limit, cost_per_instance_hr,
    )

    relays = [r for r in regions if r not in (src, dst)]

    # Single-path overlay: try all 1-hop and 2-hop relay paths, pick best
    best = direct
    # 1-hop relays
    for r in relays:
        if LIMIT_link[idx[src], idx[r]] > 0 and LIMIT_link[idx[r], idx[dst]] > 0:
            candidate = eval_path(
                [src, r, dst], lambda_rate, lat_budget, mode, regions, idx,
                C_egress, L_rtt, LIMIT_link, LIMIT_out, LIMIT_in,
                instance_limit, cost_per_instance_hr,
            )
            if candidate is None:
                continue
            if best is None or (mode == "COST" and candidate["cost_total_hr"] < best["cost_total_hr"]) or \
               (mode != "COST" and candidate["est_latency_ms"] < best["est_latency_ms"]):
                best = candidate

    # 2-hop relays
    for r1 in relays:
        if LIMIT_link[idx[src], idx[r1]] <= 0:
            continue
        for r2 in relays:
            if r2 == r1 or LIMIT_link[idx[r1], idx[r2]] <= 0 or LIMIT_link[idx[r2], idx[dst]] <= 0:
                continue
            candidate = eval_path(
                [src, r1, r2, dst], lambda_rate, lat_budget, mode, regions, idx,
                C_egress, L_rtt, LIMIT_link, LIMIT_out, LIMIT_in,
                instance_limit, cost_per_instance_hr,
            )
            if candidate is None:
                continue
            if best is None or (mode == "COST" and candidate["cost_total_hr"] < best["cost_total_hr"]) or \
               (mode != "COST" and candidate["est_latency_ms"] < best["est_latency_ms"]):
                best = candidate

    return direct, best


def eval_xron_heuristic(src, dst, lambda_rate, lat_budget,
                        regions, idx, C_egress, L_rtt, LIMIT_link,
                        LIMIT_out, LIMIT_in, instance_limit,
                        cost_per_instance_hr, s_b_override=None):
    """
    XRON Algorithm 1 – greedy latency-first path control.

    Faithfully implements Algorithm 1 from Wu et al., "XRON: A Hybrid
    Elastic Cloud Overlay Network for Video Conferencing at Planetary
    Scale", ACM SIGCOMM 2023, §5.3.

    Key behaviour preserved from the paper:
      - Builds candidate paths (shortest-path graph on topology G).
      - Sorts streams by latency descending; assigns each stream to its
        shortest (lowest-latency) path.
      - Greedily allocates c = min(demand, path.capacity).
      - Removes saturated paths; repeats until demand is met.
      - Does NOT optimise for cost (latency-first only).

    Adaptation for our single-stream, path-based evaluation:
      - One stream (src→dst) with demand = lambda_rate.
      - Candidate paths: direct + all 1-hop + 2-hop relays.
      - 4-component latency model with S_MIN (latency-first).
      - Capacity per region/link scales linearly with instance_limit.
    """
    # ── Build candidate paths (XRON line 7: shortest-path graph) ──
    candidates = []
    if LIMIT_link[idx[src], idx[dst]] > 0:
        candidates.append([src, dst])
    relays = [r for r in regions if r not in (src, dst)]
    for r in relays:
        if LIMIT_link[idx[src], idx[r]] > 0 and LIMIT_link[idx[r], idx[dst]] > 0:
            candidates.append([src, r, dst])
    for r1 in relays:
        if LIMIT_link[idx[src], idx[r1]] <= 0:
            continue
        for r2 in relays:
            if r2 == r1 or LIMIT_link[idx[r1], idx[r2]] <= 0 or LIMIT_link[idx[r2], idx[dst]] <= 0:
                continue
            candidates.append([src, r1, r2, dst])

    # ── Compute latency per path, filter by SLA ──
    feasible = []
    for path in candidates:
        edges = list(zip(path[:-1], path[1:]))

        prop = sum(L_rtt[idx[u], idx[v]] / 2.0 for u, v in edges)
        tx = sum(8.0 / LIMIT_link[idx[u], idx[v]] for u, v in edges)
        proc = ALPHA * len(path)
        batch = 8.0 / lambda_rate

        sb_used = s_b_override if s_b_override is not None else S_MIN
        lat = prop + (tx + proc + batch) * sb_used

        if lat > lat_budget + 1e-6:
            continue

        feasible.append({
            "path": path,
            "edges": edges,
            "latency": lat,
        })

    if not feasible:
        return None

    # ── Sort by latency ascending – XRON assigns shortest path first ──
    feasible.sort(key=lambda x: x["latency"])

    # ── Greedy allocation (Algorithm 1, lines 6-21) ──
    remaining = lambda_rate
    region_load = {}
    link_load = {}
    allocations = []

    for pinfo in feasible:
        if remaining <= 1e-9:
            break

        path = pinfo["path"]
        edges = pinfo["edges"]

        avail = remaining

        for i, r in enumerate(path):
            if i == 0:
                cap = LIMIT_out[idx[r]] * instance_limit
            elif i == len(path) - 1:
                cap = LIMIT_in[idx[r]] * instance_limit
            else:
                cap = min(LIMIT_in[idx[r]], LIMIT_out[idx[r]]) * instance_limit
            avail = min(avail, cap - region_load.get(r, 0.0))

        for u, v in edges:
            cap = LIMIT_link[idx[u], idx[v]] * instance_limit
            avail = min(avail, cap - link_load.get((u, v), 0.0))

        if avail <= 1e-9:
            continue

        allocated = min(remaining, avail)

        for r in path:
            region_load[r] = region_load.get(r, 0.0) + allocated
        for u, v in edges:
            link_load[(u, v)] = link_load.get((u, v), 0.0) + allocated

        allocations.append((pinfo, allocated))
        remaining -= allocated

    if remaining > 1e-6:
        return None

    # ── Compute cost (same model as MILP for fair comparison) ──
    egress_hr = 0.0
    for pinfo, rate in allocations:
        for u, v in pinfo["edges"]:
            egress_hr += rate * C_egress[idx[u], idx[v]] / 8.0 * 3600.0

    # VM computation: mirrors MILP constraints.
    # n_i >= ceil(flow / min(NIC, link_limit)) for each active link.
    total_vms = 0
    for r in regions:
        load = region_load.get(r, 0.0)
        if load <= 1e-9:
            continue
        n_r = 0
        if r == src:
            n_r = int(ceil(load / LIMIT_out[idx[r]]))
        elif r == dst:
            n_r = int(ceil(load / LIMIT_in[idx[r]]))
        else:
            n_r = max(int(ceil(load / LIMIT_in[idx[r]])),
                      int(ceil(load / LIMIT_out[idx[r]])))
        # Link capacity constrains the sender (N_u), not the receiver.
        # Only check outgoing links from region r.
        for pinfo, rate in allocations:
            path = pinfo["path"]
            if r not in path:
                continue
            ri = path.index(r)
            if ri < len(path) - 1:
                ll = LIMIT_link[idx[r], idx[path[ri + 1]]]
                if ll > 0:
                    n_r = max(n_r, int(ceil(rate / ll)))
        total_vms += n_r

    vm_hr = total_vms * cost_per_instance_hr
    cost_hr = egress_hr + vm_hr

    path_str = "; ".join(" -> ".join(p["path"]) for p, _ in allocations)
    max_lat = max(p["latency"] for p, _ in allocations)

    return {
        "cost_total_hr": cost_hr,
        "batch_size_mb": s_b_override if s_b_override is not None else S_MIN,
        "est_latency_ms": max_lat,
        "path": path_str,
    }


def main():
    parser = argparse.ArgumentParser(description="Evaluate MILP vs direct vs single-path overlay baselines.")
    parser.add_argument("--throughput", default="data/mc_throughput_real.csv")
    parser.add_argument("--latency", default="data/mc_latency_real.csv")
    parser.add_argument("--cost", default="data/mc_cost.csv")
    parser.add_argument("--src", default="aws:us-east-1")
    parser.add_argument("--dst", default="aws:ap-southeast-1")
    parser.add_argument("--pairs", default="", help="Comma-separated src:dst pairs")
    parser.add_argument("--instance-limits", default="1,2,4", help="Comma-separated instance limits")
    parser.add_argument("--lambdas", default="1,2,3,4,5,6,7,8,10")
    parser.add_argument("--budgets", default="50,100,200,300,500,1000,2000")
    parser.add_argument("--output", default="results/mc_eval_results.csv")
    args = parser.parse_args()

    lambda_list = [float(x) for x in args.lambdas.split(",") if x.strip()]
    budget_list = [float(x) for x in args.budgets.split(",") if x.strip()]
    instance_limits = [int(x) for x in args.instance_limits.split(",") if x.strip()]

    solver = ThroughputSolverILP(
        df_path=str(Path(args.throughput)),
        cost_df_path=str(Path(args.cost)),
        latency_df_path=str(Path(args.latency)),
    )

    regions = solver.get_regions()
    idx = {r: i for i, r in enumerate(regions)}
    C_egress = solver.get_cost_grid()
    L_rtt = solver.get_latency_grid()
    LIMIT_link = solver.get_throughput_grid()

    # Per-region NIC limits from problem defaults
    p = ThroughputProblem(
        src=args.src,
        dst=args.dst,
        instance_limit=max(instance_limits),
        min_stream_throughput_gbps=1.0,
        target_avg_batch_latency_ms=300.0,
    )
    limit_out = []
    limit_in = []
    for r in regions:
        if r.startswith("aws:"):
            lim = p.aws_instance_throughput_limit
        elif r.startswith("gcp:"):
            lim = p.gcp_instance_throughput_limit
        elif r.startswith("azure:"):
            lim = p.azure_instance_throughput_limit
        else:
            lim = (5.0, 5.0)
        limit_out.append(lim[0])
        limit_in.append(lim[1])
    LIMIT_out = np.array(limit_out)
    LIMIT_in = np.array(limit_in)

    rows = []

    if args.pairs:
        pairs = []
        for pair_str in args.pairs.split(","):
            pair_str = pair_str.strip()
            if not pair_str:
                continue
            parts = pair_str.split(":")
            if len(parts) == 4:
                pairs.append((f"{parts[0]}:{parts[1]}", f"{parts[2]}:{parts[3]}"))
            elif len(parts) == 2:
                pairs.append((f"aws:{parts[0]}", f"aws:{parts[1]}"))
            else:
                print(f"WARNING: skipping malformed pair '{pair_str}'")
    else:
        pairs = [
            ("aws:us-east-1", "azure:southeastasia"),
            ("aws:eu-west-1", "gcp:asia-northeast1"),
            ("gcp:us-east1", "azure:brazilsouth"),
            ("gcp:europe-west1", "aws:ap-northeast-1"),
            ("azure:eastus", "gcp:southamerica-east1"),
            ("aws:ap-southeast-1", "azure:westeurope"),
        ]

    # --- Warm-up phase: 5 throwaway MILP + heuristic solves ---
    warmup_src, warmup_dst = pairs[0]
    warmup_problem = ThroughputProblem(
        src=warmup_src,
        dst=warmup_dst,
        instance_limit=2,
        min_stream_throughput_gbps=1.0,
        target_avg_batch_latency_ms=500.0,
    )
    print("[WARMUP] Running 5 throwaway solves to warm up solver...", flush=True)
    for wi in range(5):
        solver.solve_skyhost_streaming(warmup_problem, mode="COST", max_hops=2)
        eval_xron_heuristic(
            warmup_src, warmup_dst, 1.0, 500.0,
            regions, idx, C_egress, L_rtt, LIMIT_link,
            LIMIT_out, LIMIT_in, 2, p.cost_per_instance_hr,
        )
    print("[WARMUP] Done. Starting timed evaluation.", flush=True)

    total_combos = len(pairs) * len(instance_limits) * len(lambda_list) * len(budget_list)
    combo_i = 0

    for src, dst in pairs:
        for instance_limit in instance_limits:
            for lambda_rate in lambda_list:
                for budget in budget_list:
                    for mode in ("COST",):
                        combo_i += 1
                        if combo_i % 50 == 1 or combo_i == total_combos:
                            print(f"[PROGRESS] {combo_i}/{total_combos}  "
                                  f"pair={src}->{dst} inst={instance_limit} "
                                  f"λ={lambda_rate} B={budget} {mode}",
                                  flush=True)
                        # --- SkyHost (our full MILP with SLA) ---
                        problem = ThroughputProblem(
                            src=src,
                            dst=dst,
                            instance_limit=instance_limit,
                            min_stream_throughput_gbps=lambda_rate,
                            target_avg_batch_latency_ms=budget,
                        )
                        t0 = time.perf_counter()
                        sol = solver.solve_skyhost_streaming(problem, mode=mode, max_hops=2)
                        milp_solve_time = time.perf_counter() - t0
                        if sol.is_feasible:
                            rows.append({
                                "baseline": "MILP",
                                "mode": mode,
                                "lambda_gbps": lambda_rate,
                                "latency_budget_ms": budget,
                                "instance_limit": instance_limit,
                                "src": src,
                                "dst": dst,
                                "feasible": True,
                                "cost_total_hr": sol.cost_total,
                                "batch_size_mb": sol.extra_data.get("batch_size_mb") if sol.extra_data else None,
                                "est_latency_ms": sol.extra_data.get("est_latency_ms") if sol.extra_data else None,
                                "path": "; ".join(sol.extra_data.get("active_paths", [])) if sol.extra_data else "MILP",
                                "solve_time_s": milp_solve_time,
                            })
                        else:
                            rows.append({
                                "baseline": "MILP",
                                "mode": mode,
                                "lambda_gbps": lambda_rate,
                                "latency_budget_ms": budget,
                                "instance_limit": instance_limit,
                                "src": src,
                                "dst": dst,
                                "feasible": False,
                                "cost_total_hr": None,
                                "batch_size_mb": None,
                                "est_latency_ms": None,
                                "path": None,
                                "solve_time_s": milp_solve_time,
                            })

                        # --- Direct + Single-path overlay ---
                        t0 = time.perf_counter()
                        direct, heuristic = eval_direct_and_heuristic(
                            src, dst, lambda_rate, budget, mode,
                            regions, idx, C_egress, L_rtt, LIMIT_link,
                            LIMIT_out, LIMIT_in, instance_limit,
                            p.cost_per_instance_hr,
                        )
                        heur_solve_time = time.perf_counter() - t0

                        for name, result in (("Direct", direct), ("Single-path", heuristic)):
                            if result is None:
                                rows.append({
                                    "baseline": name,
                                    "mode": mode,
                                    "lambda_gbps": lambda_rate,
                                    "latency_budget_ms": budget,
                                    "instance_limit": instance_limit,
                                    "src": src,
                                    "dst": dst,
                                    "feasible": False,
                                    "cost_total_hr": None,
                                    "batch_size_mb": None,
                                    "est_latency_ms": None,
                                    "path": None,
                                    "solve_time_s": heur_solve_time,
                                })
                            else:
                                rows.append({
                                    "baseline": name,
                                    "mode": mode,
                                    "lambda_gbps": lambda_rate,
                                    "latency_budget_ms": budget,
                                    "instance_limit": instance_limit,
                                    "src": src,
                                    "dst": dst,
                                    "feasible": True,
                                    "cost_total_hr": result["cost_total_hr"],
                                    "batch_size_mb": result["batch_size_mb"],
                                    "est_latency_ms": result["est_latency_ms"],
                                    "path": result["path"],
                                    "solve_time_s": heur_solve_time,
                                })

                        # --- XRON heuristic (Wu et al., SIGCOMM 2023) ---
                        t0 = time.perf_counter()
                        xron = eval_xron_heuristic(
                            src, dst, lambda_rate, budget,
                            regions, idx, C_egress, L_rtt, LIMIT_link,
                            LIMIT_out, LIMIT_in, instance_limit,
                            p.cost_per_instance_hr,
                        )
                        xron_solve_time = time.perf_counter() - t0
                        if xron is None:
                            rows.append({
                                "baseline": "XRON",
                                "mode": mode,
                                "lambda_gbps": lambda_rate,
                                "latency_budget_ms": budget,
                                "instance_limit": instance_limit,
                                "src": src,
                                "dst": dst,
                                "feasible": False,
                                "cost_total_hr": None,
                                "batch_size_mb": None,
                                "est_latency_ms": None,
                                "path": None,
                                "solve_time_s": xron_solve_time,
                            })
                        else:
                            rows.append({
                                "baseline": "XRON",
                                "mode": mode,
                                "lambda_gbps": lambda_rate,
                                "latency_budget_ms": budget,
                                "instance_limit": instance_limit,
                                "src": src,
                                "dst": dst,
                                "feasible": True,
                                "cost_total_hr": xron["cost_total_hr"],
                                "batch_size_mb": xron["batch_size_mb"],
                                "est_latency_ms": xron["est_latency_ms"],
                                "path": xron["path"],
                                "solve_time_s": xron_solve_time,
                            })

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out_path, index=False)
    print(f"Wrote {out_path} with {len(rows)} rows")


if __name__ == "__main__":
    main()
