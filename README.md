# Pavo

Task-aware CPU scheduling for Android, built on Linux `sched_ext` and eBPF.

[Build and run](#build-and-run) · [Scheduler](scheduler/scx_pavo/README.md) · [Experiments](experiments/README.md) · [Baselines](baselines/README.md) · [Device setup](env/README.md)

Pavo uses task behavior and Android thread roles to guide time allocation,
CPU placement and CPU performance requests. Its Rust loader discovers CPU
topology and manages BPF attachment.

This repository contains the scheduler, baselines and experiment scripts,
along with setup guides for Pixel 6 and OnePlus 15.

## Build and run

Prepare the device using the [Pixel 6](env/pixel6.md) or
[OnePlus 15](env/oneplus15.md) guide, then set up [ADEB and the toolchain](env/adeb.md).
Pavo requires root access and a kernel with `sched_ext` and BTF support.

In the phone's ARM64 Debian/ADEB root shell, run from the repository root:

```bash
cd scheduler/scx_pavo
bash scripts/build.sh
./target/release/scx_pavo
```

Build separately on each phone. Run one `sched_ext` scheduler at a time.
Press Ctrl+C to stop Pavo. Build and runtime options
are in the [scheduler README](scheduler/scx_pavo/README.md).

## Run experiments

The automation runs on a Windows host with Python 3.11+, ADB and PerfDog.
From the repository root, install the Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

Prepare the [baselines](baselines/README.md) and [workloads](workloads/README.md).
Copy the runtime template for your device and fill in the executable paths
and [App/PerfDog settings](experiments/configs/README.md):

| Device | Template | Save the configured copy as |
|---|---|---|
| Pixel 6 | [runtime_config.example.json](baselines/runtime_config.example.json) | `pixel6.private.json` |
| OnePlus 15 | [runtime_config.oneplus15.example.json](baselines/runtime_config.oneplus15.example.json) | `oneplus15.private.json` |

Use the matching command with the connected device's serial number and a new
output directory:

```powershell
python experiments/pixel6/run_main.py --execute --serial DEVICE_SERIAL --runtime-config pixel6.private.json --output-root ../results/pixel6-001
python experiments/oneplus15/run_main.py --execute --serial DEVICE_SERIAL --runtime-config oneplus15.private.json --output-root ../results/oneplus15-001
```

The [experiment guide](experiments/README.md) covers App selection, policies
and collection. Pixel 6 also has [temperature and game entries](experiments/pixel6/README.md).

## Repository contents

| Directory | Contents |
|---|---|
| [scheduler/scx_pavo/](scheduler/scx_pavo/README.md) | BPF policy and Rust loader |
| [baselines/](baselines/README.md) | Native controls, scx_simple, scx_lavd, UCLAMP-GFX and zTT |
| [experiments/](experiments/README.md) | App automation, PerfDog integration, collectors and metric utilities |
| [workloads/](workloads/README.md) | stress-ng, FFmpeg, fio, dex2oat and MobileNet setup |
| [env/](env/README.md) | Device setup, root access, ADEB, toolchains and kernel builds |

## Licensing

Independently authored scripts and documentation use [MIT](LICENSE).
The scheduler uses [GPL-2.0-only](scheduler/scx_pavo/LICENSE).
Third-party components retain their own licenses and copyright notices.
