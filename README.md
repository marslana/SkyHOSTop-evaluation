# SkyHOSTop Evaluation Artifact

This repository contains the evaluation code, measurement data, and results for the paper:

> **SkyHOSTop: Cost-Aware Multi-Cloud Streaming Overlay Optimization**

## Prerequisites

- **Python 3.10 or 3.11** is required. Python 3.12+ is currently not compatible due to dependency constraints.
- A virtual environment is recommended.

## Repository Structure

```
SkyHOSTop-evaluation/
├── data/                              # Multi-cloud measurement matrices 
│   ├── mc_throughput_real.csv          # Per-VM throughput (Gbps), measured via iperf3
│   ├── mc_latency_real.csv            # Inter-region RTT (ms), measured via ping
│   └── mc_cost.csv                    # Egress cost ($/GB) from provider pricing
├── solver/                            # SkyHOSTop MILP solver (standalone)
│   ├── solver.py                      # Base solver classes (ThroughputProblem, ThroughputSolution)
│   └── solver_ilp.py                  # SkyHOSTop MILP formulation (SCIP-based)
├── scripts/                           # Evaluation and plotting scripts
│   ├── eval_baselines.py              # Main evaluation: MILP + 3 baselines across parameter sweep
│   ├── batch_ablation.py              # Routing-vs-batch-size (MILP/XRON at fixed Sb)
│   ├── scalability_sweep.py           # MILP solve time vs topology size (18-50 regions)
│   ├── hop_comparison.py              # Effect of max relay hops (Hmax = 1, 2, 3)
│   ├── uniform_drop.py                # Robustness to uniform throughput degradation
│   ├── plot_impact.py                 # Generates Fig 1 (feasibility) and Fig 2 (MILP vs XRON)
│   └── plot_scalability_v.py          # Generates scalability figure (solve time vs |V|)
├── results/                           # Pre-computed evaluation results
│   ├── mc_eval_results.csv            # Main eval (1,440 scenarios x 4 baselines = 5,760 rows)
│   ├── batch_ablation.csv             # Routing vs batch-size 
│   ├── scalability.csv                # Solve times for |V| in {18, 24, 30, 36, 42, 50}
│   ├── hop_comparison.csv             # Hmax sweep on a small probe set
│   ├── hop_comparison_full.csv        # Hmax sweep on the full parameter grid
│   ├── hop_comparison_hard.csv        # Hmax sweep on the stress-test (hard) subset
│   └── uniform_drop.csv               # Cost change under 10% / 20% throughput degradation
├── figures/                           # Paper figures
│   ├── fig1_feasibility_vs_instances.{pdf,png}   # Feasibility (%) vs N_max
│   ├── fig2_milp_vs_xron.{pdf,png}              # MILP vs XRON cost by N_max and rate
│   └── fig_scalability.pdf                       # Solve time vs |V|
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

## Parameter Sweep (Main Evaluation)

| Parameter | Values |
|-----------|--------|
| Stream rate (λ) | {1, 2, 3, 5, 8, 10, 12, 15} Gbps |
| Latency budget | {100, 200, 300, 500, 1000} ms |
| Instance limit (N_max) | {1, 2, 4} per region |
| Source-destination pairs | 12 cross-cloud pairs (4 per provider combination) |
| **Total scenarios** | **1,440 per baseline (5,760 rows total)** |

The 12 source-destination pairs cover all three cross-cloud combinations (AWS↔Azure, AWS↔GCP, GCP↔Azure) with four intercontinental pairs per combination spanning US↔Europe, US↔Asia, US↔South America, and Europe↔Asia. All 18 regions remain available as relay candidates for every pair.

## Quick Start: Regenerate Figures

To regenerate the paper figures from the provided results (no MILP solver needed):

```bash
pip install matplotlib pandas numpy

python scripts/plot_impact.py --csv results/mc_eval_results.csv --outdir figures/

python scripts/plot_scalability_v.py --input results/scalability.csv --output figures/fig_scalability.pdf
```

This produces:
- `fig1_feasibility_vs_instances.pdf` — Feasibility (%) vs instance limit N_max
- `fig2_milp_vs_xron.pdf` — MILP vs XRON cost comparison by N_max and stream rate
- `fig_scalability.pdf` — MILP solve time vs topology size |V| at Hmax = 2

## Full Reproduction: Re-run Evaluation

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

The MILP solver requires [SCIP](https://www.scipopt.org/) (version 9.0 used in the paper) via the `pyscipopt` Python interface.

### 2. Run the main evaluation

```bash
python scripts/eval_baselines.py \
    --throughput data/mc_throughput_real.csv \
    --latency data/mc_latency_real.csv \
    --cost data/mc_cost.csv \
    --pairs "aws:ap-northeast-1:azure:westeurope,aws:eu-west-1:gcp:asia-southeast1,aws:us-east-1:azure:brazilsouth,aws:us-east-1:gcp:europe-west1,azure:eastus:aws:eu-west-1,azure:japaneast:aws:us-west-2,azure:japaneast:gcp:us-west1,azure:westeurope:gcp:southamerica-east1,gcp:asia-northeast1:aws:us-west-2,gcp:asia-northeast1:azure:eastus,gcp:europe-west1:azure:southeastasia,gcp:us-east1:aws:sa-east-1" \
    --instance-limits "1,2,4" \
    --lambdas "1,2,3,5,8,10,12,15" \
    --budgets "100,200,300,500,1000" \
    --output results/mc_eval_results.csv
