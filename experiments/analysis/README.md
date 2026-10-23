# PerfDog metrics

Calculate WJ (/10min) and average power (mW) from PerfDog exports.

Run from the package root:

```sh
python -m pip install openpyxl==3.1.5
python experiments/analysis/perfdog_metrics.py ../results/run_inventory.json --output-dir ../results/metrics
```

Use a separate output directory for each device and workload condition.
`runs.csv` contains individual runs. `summary.csv` groups them by App and policy.

Direct XLSX input is also supported. See `python experiments/analysis/perfdog_metrics.py --help`.
