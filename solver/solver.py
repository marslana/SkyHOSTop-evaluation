import functools
import logging
import shutil
from collections import namedtuple
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple

GBIT_PER_GBYTE = 8
GB = 1e9

_logger = logging.getLogger("skyhost")


class _LogShim:
    """Minimal shim matching skyplane's logger.fs interface."""
    def debug(self, msg): _logger.debug(msg)
    def info(self, msg): _logger.info(msg)
    def warning(self, msg): _logger.warning(msg)
    def error(self, msg): _logger.error(msg)


class _Logger:
    fs = _LogShim()

logger = _Logger()


@dataclass
class ThroughputProblem:
    src: str
    dst: str
    instance_limit: int
    # Optional per-region VM quota (aligned with solver region order or keyed by region)
    instance_limit_per_region: Optional[Dict[str, int]] = None
    # --- File Transfer Fields (Optional) ---
    # required_throughput_gbits is the *exact* target for files
    required_throughput_gbits: Optional[float] = None
    gbyte_to_transfer: Optional[float] = None
    # --- Streaming Fields (Optional) ---
    # min_stream_throughput_gbps is the *minimum* required for streams
    min_stream_throughput_gbps: Optional[float] = None
    target_avg_batch_latency_ms: Optional[float] = None
    nic_limit_egress: Optional[list] = None  # Make sure these exist or are handled
    nic_limit_ingress: Optional[list] = None

    # --- Grids (Loaded or Passed) ---
    const_throughput_grid_gbits: Optional[np.ndarray] = None
    const_cost_per_gb_grid: Optional[np.ndarray] = None
    const_latency_grid_ms: Optional[np.ndarray] = None # Added Latency Grid
    # --- Default Limits/Costs ---
    aws_instance_throughput_limit: Tuple[float, float] = (5, 10)
    gcp_instance_throughput_limit: Tuple[float, float] = (7, 16)
    azure_instance_throughput_limit: Tuple[float, float] = (16, 16)
    benchmarked_throughput_connections: int = 64
    cost_per_instance_hr: float = 0.54
    instance_cost_multiplier: float = 1.0
    instance_provision_time_s: float = 0.0

    def __post_init__(self):
        # Ensure at least one throughput requirement is set
        if self.required_throughput_gbits is None and self.min_stream_throughput_gbps is None:
            raise ValueError("Either required_throughput_gbits (for files) or min_stream_throughput_gbps (for streams) must be provided.")
        if self.required_throughput_gbits is not None and self.min_stream_throughput_gbps is not None:
            logger.fs.warning("Both required_throughput_gbits and min_stream_throughput_gbps provided; ILP solver will prioritize based on context.")
        # Ensure latency target is only set for streams
        if self.target_avg_batch_latency_ms is not None and self.min_stream_throughput_gbps is None:
             logger.fs.warning("target_avg_batch_latency_ms provided without min_stream_throughput_gbps; latency constraint will be ignored.")

    def to_summary_dict(self):
        """Simple summary of the problem"""
        summary = {k: v for k, v in self.__dict__.items() if not k.startswith("const_") and v is not None}
        return summary


@dataclass
class ThroughputSolution:
    problem: ThroughputProblem
    is_feasible: bool
    extra_data: Optional[Dict] = None # To store cost rates, solver status etc.

    # solution variables
    var_edge_flow_gigabits: Optional[np.ndarray] = None
    var_conn: Optional[np.ndarray] = None
    var_instances_per_region: Optional[np.ndarray] = None

    # solution values
    # Note: For streams, cost_total/egress/instance might represent $/hr
    throughput_achieved_gbits: Optional[List[float]] = None
    cost_egress_by_edge: Optional[np.ndarray] = None # This might be a rate for streams
    cost_egress: Optional[float] = None
    cost_instance: Optional[float] = None
    cost_total: Optional[float] = None
    transfer_runtime_s: Optional[float] = None # Only applicable for file transfers

    # baseline
    baseline_throughput_achieved_gbits: Optional[float] = None
    baseline_cost_egress: Optional[float] = None
    baseline_cost_instance: Optional[float] = None
    baseline_cost_total: Optional[float] = None

    def to_summary_dict(self):
        """Print simple summary of solution."""
        if self.is_feasible:
            # Adjust reporting based on problem type (file vs stream)
            is_streaming = self.problem.min_stream_throughput_gbps is not None
            cost_unit = "/hr" if is_streaming else ""
            runtime_unit = " (N/A for stream)" if is_streaming else "s"

            return {
                "is_feasible": self.is_feasible,
                "solution": {
                    "throughput_achieved_gbits": self.throughput_achieved_gbits,
                    "cost_egress": f"${self.cost_egress:.4f}{cost_unit}" if self.cost_egress is not None else "N/A",
                    "cost_instance": f"${self.cost_instance:.4f}{cost_unit}" if self.cost_instance is not None else "N/A",
                    "cost_total": f"${self.cost_total:.4f}{cost_unit}" if self.cost_total is not None else "N/A",
                    "transfer_runtime_s": f"{self.transfer_runtime_s:.2f}{runtime_unit}" if self.transfer_runtime_s is not None else "N/A (stream)",
                },
                "baseline": {
                    "throughput_achieved_gbits": self.baseline_throughput_achieved_gbits,
                    "cost_egress": self.baseline_cost_egress,
                    "cost_instance": self.baseline_cost_instance,
                    "cost_total": self.baseline_cost_total,
                },
                 "extra_data": self.extra_data # Include extra data like cost rates
            }
        else:
            return {"is_feasible": self.is_feasible, "extra_data": self.extra_data}