```

This takes approximately 30-45 minutes on an Apple M1 Pro.

### 3. (Optional) Re-run secondary experiments

The pre-computed CSVs in `results/` cover the full settings reported in the paper. To re-run:

```bash
# Routing-vs-batch ablation (Table IV in the paper)
# Uses the same 12 pairs and parameter grid as the main eval (~30-45 min)
python scripts/batch_ablation.py --output results/batch_ablation.csv

# Scalability sweep (Fig 6 in the paper)
# Hmax=2, 15 random source-destination probes per topology size,
# synthetic topologies for |V| > 18 (~10-15 min)
python scripts/scalability_sweep.py \
    --sizes "18,24,30,36,42,50" \
    --hops "2" \
    --probes-per-size 15 \
    --output results/scalability.csv

# Hmax sweep (effect of max relay hops on cost / feasibility / solve time)
# The pre-computed `hop_comparison_hard.csv` is the stress-test subset cited in
# the paper. Re-run with default args to regenerate `hop_comparison.csv`.
python scripts/hop_comparison.py --output results/hop_comparison.csv

# Robustness to uniform throughput degradation (Section V-D)
# Note: the pre-computed `uniform_drop.csv` covers all 306 directed cross-region
# pairs at lambda=8 Gbps, budget 500 ms, N_max=2. The default --pairs uses 6
# representative pairs for a faster run.
python scripts/uniform_drop.py --output results/uniform_drop.csv
```

### 4. Regenerate figures from new results

```bash
python scripts/plot_impact.py --csv results/mc_eval_results.csv --outdir figures/
python scripts/plot_scalability_v.py --input results/scalability.csv --output figures/fig_scalability.pdf
```

## Key Results Summary (12-pair main evaluation)

### Feasibility (%) by per-region VM limit (N_max)

| Method | N_max=1 | N_max=2 | N_max=4 | Aggregate |
|--------|---------|---------|---------|-----------|
| **SkyHOSTop (MILP)** | **56.9%** | **81.7%** | **93.1%** | **77.2%** |
| XRON heuristic       | 55.6%   | 81.7%   | 93.1%   | 76.8%     |
| Single-path overlay  | 26.9%   | 42.3%   | 70.2%   | 46.5%     |
| Direct transfer      |  8.1%   | 19.4%   | 31.7%   | 19.7%     |

### Cost saving — SkyHOSTop vs XRON (1,106 mutually feasible scenarios)

| Metric | Value |
|--------|-------|
| Aggregate cost saving (cost-weighted) | **18.6%** |
| Per-N_max saving (1 / 2 / 4) | 17.3% / 18.5% / 19.2% |
| Per-λ saving (Gbps) range | 18.0% (λ=1) – 19.9% (λ=3) |
| Median per-scenario saving | 12.5% |
| Maximum per-scenario saving | 54.7% |

### Routing vs batch-size ablation (`batch_ablation.csv`)

| Both methods use Sb = | XRON ($/hr) | SkyHOSTop ($/hr) | Saving |
|-----------------------|-------------|------------------|--------|
| 1 MB (XRON default)   | 525,301     | 427,532          | 18.61% |
| Sb* (SkyHOSTop's optimal, mean 8.5 MB) | 522,084 | 427,532 | 18.11% |

This shows that the cost reduction comes primarily from **routing decisions** (relay selection, traffic splitting, VM placement), not from batch-size optimization.

### Scalability (Hmax = 2)

| Topology size \|V\| | Median candidate paths | Median solve time |
|---------------------|------------------------|-------------------|
| 18 (measured)       | 257                    | 1.0 s             |
| 30 (synthetic)      | 785                    | 3.1 s             |
| 42 (synthetic)      | 1,601                  | 6.8 s             |
| 50 (synthetic)      | 2,305                  | 10.9 s            |

Worst-case solve time at \|V\|=50 across 15 random probes was below 13 s.

### Robustness to uniform throughput degradation

| Drop level | Scenarios losing feasibility | Median cost change | Mean cost change |
|------------|------------------------------|--------------------|------------------|
| 10%        | 0 / 306                       | +2.0%              | +4.0%            |
| 20%        | 0 / 306                       | +4.8%              | +9.5%            |

## Measurement Methodology

- **Throughput**: iperf3 with 16 parallel TCP streams and TCP buffer tuning on AWS m5.2xlarge, GCP n2-standard-8, and Azure Standard_D8s_v3 instances. Each pair was measured twice and averaged.
- **Latency**: ICMP ping (10 probes, averaged) between all region pairs.
- **Cost**: Published provider egress pricing as of measurement date (AWS EC2, GCP VPC, Azure Bandwidth).
- **Processing coefficient (α)**: 4.0 ms/MB, measured by benchmarking the gateway serialization and compression pipeline on AWS m5.8xlarge VMs.

## License

This artifact is provided for research reproducibility under the LICENSE file in this repository. Please cite our paper if you use this code or data.
