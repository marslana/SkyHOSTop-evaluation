# SkyHOSTop Evaluation Artifact

This repository contains the evaluation code, measurement data, and results for the paper:

> **SkyHOSTop: Cost-Aware Multi-Cloud Streaming Overlay Optimization**

## Repository Structure

```
SkyHOSTop-evaluation/
├── data/                        # Multi-cloud measurement matrices
│   ├── mc_throughput_real.csv    # Per-VM throughput (Gbps), measured via iperf3
│   ├── mc_latency_real.csv      # Inter-region RTT (ms), measured via ping
│   └── mc_cost.csv              # Egress cost ($/GB) from provider pricing
├── solver/                      # SkyHOSTop MILP solver (from SkyHOST platform)
│   ├── solver.py                # Base solver classes (ThroughputProblem, ThroughputSolution)
│   └── solver_ilp.py            # SkyHOSTop MILP formulation (SCIP-based)
├── scripts/                     # Evaluation and plotting scripts
│   ├── eval_baselines.py        # Run MILP + baselines across parameter sweep
│   └── plot_impact.py           # Generate publication figures from results
├── results/                     # Pre-computed evaluation results
│   └── mc_eval_results.csv      # Full results (2880 COST-mode rows)
├── requirements.txt
└── README.md
```

## Multi-Cloud Topology

The evaluation uses 18 cloud regions across three providers:

| Provider | Regions |
|----------|---------|
| AWS      | us-east-1, us-west-2, eu-west-1, ap-northeast-1, ap-southeast-1, sa-east-1 |
| GCP      | us-east1, us-west1, europe-west1, asia-northeast1, southamerica-east1, asia-southeast1 |
| Azure    | eastus, westus2, westeurope, japaneast, southeastasia, brazilsouth |

## Parameter Sweep

| Parameter | Values |
|-----------|--------|
| Stream rate (λ) | {1, 2, 3, 5, 8, 10, 12, 15} Gbps |
| Latency budget | {100, 200, 300, 500, 1000} ms |
| Instance limit (N_max) | {1, 2, 4} per region |
| Source-destination pairs | 6 cross-cloud pairs (2 per provider combination) |
| **Total scenarios** | **720 per baseline** |

## Quick Start: Regenerate Figures

To regenerate the paper figures from the provided results (no MILP solver needed):

```bash
pip install matplotlib pandas numpy

python scripts/plot_impact.py \
    --csv results/mc_eval_results.csv \
    --outdir figures/
```

This produces:
- `fig1_feasibility_vs_instances.pdf` — Feasibility (%) vs instance limit
- `fig3_milp_vs_xron.pdf` — MILP vs XRON cost comparison by N_max

## Full Reproduction: Re-run Evaluation

To re-run the MILP and baseline evaluation from scratch:

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

The MILP solver requires [SCIP](https://www.scipopt.org/) (version 9.0 used in the paper) via the `pyscipopt` Python interface.

### 2. Install Skyplane and SkyHOSTop solver

The evaluation scripts build on the [Skyplane](https://github.com/skyplane-project/skyplane) data transfer framework. Install Skyplane and then copy the SkyHOSTop solver files (provided in this repository) into the Skyplane installation:

```bash
pip install "skyplane[aws,azure,gcp]"

# Copy the SkyHOSTop MILP solver into the Skyplane planner module
SKYPLANE_PATH=$(python -c "import skyplane; import os; print(os.path.dirname(skyplane.__file__))")
cp solver/solver.py "$SKYPLANE_PATH/planner/solver.py"
cp solver/solver_ilp.py "$SKYPLANE_PATH/planner/solver_ilp.py"
```

The MILP solver implementation is in `solver/solver_ilp.py` and the base solver classes are in `solver/solver.py`. These files extend Skyplane's solver with the SkyHOSTop streaming MILP formulation described in the paper.

### 3. Run the evaluation

```bash
python scripts/eval_baselines.py \
    --throughput data/mc_throughput_real.csv \
    --latency data/mc_latency_real.csv \
    --cost data/mc_cost.csv \
    --pairs "aws:us-east-1:azure:brazilsouth,aws:us-east-1:gcp:europe-west1,gcp:asia-northeast1:azure:eastus,azure:eastus:aws:eu-west-1,gcp:us-east1:aws:sa-east-1,azure:japaneast:gcp:us-west1" \
    --instance-limits "1,2,4" \
    --lambdas "1,2,3,5,8,10,12,15" \
    --budgets "100,200,300,500,1000" \
    --output results/mc_eval_results.csv
```

This takes approximately 20-30 minutes on an Apple M1 Pro.

## Key Results Summary

| Baseline | Feasibility | Avg Cost Saving vs XRON |
|----------|-------------|------------------------|
| SkyHOSTop (MILP) | 82.6% | 14.2% |
| XRON heuristic | 82.5% | — |
| Single-path overlay | 55.1% | — |
| Direct transfer | 30.1% | — |

## Measurement Methodology

- **Throughput**: iperf3 with 16 parallel TCP streams, 10-second tests, TCP buffer tuning (wmem/rmem set to 4MB-16MB) on AWS m5.2xlarge, GCP n2-standard-8, Azure Standard_D8s_v3 instances.
- **Latency**: ICMP ping (10 probes, averaged) between all region pairs.
- **Cost**: Published provider egress pricing as of measurement date.

## License

This artifact is provided for research reproducibility. Please cite our paper if you use this code or data.
