# Baselines

| Device | Policy keys | Implementation |
|---|---|---|
| Pixel 6 | android_default, performance | Android scheduler with schedutil / performance |
| Pixel 6 | scx_simple, scx_lavd | [scx v1.0.9](scx/README.md), lavd in Autopilot mode |
| Pixel 6 | uclamp_gfx_700 | Foreground thread UCLAMP floor |
| Pixel 6 | ztt | [zTT adaptation](ztt/README.md) |
| OnePlus 15 | stock, stock_schedutil | Vendor scheduler with walt / schedutil |

Fill the [Pixel](runtime_config.example.json) or
[OnePlus](runtime_config.oneplus15.example.json) runtime template and use
`--policies` with the [main runner](../experiments/README.md).
scx_simple and scx_lavd start without arguments.
Pavo is in [scheduler/scx_pavo](../scheduler/scx_pavo/README.md).
Set `environment.kernel_release` to the complete output of `adb -s SERIAL shell uname -r`.
The OnePlus template records the experiment kernel's release; the runner checks it exactly.

## Native controls

Stock uses the OnePlus vendor-default scheduler and `walt` governor on the
experimental kernel. Stock+`schedutil` uses the same kernel and vendor
environment with both CPU policies switched to `schedutil`.

[device_control.py](../experiments/device_control.py) switches governors
and restores the saved settings. Native conditions run with sched_ext disabled
and retain the system vendor/thermal services.

## UCLAMP-GFX

Build the daemon for Android ARM64 on a Linux host with the NDK installed:

~~~bash
"$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android35-clang" \
  -O2 -Wall -Wextra baselines/uclamp_gfx/uclamp_floor_daemon.c \
  -o /tmp/uclamp_floor_daemon_pavo_baseline
~~~

Deploy it to Android and set `binaries.uclamp_floor_daemon` in the runtime
configuration. `/system/bin/uclampset` is also required.
[uclamp_gfx.py](uclamp_gfx/uclamp_gfx.py) selects main/UI, RenderThread and
hwuiTask* threads. The daemon maintains their lower bounds.
The runner manages startup and restores the original clamps after each run.

See [component licensing](../README.md#licensing).