class ThroughputSolver:
    # <<< MODIFIED __init__ >>>
    def __init__(self, df_path, default_throughput=0.0, cost_df_path=None, latency_df_path=None):
        print(f"[DEBUG Solver Init] Throughput df_path = '{df_path}'", flush=True)
        logger.fs.debug(f"[DEBUG Solver Init] Throughput df_path = '{df_path}'")
        self.default_throughput = default_throughput
        self.df = None
        self.cost_df = None
        self.latency_df = None

        # Load Throughput Data (Required)
        try:
            self.df = pd.read_csv(df_path).set_index(["src_region", "dst_region", "src_tier", "dst_tier"]).sort_index()
            print(f"[DEBUG Solver Init] Loaded throughput CSV from '{df_path}'. Shape: {self.df.shape}", flush=True)
            logger.fs.debug(f"[DEBUG Solver Init] Loaded throughput CSV from '{df_path}'. Shape: {self.df.shape}")
        except FileNotFoundError:
            print(f"[DEBUG ERROR] FileNotFoundError: Throughput CSV '{df_path}'!", flush=True)
            logger.fs.error(f"[DEBUG ERROR] FileNotFoundError: Throughput CSV '{df_path}'!")
            raise
        except Exception as e:
            print(f"[DEBUG ERROR] Failed loading throughput CSV '{df_path}': {e}", flush=True)
            logger.fs.error(f"[DEBUG ERROR] Failed loading throughput CSV '{df_path}': {e}")
            raise

        # Load Custom Cost Data (Optional)
        if cost_df_path:
            print(f"[DEBUG Solver Init] Custom cost_df_path provided = '{cost_df_path}'", flush=True)
            logger.fs.debug(f"[DEBUG Solver Init] Custom cost_df_path provided = '{cost_df_path}'")
            try:
                # Assuming structure: src,dst,cost ($/GB) - matching aws_transfer_costs structure for simplicity
                self.cost_df = pd.read_csv(cost_df_path).set_index(["src", "dst"])
                print(f"[DEBUG Solver Init] Loaded custom cost CSV from '{cost_df_path}'. Shape: {self.cost_df.shape}", flush=True)
                logger.fs.debug(f"[DEBUG Solver Init] Loaded custom cost CSV from '{cost_df_path}'. Shape: {self.cost_df.shape}")
            except Exception as e:
                print(f"[DEBUG WARNING] Failed loading custom cost CSV '{cost_df_path}', will use default lookup: {e}", flush=True)
                logger.fs.warning(f"[DEBUG WARNING] Failed loading custom cost CSV '{cost_df_path}', will use default lookup: {e}")
                self.cost_df = None

        # Load Custom Latency Data (Optional, but needed for latency constraint)
        if latency_df_path:
            print(f"[DEBUG Solver Init] Custom latency_df_path provided = '{latency_df_path}'", flush=True)
            logger.fs.debug(f"[DEBUG Solver Init] Custom latency_df_path provided = '{latency_df_path}'")
            try:
                # Assuming structure: src_region,dst_region,latency_rtt_ms
                self.latency_df = pd.read_csv(latency_df_path).set_index(["src_region", "dst_region"])
                print(f"[DEBUG Solver Init] Loaded custom latency CSV from '{latency_df_path}'. Shape: {self.latency_df.shape}", flush=True)
                logger.fs.debug(f"[DEBUG Solver Init] Loaded custom latency CSV from '{latency_df_path}'. Shape: {self.latency_df.shape}")
            except Exception as e:
                print(f"[DEBUG WARNING] Failed loading custom latency CSV '{latency_df_path}': {e}", flush=True)
                logger.fs.warning(f"[DEBUG WARNING] Failed loading custom latency CSV '{latency_df_path}': {e}")
                self.latency_df = None

    @functools.lru_cache(maxsize=None)
    def get_path_throughput(self, src_region_tag, dst_region_tag, src_tier="PREMIUM", dst_tier="PREMIUM"):
        # Added type hints and check for self.df
        if not hasattr(self, 'df') or self.df is None: return self.default_throughput
        if src_region_tag == dst_region_tag: return self.default_throughput
        # Use region tags directly for lookup in the throughput dataframe
        if (src_region_tag, dst_region_tag, src_tier, dst_tier) not in self.df.index: return None
        result = self.df.loc[(src_region_tag, dst_region_tag, src_tier, dst_tier), "throughput_sent"]
        if pd.api.types.is_scalar(result): return result
        else: return result.values[0] if not result.empty else None

    @functools.lru_cache(maxsize=None)
    def get_path_cost(self, src_region_tag, dst_region_tag, src_tier="PREMIUM", dst_tier="PREMIUM"):
        assert src_tier == "PREMIUM" and dst_tier == "PREMIUM"
        cost = None
        # <<< TRY CUSTOM COST DF FIRST >>>
        if hasattr(self, 'cost_df') and self.cost_df is not None:
            try:
                # Logic assumes cost_df has 'src', 'dst' columns matching AWS CSV format
                src_provider, src = src_region_tag.split(":", 1)
                dst_provider, dst = dst_region_tag.split(":", 1)
                lookup_dst = "internet" if src_provider != dst_provider else dst
                if (src, lookup_dst) in self.cost_df.index:
                     cost_val = self.cost_df.loc[(src, lookup_dst), "cost"] # Access column by name
                     cost = cost_val.iloc[0] if isinstance(cost_val, pd.Series) else cost_val
                # else: print(f"Path {src}->{lookup_dst} not in custom cost df") # Debug
            except Exception as e:
                 print(f"[DEBUG WARNING] Error looking up cost in custom CSV: {e}. Trying default.", flush=True)
                 logger.fs.warning(f"[DEBUG WARNING] Error looking up cost in custom CSV: {e}. Trying default.")

        if cost is None:
            print(f"[DEBUG WARNING] get_transfer_cost returned None for {src_region_tag} -> {dst_region_tag}", flush=True)
            logger.fs.warning(f"[DEBUG WARNING] get_transfer_cost returned None for {src_region_tag} -> {dst_region_tag}")
            return float('inf')
        return cost

    # <<< ADDED get_path_latency >>>
    @functools.lru_cache(maxsize=None)
    def get_path_latency(self, src_region_tag, dst_region_tag):
        """Gets latency from the loaded latency_df."""
        if not hasattr(self, 'latency_df') or self.latency_df is None:
            print(f"[DEBUG WARNING] Latency data not loaded, cannot get latency.", flush=True)
            logger.fs.warning(f"[DEBUG WARNING] Latency data not loaded.")
            return float('inf')

        if src_region_tag == dst_region_tag: return 1.0

        if (src_region_tag, dst_region_tag) in self.latency_df.index:
            result = self.latency_df.loc[(src_region_tag, dst_region_tag), "latency_rtt_ms"]
            if pd.api.types.is_scalar(result): return result
            else: return result.values[0] if not result.empty else float('inf')
        else:
            # print(f"[DEBUG WARNING] Latency path not found {src_region_tag} -> {dst_region_tag}", flush=True)
            return float('inf')

    def get_regions(self):
        if not hasattr(self, 'df') or self.df is None:
            raise ValueError("ThroughputSolver cannot determine regions without loaded throughput profile data.")
        regions = set(list(self.df.index.get_level_values(0).unique()) + list(self.df.index.get_level_values(1).unique()))
        # Add regions from cost/latency DFs if they were loaded
        if hasattr(self, 'cost_df') and self.cost_df is not None:
             # Assumes cost_df index levels 0,1 are src, dst region names (not provider:region)
             # This might need adjustment based on actual custom cost CSV format
             # regions.update(list(self.cost_df.index.get_level_values(0).unique()))
             # regions.update(list(self.cost_df.index.get_level_values(1).unique()))
             pass # Skip adding from cost for now, assume throughput covers all needed regions
        if hasattr(self, 'latency_df') and self.latency_df is not None:
             regions.update(list(self.latency_df.index.get_level_values(0).unique()))
             regions.update(list(self.latency_df.index.get_level_values(1).unique()))
        return list(sorted(list(regions)))

    def get_throughput_grid(self):
        regions = self.get_regions()
        data_grid = np.zeros((len(regions), len(regions)))
        print(f"[DEBUG Solver] Generating throughput grid for regions: {regions}", flush=True)
        for i, src in enumerate(regions):
            for j, dst in enumerate(regions):
                # Use PREMIUM tier by default as per original code/solver structure
                throughput_value = self.get_path_throughput(src, dst, src_tier="PREMIUM", dst_tier="PREMIUM")
                data_grid[i, j] = throughput_value if throughput_value is not None else 0
        #data_grid = data_grid / GB # Convert Bits/s to Gbit/s
        print(f"[DEBUG Solver] Generated throughput grid (Gbps, shape {data_grid.shape})", flush=True)
        return data_grid.round(4)

    def get_cost_grid(self):
        regions = self.get_regions()
        data_grid = np.zeros((len(regions), len(regions)))
        print(f"[DEBUG Solver] Generating cost grid for regions: {regions}", flush=True)
        for i, src in enumerate(regions):
            for j, dst in enumerate(regions):
                cost = self.get_path_cost(src, dst) # Will use custom df if loaded, else default
                assert cost is not None and cost >= 0, f"Cost for {src} -> {dst} is invalid: {cost}"
                data_grid[i, j] = cost
        print(f"[DEBUG Solver] Generated cost grid ($/GB, shape {data_grid.shape})", flush=True)
        return data_grid.round(4)

    # <<< ADDED get_latency_grid >>>
    def get_latency_grid(self):
        """Generates the latency grid (ms RTT) based on loaded latency_df."""
        if not hasattr(self, 'latency_df') or self.latency_df is None:
             print("[DEBUG ERROR] Latency data not loaded, cannot generate latency grid.", flush=True)
             logger.fs.error("[DEBUG ERROR] Latency data not loaded, cannot generate latency grid.")
             raise ValueError("Latency profile data (latency_df) was not loaded.")

        regions = self.get_regions()
        data_grid = np.full((len(regions), len(regions)), float('inf')) # Default to infinite latency
        print(f"[DEBUG Solver] Generating latency grid for regions: {regions}", flush=True)
        logger.fs.debug(f"[DEBUG Solver] Generating latency grid for regions: {regions}")

        for i, src in enumerate(regions):
            for j, dst in enumerate(regions):
                latency_value = self.get_path_latency(src, dst)
                data_grid[i, j] = latency_value if latency_value is not None and latency_value >= 0 else float('inf')

        print(f"[DEBUG Solver] Generated latency grid (ms RTT, shape {data_grid.shape})", flush=True)
        logger.fs.debug(f"[DEBUG Solver] Generated latency grid (ms RTT, shape {data_grid.shape})")
        return data_grid.round(2)

    # Modified baseline to handle potential missing problem fields for streams
    def get_baseline_throughput_and_cost(self, p: ThroughputProblem) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        src, dst = p.src, p.dst
        path_throughput_bps = self.get_path_throughput(src, dst)
        if path_throughput_bps is None or path_throughput_bps <= 0: return 0.0, None, None

        # Determine effective throughput for baseline calculation
        effective_required_throughput_gbits = None
        if p.required_throughput_gbits is not None and p.required_throughput_gbits > 0:
            effective_required_throughput_gbits = p.required_throughput_gbits
        elif p.min_stream_throughput_gbps is not None and p.min_stream_throughput_gbps > 0:
            effective_required_throughput_gbits = p.min_stream_throughput_gbps

        throughput_gbps = max(p.instance_limit * path_throughput_bps / GB, 1e-6)

        # Determine GB for cost calculation
        gb_for_cost = p.gbyte_to_transfer
        if gb_for_cost is None and effective_required_throughput_gbits is not None and effective_required_throughput_gbits > 0:
             gb_for_cost = (effective_required_throughput_gbits * 3600) / GBIT_PER_GBYTE # 1hr equivalent
        elif gb_for_cost is None:
             print("[DEBUG WARNING] Cannot calculate baseline cost without gbyte_to_transfer or effective throughput", flush=True)
             return throughput_gbps, None, None

        # Calculate costs
        transfer_s = gb_for_cost * GBIT_PER_GBYTE / throughput_gbps if throughput_gbps > 0 else float('inf')
        instance_cost = p.cost_per_instance_hr * p.instance_limit * transfer_s / 3600 if transfer_s != float('inf') else float('inf')
        path_cost_per_gb = self.get_path_cost(src, dst)
        egress_cost = gb_for_cost * path_cost_per_gb if path_cost_per_gb is not None else None

        return throughput_gbps, egress_cost, instance_cost

    # ... plotting and graphviz methods remain the same ...
    # ... to_replication_topology method remains the same ...

# --- END OF MODIFIED solver.py ---