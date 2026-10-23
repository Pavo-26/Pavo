# Collectors

With root ADB, run from the package root and select a new output directory:

~~~sh
python experiments/record_raw.py --source trace --execute \
  --serial DEVICE_SERIAL --output-dir /path/to/new-trace

python experiments/record_raw.py --source background --duration 60 --execute \
  --serial DEVICE_SERIAL --output-dir /path/to/new-background

python experiments/record_raw.py --source odpm --duration 60 --interval 1 --execute \
  --serial DEVICE_SERIAL --output-dir /path/to/new-odpm
~~~

## Options

- **Perfetto:** [scheduler.pbtxt](perfetto/configs/scheduler.pbtxt) captures
  scheduling, CPU frequency/idle and process events for 60 seconds.
  Use `--trace-config` for another configuration or `--duration` for 1–600 seconds.
- **Background:** defaults to 16 stress-ng matrixprod workers.
  `--workload-config` accepts a JSON `argv` list with an absolute executable
  path. See [workload preparation](../../workloads/README.md).
- **ODPM:** Pixel energy counters and cpufreq time-in-state, sampled at the
  requested interval plus read time.

The [custom Pixel collector](pixel6/scheduler_delay_collector.py) uses a
separate tracefs instance and requires matching kernel events. Mount tracefs
inside ADEB and use a `/data` path shared with ADB.
It saves compressed traces and per-CPU ftrace statistics.
