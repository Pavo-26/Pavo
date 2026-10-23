# scx_simple and scx_lavd

Sources are based on sched-ext/scx v1.0.9, with kernel compatibility changes
to LAVD lock tracing and sched_ext kfunc interfaces.
[Upstream licensing](source/LICENSE) and source notices are retained.

Prepare [ARM64 build dependencies](../../env/adeb.md), including libbpf and bpftool.
From the package root in ADEB, use a new output directory:

~~~bash
python baselines/scx/build.py --out /tmp/pavo-baselines-arm64 --execute
~~~

Add `--offline` if dependencies are cached. Outputs are under
`/tmp/pavo-baselines-arm64/bin/`. Set their paths in the
[runtime configuration](../runtime_config.example.json).
Run both schedulers without arguments. scx_simple uses weighted virtual-time.
scx_lavd runs without arguments, which enables its default Autopilot mode.

For the OnePlus 6.12 kernel, add `--legacy-kfuncs` to select its kfunc ABI.
