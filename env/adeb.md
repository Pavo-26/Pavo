# ADEB and ARM64 builds

Start with root ADB from [Pixel](pixel6.md) or [OnePlus](oneplus15.md) setup.
Use an existing ARM64 Debian 12.x environment if already installed. Pixel uses
Debian 12.13. Set `environment.debian_version` in the runtime configuration to
the output of `cat /etc/debian_version`.

## Install ADEB

On the Ubuntu host:

~~~bash
sudo apt-get update
sudo apt-get install -y git qemu-user-static binfmt-support debootstrap debian-archive-keyring
git clone --branch v0.99h https://github.com/joelagnel/adeb.git
cd adeb
~~~

For a new Debian 12 (bookworm) environment, set these values in `androdeb`:

~~~sh
DISTRO=bookworm; ARCH=arm64
~~~

In the kernel-header download condition in `androdeb`, replace
`[ ! -z "$FULL" ]` with `[ "$FULL" -eq 1 ]`.

Install only on a target without an existing `/data/androdeb` environment:

~~~sh
adb -s YOUR_DEVICE_SERIAL root
adb -s YOUR_DEVICE_SERIAL wait-for-device
adb -s YOUR_DEVICE_SERIAL shell 'test ! -e /data/androdeb'
~~~

Continue if the last command succeeds. If the directory exists, use that environment.

~~~bash
sudo update-binfmts --enable qemu-aarch64
./adeb -s YOUR_DEVICE_SERIAL prepare --build --arch arm64
./adeb -s YOUR_DEVICE_SERIAL shell
~~~

Inside ADEB, check the architecture and mounts. `/data`, `/proc` and `/sys`
should expose the phone's real filesystem and processes.

~~~sh
cat /etc/debian_version
dpkg --print-architecture
test -r /proc/1/stat
test -r /sys/kernel/btf/vmlinux
ls /data/misc
~~~

## Build dependencies

Run inside the phone's ADEB shell:

~~~bash
apt-get update
apt-get install -y build-essential git curl wget unzip cmake pkg-config \
  libdw-dev libelf-dev zlib1g-dev libzstd-dev libclang-dev \
  llvm bash-completion file
mkdir -p /data/misc/scx
~~~

### LLVM

Obtain [LLVM 19.1.7 for Linux/AArch64](https://github.com/llvm/llvm-project/releases/tag/llvmorg-19.1.7).
Extract it as `llvm-19.1.7/`, with `bin/` and `lib/` directly inside.
From the host:

~~~sh
adb -s YOUR_DEVICE_SERIAL push llvm-19.1.7 /data/misc/scx/
~~~

In ADEB, set these variables before building on either phone:

~~~bash
export CLANG_ROOT=/data/misc/scx/llvm-19.1.7
export PATH="$CLANG_ROOT/bin:$PATH"
export BPF_CLANG="$CLANG_ROOT/bin/clang"
export LIBCLANG_PATH="$CLANG_ROOT/lib"
export CC="$CLANG_ROOT/bin/clang"
export CXX="$CLANG_ROOT/bin/clang++"
~~~

### pahole and Rust

~~~bash
cd /data/misc/scx
git clone --branch v1.25 https://git.kernel.org/pub/scm/devel/pahole/pahole.git pahole-1.25
cmake -S pahole-1.25 -B pahole-1.25/build -D__LIB=lib
cmake --build pahole-1.25/build
install -m 755 pahole-1.25/build/pahole /usr/local/bin/pahole
~~~

Install rustup if absent, then Rust 1.88.0:

~~~bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"
rustup toolchain install 1.88.0 --component rustfmt
~~~

### libbpf and bpftool

For scx_simple, build libbpf and bpftool in ADEB:

~~~bash
cd /data/misc/scx
export SCX_TOOLS=/data/misc/scx/build-tools
git clone --depth 1 --branch v1.4.6 https://github.com/libbpf/libbpf.git libbpf-1.4.6
make -C libbpf-1.4.6/src -j2 BUILD_STATIC_ONLY=1 \
  PREFIX="$SCX_TOOLS" LIBDIR="$SCX_TOOLS/lib" install
git clone --depth 1 --branch v7.4.0 --recurse-submodules \
  https://github.com/libbpf/bpftool.git bpftool-7.4.0
make -C bpftool-7.4.0/src -j2
install -D -m 755 bpftool-7.4.0/src/bpftool "$SCX_TOOLS/bin/bpftool"
export PATH="$SCX_TOOLS/bin:$PATH"
export PKG_CONFIG_PATH="$SCX_TOOLS/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
~~~

Keep these variables set when running the [baseline build](../baselines/scx/README.md).

## Build the scheduler

Inside the target phone's ADEB shell, enter the source directory:

~~~bash
cd /path/to/scx_pavo
source "$HOME/.cargo/env"
cargo +1.88.0 build --release
~~~

The executable is `target/release/scx_pavo`. Build separately on each phone.
See [scheduler usage](../scheduler/scx_pavo/README.md).
