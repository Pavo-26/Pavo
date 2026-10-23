# scx_pavo

The Pavo BPF scheduling policy and Rust loader. The loader discovers CPU
topology and manages BPF attachment.

## Build and run

Prepare [ARM64 Debian/ADEB and the toolchain](../../env/adeb.md).
The kernel must expose `sched_ext` and BTF, with readable `/proc` and `/sys`.
In the phone's ADEB root shell, enter this directory and run:

~~~bash
bash scripts/build.sh
./target/release/scx_pavo
~~~

Build separately on each phone and run one sched_ext scheduler at a time.
Start Pavo without arguments and press Ctrl+C to stop.
Use the `schedutil` governor. The experiment runner sets it automatically.

Linux 6.12 uses the default build. For kernels exposing the newer sched_ext
interface names, use `bash scripts/build.sh --modern-kfuncs`.

## Automatic placement groups

Without `--cpumasks`, the loader uses cache/index3, then cache/index2, and
otherwise one group per NUMA node. Placement prefers eligible CPUs in the
previous CPU's group and falls back to the full eligible set if the group
has no eligible CPU. Explicit `--cpumasks` overrides these placement groups
without changing capacity classes or cpufreq domains.

## Source layout

| File | Role |
|---|---|
| [src/main.rs](src/main.rs) | Loader and BPF lifecycle |
| [src/topology.rs](src/topology.rs) | CPU capacity and topology |
| [src/slice.rs](src/slice.rs) | Slice base updates |
| [src/bpf/pavo.bpf.c](src/bpf/pavo.bpf.c) | sched_ext callbacks and queues |
| [src/bpf/pavo_policy.h](src/bpf/pavo_policy.h) | Scheduling policy |
| [include/pavo_compat.bpf.h](include/pavo_compat.bpf.h) | Kernel interface compatibility |

## License and acknowledgments

Licensed under [GPL-2.0-only](LICENSE). Includes code derived from
[sched-ext/scx v1.0.9](https://github.com/sched-ext/scx/tree/v1.0.9) and headers from
[libbpf](https://github.com/libbpf/libbpf). Copyright and SPDX notices are retained
in the source files. Third-party licenses are in [LICENSES/](LICENSES/).
