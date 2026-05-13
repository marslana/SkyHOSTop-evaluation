import cvxpy as cp
import numpy as np

from solver.solver import ThroughputSolver, ThroughputProblem, ThroughputSolution, logger


class ThroughputSolverILP(ThroughputSolver):
    @staticmethod
    def choose_solver():
        """Selects the best available MILP solver."""
        installed = cp.installed_solvers()
        order = ["SCIP", "GUROBI", "CBC", "GLPK_MI"]
        for package in order:
            if package in installed:
                logger.fs.debug(f"[SkyHOST] Using solver: {package}")
                return getattr(cp, package)

        logger.fs.error("[SkyHOST] No suitable ILP solver found.")
        raise ImportError("No suitable ILP solver found. Install 'pyscipopt' (SCIP) or 'cylp' (CBC).")

    def _enumerate_paths(self, src_idx, dst_idx, n, LIMIT_link, max_hops=1):
        """
        Enumerate candidate paths up to a given number of relay hops.
        max_hops=1 gives direct + 1-hop relays (default, paper formulation).
        max_hops=2 adds 2-hop relay paths (s -> r1 -> r2 -> t), etc.
        Returns list of paths, where each path is a list of node indices.
        Only includes paths whose every edge has positive link capacity.
        """
        paths = []

        # Direct path: s -> t
        if LIMIT_link[src_idx, dst_idx] > 0:
            paths.append([src_idx, dst_idx])

        # 1-hop relay paths: s -> r -> t
        relays = [r for r in range(n) if r != src_idx and r != dst_idx]
        for r in relays:
            if LIMIT_link[src_idx, r] > 0 and LIMIT_link[r, dst_idx] > 0:
                paths.append([src_idx, r, dst_idx])

        # 2-hop relay paths: s -> r1 -> r2 -> t
        if max_hops >= 2:
            for r1 in relays:
                if LIMIT_link[src_idx, r1] <= 0:
                    continue
                for r2 in relays:
                    if r2 == r1:
                        continue
                    if LIMIT_link[r1, r2] > 0 and LIMIT_link[r2, dst_idx] > 0:
                        paths.append([src_idx, r1, r2, dst_idx])

        # 3-hop relay paths: s -> r1 -> r2 -> r3 -> t
        if max_hops >= 3:
            for r1 in relays:
                if LIMIT_link[src_idx, r1] <= 0:
                    continue
                for r2 in relays:
                    if r2 == r1 or LIMIT_link[r1, r2] <= 0:
                        continue
                    for r3 in relays:
                        if r3 == r1 or r3 == r2:
                            continue
                        if LIMIT_link[r2, r3] > 0 and LIMIT_link[r3, dst_idx] > 0:
                            paths.append([src_idx, r1, r2, r3, dst_idx])

        return paths

    def solve_skyhost_streaming(self, p: ThroughputProblem, mode="COST", solver_verbose=False, max_hops=1, fix_sb_mb=None) -> ThroughputSolution:
        """
        Path-Based MILP for SkyHOST Streaming.
        Matches the conference paper formulation:
          - Decision vars: x_p (path flow), delta_p (path active), N_v (VMs), S_b (batch size)
          - 4-component per-path latency: propagation + transmission + processing + batching
          - Strict per-path SLA via Big-M indicator constraints
        """
        print(f"[SkyHOST] Solving Mode: {mode} | Rate: {p.min_stream_throughput_gbps} Gbps", flush=True)

        # --- 1. Setup Constants & Inputs ---
        regions = self.get_regions()
        n = len(regions)
        try:
            src_idx = regions.index(p.src)
            dst_idx = regions.index(p.dst)
        except ValueError:
            return ThroughputSolution(p, False, {"error": "Invalid Src/Dst"})

        lambda_rate = p.min_stream_throughput_gbps
        lat_budget = p.target_avg_batch_latency_ms if p.target_avg_batch_latency_ms else 10000.0

        # Constants (matching paper)
        ALPHA = 4.0         # ms/MB per node (empirically measured processing rate coefficient)
        S_MIN = 1.0         # MB
        S_MAX = 32.0        # MB
        M_F = lambda_rate   # Big-M for flow-indicator linking (Eq. bigm-upper)
        M_L = 100000.0      # Big-M for latency relaxation (Eq. max-latency-bigm)

        # Load Grids
        C_egress_raw = self.get_cost_grid()
        C_egress = np.nan_to_num(C_egress_raw, nan=0.0, posinf=10.0, neginf=0.0)

        L_rtt_raw = self.get_latency_grid()
        L_rtt = np.nan_to_num(L_rtt_raw, nan=9999.0, posinf=9999.0)
        L_owd = L_rtt / 2.0  # One-way delay = RTT/2 (paper: L^OWD)

        LIMIT_link = np.nan_to_num(self.get_throughput_grid(), nan=0.0)

        # VM Cost ($/s)
        if hasattr(p, 'cost_per_instance_hr_vector') and p.cost_per_instance_hr_vector:
            C_vm = np.array(p.cost_per_instance_hr_vector) / 3600.0
        else:
            C_vm = np.full(n, p.cost_per_instance_hr) / 3600.0

        # NIC Limits
        limit_out_list, limit_in_list = [], []
        for r in regions:
            if r.startswith("aws:"):
                lim = p.aws_instance_throughput_limit
            elif r.startswith("gcp:"):
                lim = p.gcp_instance_throughput_limit
            elif r.startswith("azure:"):
                lim = p.azure_instance_throughput_limit
            else:
                lim = (5.0, 5.0)
            limit_out_list.append(lim[0])
            limit_in_list.append(lim[1])
        LIMIT_out = np.array(limit_out_list)
        LIMIT_in = np.array(limit_in_list)

        # Instance limits (Eq. activation)
        if getattr(p, "instance_limit_per_region", None):
            if isinstance(p.instance_limit_per_region, dict):
                limit_vm = np.array([p.instance_limit_per_region.get(r, p.instance_limit) for r in regions])
            else:
                limit_vm = np.array(p.instance_limit_per_region)
                if len(limit_vm) != n:
                    return ThroughputSolution(p, False, {"error": "instance_limit_per_region length mismatch"})
        else:
            limit_vm = np.full(n, p.instance_limit)

        # --- 2. Enumerate Candidate Paths ---
        paths = self._enumerate_paths(src_idx, dst_idx, n, LIMIT_link, max_hops=max_hops)
        num_paths = len(paths)

        if num_paths == 0:
            return ThroughputSolution(p, False, {"error": "No viable paths"})

        print(f"[SkyHOST] Enumerated {num_paths} candidate paths", flush=True)

        # --- 3. Precompute Per-Path Latency Components ---
        # L_total(p) = L_prop(p) + L_tx(p) + L_proc(p) + L_batch
        #            = sum(OWD) + S_b * [sum(8/LIMIT) + alpha*(k+1) + 8/lambda]
        #            = fixed_lat[p] + sb_coeff[p] * S_b
        path_edges = []         # edge list per path
        path_prop_delay = []    # L_prop(p) in ms (fixed, independent of S_b)
        path_tx_coeff = []      # transmission delay coefficient (ms/MB)
        path_proc_coeff = []    # processing delay coefficient: alpha*(k+1) (ms/MB)

        for path in paths:
            edges = list(zip(path[:-1], path[1:]))
            path_edges.append(edges)

            # Propagation delay: sum of OWD along edges (Eq. L_prop)
            prop = sum(L_owd[u, v] for u, v in edges)
            path_prop_delay.append(prop)

            # Processing delay coefficient: alpha * (k+1) nodes (Eq. L_proc)
            path_proc_coeff.append(ALPHA * len(path))

            # Transmission delay coefficient: sum of 8/LIMIT_link per edge (Eq. L_tx)
            tx = sum(8.0 / LIMIT_link[u, v] for u, v in edges)
            path_tx_coeff.append(tx)

        # Batching delay coefficient: 8/lambda (Eq. L_batch), same for all paths
        batch_coeff = 8.0 / lambda_rate

        # Per-path: L_total(p) = fixed_lat[p] + sb_coeff[p] * S_b
        # fixed_lat = propagation only (independent of S_b)
        # sb_coeff  = transmission + processing + batching (all scale with S_b)
        fixed_lat = np.array(path_prop_delay)
        sb_coeff = np.array([path_tx_coeff[i] + path_proc_coeff[i] + batch_coeff
                             for i in range(num_paths)])

        # Build edge-to-path-indices mapping for capacity constraints
        edge_to_pidxs = {}  # (u,v) -> list of path indices
        for i, edges in enumerate(path_edges):
            for (u, v) in edges:
                edge_to_pidxs.setdefault((u, v), []).append(i)

        # --- 4. Decision Variables ---
        x = cp.Variable(num_paths, nonneg=True, name="PathFlow")        # x_p
        delta = cp.Variable(num_paths, boolean=True, name="PathActive")  # delta_p
        N = cp.Variable(n, integer=True, name="VMs")                     # N_v
        Sb = cp.Variable(nonneg=True, name="BatchSize")                  # S_b

        # --- 5. Constraints ---
        cons = []

        # Non-negativity for N
        cons.append(N >= 0)

        # Throughput constraint (Eq. source-throughput): sum x_p >= lambda
        cons.append(cp.sum(x) >= lambda_rate)

        # Flow-indicator linking (Eq. bigm-upper): x_p <= M_f * delta_p
        for i in range(num_paths):
            cons.append(x[i] <= M_F * delta[i])

        # Link capacity (Eq. link-capacity): for each edge, aggregate flow <= N_u * LIMIT
        for (u, v), pidxs in edge_to_pidxs.items():
            cons.append(cp.sum([x[i] for i in pidxs]) <= N[u] * LIMIT_link[u, v])

        # VM bandwidth limits (Eqs. ingress-limit, egress-limit)
        for v in range(n):
            # Egress: total flow on all edges leaving v
            egress_pidxs = set()
            for (u, w), pidxs in edge_to_pidxs.items():
                if u == v:
                    egress_pidxs.update(pidxs)
            if egress_pidxs:
                cons.append(cp.sum([x[i] for i in egress_pidxs]) <= N[v] * LIMIT_out[v])

            # Ingress: total flow on all edges entering v
            ingress_pidxs = set()
            for (u, w), pidxs in edge_to_pidxs.items():
                if w == v:
                    ingress_pidxs.update(pidxs)
            if ingress_pidxs:
                cons.append(cp.sum([x[i] for i in ingress_pidxs]) <= N[v] * LIMIT_in[v])

        # Instance quotas (Eq. activation)
        cons.append(N <= limit_vm)

        # Source and destination must have at least 1 VM (Eq. source-dest-vms)
        cons.append(N[src_idx] >= 1)
        cons.append(N[dst_idx] >= 1)

        # Batch size bounds (Eq. batch-bounds)
        if fix_sb_mb is not None:
            cons.append(Sb == float(fix_sb_mb))
        else:
            cons.append(Sb >= S_MIN)
            cons.append(Sb <= S_MAX)

        # --- 6. Objective ---
        if mode == "COST":
            # Strict per-path SLA (Eq. max-latency-bigm):
            # L_total(p) <= LAT_max + M_L*(1 - delta_p)
            for i in range(num_paths):
                cons.append(fixed_lat[i] + sb_coeff[i] * Sb
                            <= lat_budget + M_L * (1 - delta[i]))

            # Egress cost rate ($/s): C_egress ($/GB) * flow (Gbps) / 8
            egress_terms = []
            for (u, v), pidxs in edge_to_pidxs.items():
                edge_flow = cp.sum([x[i] for i in pidxs])
                egress_terms.append(C_egress[u, v] * edge_flow / 8.0)
            egress_cost = cp.sum(egress_terms) if egress_terms else 0.0

            # VM cost rate ($/s)
            vm_cost = cp.sum(cp.multiply(N, C_vm))

            # Minimize cost with tie-breaking (Eq. cost-objective)
            objective = cp.Minimize(egress_cost + vm_cost - 1e-6 * Sb)

        elif mode == "LATENCY":
            # Min-max latency (Eqs. latency-objective, latency-minmax)
            L_max = cp.Variable(nonneg=True, name="L_max")

            for i in range(num_paths):
                cons.append(fixed_lat[i] + sb_coeff[i] * Sb
                            <= L_max + M_L * (1 - delta[i]))

            objective = cp.Minimize(L_max)

        # --- 7. Solve ---
        prob = cp.Problem(objective, cons)
        solver_engine = self.choose_solver()

        try:
            prob.solve(solver=solver_engine, verbose=solver_verbose)
        except Exception as e:
            return ThroughputSolution(p, False, {"error": str(e)})

        # --- 8. Extract Results ---
        is_feasible = (prob.status == cp.OPTIMAL or prob.status == cp.OPTIMAL_INACCURATE)
        sol = ThroughputSolution(p, is_feasible)

        if is_feasible:
            x_val = x.value
            sb_val = float(Sb.value)

            # Reconstruct edge flow matrix from path flows
            F = np.zeros((n, n))
            for i, edges in enumerate(path_edges):
                flow = x_val[i] if x_val[i] > 1e-8 else 0.0
                for (u, v) in edges:
                    F[u, v] += flow

            sol.var_edge_flow_gigabits = F
            sol.var_instances_per_region = np.round(N.value)

            # Cost in $/hr
            raw_egress = (np.sum(F * C_egress) / 8.0) * 3600
            raw_vm = np.sum(sol.var_instances_per_region * C_vm) * 3600
            sol.cost_egress = raw_egress
            sol.cost_instance = raw_vm
            sol.cost_total = raw_egress + raw_vm

            # Per-path latency for active paths
            active_latencies = []
            active_paths_str = []
            for i in range(num_paths):
                if x_val[i] > 1e-8:
                    lat = fixed_lat[i] + sb_coeff[i] * sb_val
                    active_latencies.append(lat)
                    names = [regions[node] for node in paths[i]]
                    active_paths_str.append(
                        f"{' -> '.join(names)} ({x_val[i]:.3f} Gbps, {lat:.1f} ms)"
                    )

            est_latency = max(active_latencies) if active_latencies else 0.0

            sol.extra_data = {
                "batch_size_mb": sb_val,
                "est_latency_ms": est_latency,
                "mode": mode,
                "solver_status": prob.status,
                "active_paths": active_paths_str,
                "num_candidate_paths": num_paths,
            }
        else:
            sol.extra_data = {"status": prob.status}

        return sol

    # Alias
    def solve_min_cost(self, p, **kwargs):
        if p.min_stream_throughput_gbps:
            return self.solve_skyhost_streaming(p, mode="COST", **kwargs)
        return ThroughputSolution(p, False)
