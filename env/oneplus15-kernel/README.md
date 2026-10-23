# OnePlus 15 control-kernel build

Build workflow on Ubuntu 24.04 for the experimental Linux 6.12.23 kernel.
Firmware and root-access setup are described in [OnePlus setup](../oneplus15.md).

## Source

The experiment uses the source tree below, based on OPPO's official
open-source release and pinned to the recorded commit.

~~~bash
mkdir oneplus15-kernel
cd oneplus15-kernel
git clone --branch oneplus/sm8850_v_16.0.0_oneplus_15 \
  https://github.com/cctv18/android_kernel_common_oneplus_sm8850.git common-control-clean
git -C common-control-clean checkout --detach 1fdec1073c8fa24282dcbe8395593759c2ce0d17
~~~

## Tools

Extract these archives into `clang19/`, `rust/` and `build-tools/` in the
same working directory, with executables directly under each `bin/`:

- [Android Clang 19 / r536225](https://github.com/cctv18/oneplus_sm8650_toolchain/releases/download/LLVM-Clang19-r536225/clang-r536225.zip)
- [Android Rust](https://github.com/cctv18/oneplus_sm8650_toolchain/releases/download/LLVM-Clang19-r536225/rust.zip)
- [Build tools](https://github.com/cctv18/oneplus_sm8650_toolchain/releases/download/LLVM-Clang19-r536225/build-tools.zip)

~~~bash
sudo apt-get update
sudo apt-get install -y build-essential bc bison flex libssl-dev libelf-dev \
  libncurses-dev dwarves python3 rsync cpio lz4 zstd unzip
~~~

## Build

[build_control.sh](build_control.sh) uses the included [configuration](kernel.config).
Source and toolchain details are in [build_info.json](build_info.json).

~~~bash
JOBS=16 bash /path/to/oneplus15-kernel/build_control.sh /path/to/kernel_workspace
~~~

Here `kernel_workspace` is the working directory prepared above.
Use a fresh `common-control-clean/out-control-rustfix/` directory. Outputs include
`arch/arm64/boot/Image`, modules, `.config`, `Module.symvers` and build logs.
The build uses `RUSTC_FORCE_RUSTC_VERSION=rustc-android` and
`KBUILD_GENDWARFKSYMS_STABLE=1`.

Kernel/module compatibility and boot-image parameters must match the target
firmware. Keep recovery images, try temporary boot before flashing, and leave
the bootloader unlocked while using modified images.
