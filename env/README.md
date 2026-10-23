# Environment setup

| | Pixel 6 | OnePlus 15 |
|---|---|---|
| System | AOSP Android 15, userdebug | PLK110_16.0.8.302(CN01), ColorOS 16 |
| Kernel | Google Android common 6.12 development branch | Linux 6.12.23 built from source based on OPPO's official open-source release |
| Root | Native adb root | Magisk 30.7 and root adbd |
| Build environment | ARM64 Debian 12.13 / ADEB | ARM64 Debian / ADEB |
| Scheduler toolchain | LLVM 19.1.7, Rust 1.88.0 | LLVM 19.1.7, Rust 1.88.0 |
| Capture | PerfDog 11.2.250408 | PerfDog 11.2.250408 |

Use [Platform Tools](https://developer.android.com/tools/releases/platform-tools)
on the connected host. Enable USB debugging and authorize the host.
Replace `YOUR_DEVICE_SERIAL` with the phone's serial number.

1. Prepare [Pixel 6](pixel6.md) or [OnePlus 15](oneplus15.md).
2. Install [ADEB and build tools](adeb.md).
3. Configure the [experiment scripts](../experiments/README.md).

System and kernel builds run on Ubuntu. Experiment automation runs on Windows.
Scheduler builds run inside the target phone's ADEB shell.
See [OnePlus kernel build](oneplus15-kernel/README.md) for source and build steps,
and [OnePlus setup](oneplus15.md) for firmware and root access.
