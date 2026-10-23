#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: $0 [--check] KERNEL_WORKSPACE"
    echo "Workspace: common-control-clean/, clang19/, rust/, build-tools/"
    echo "JOBS defaults to 16. Existing output directories are never reused."
}
check_only=0
if [[ "${1:-}" == "--help" ]]; then usage; exit 0; fi
if [[ "${1:-}" == "--check" ]]; then check_only=1; shift; fi
if [[ $# -ne 1 ]]; then usage >&2; exit 2; fi

fail() { echo "ERROR: $*" >&2; exit 1; }
[[ "$(uname -s)" == Linux ]] || fail "Use an Ubuntu Linux host."
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
export TASK_TOOLROOT="$(cd -- "$1" && pwd -P)"
export TASK_SRC="$TASK_TOOLROOT/common-control-clean"
JOBS="${JOBS:-16}"
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || fail "JOBS must be a positive integer."
[[ -f "$TASK_SRC/Makefile" ]] || fail "Kernel source not found."
[[ "$(git -C "$TASK_SRC" rev-parse HEAD)" == 1fdec1073c8fa24282dcbe8395593759c2ce0d17 ]] ||
    fail "Use the source revision listed in build_info.json."
git -C "$TASK_SRC" diff --quiet || fail "Tracked source files have local changes."
git -C "$TASK_SRC" diff --cached --quiet || fail "Source has staged changes."
[[ ! -e "$TASK_SRC/out-control-rustfix" && ! -L "$TASK_SRC/out-control-rustfix" ]] ||
    fail "out-control-rustfix already exists; use a separate clean workspace."
[[ -f "$SCRIPT_DIR/kernel.config" ]] || fail "The reference kernel.config is missing."

# These settings and both make commands come from the successful control build.
export PATH="$TASK_TOOLROOT/clang19/bin:$TASK_TOOLROOT/build-tools/bin:$TASK_TOOLROOT/rust/bin:$PATH"
export CC="$TASK_TOOLROOT/clang19/bin/clang"
export HOSTCC="$TASK_TOOLROOT/clang19/bin/clang"
export RUSTC=rustc
export BINDGEN=bindgen
export LIBCLANG_PATH="$TASK_TOOLROOT/clang19/lib"
export LLVM=1
export LLVM_IAS=1
export ARCH=arm64
export SUBARCH=arm64
export CROSS_COMPILE=aarch64-linux-gnu-
export LD=ld.lld
export HOSTLD=ld.lld
export AR=llvm-ar
export NM=llvm-nm
export AS=clang
export READELF=llvm-readelf
export OBJCOPY=llvm-objcopy
export OBJDUMP=llvm-objdump
export OBJSIZE=llvm-size
export STRIP=llvm-strip
export KBUILD_BUILD_USER=build-user
export KBUILD_BUILD_HOST=build-host
export KBUILD_BUILD_VERSION=1
export KBUILD_BUILD_TIMESTAMP="Wed Jun  3 06:46:40 UTC 2026"
export KBUILD_GENDWARFKSYMS_STABLE=1
export RUSTC_FORCE_RUSTC_VERSION=rustc-android
export KCFLAGS="-no-canonical-prefixes -O2 -pipe -Wno-error -fno-stack-protector -D__ANDROID_COMMON_KERNEL__ -fdebug-prefix-map=$TASK_TOOLROOT=. -fmacro-prefix-map=$TASK_TOOLROOT=. -ffile-prefix-map=$TASK_TOOLROOT=."

# Use the configuration and output directory selected above.
unset KCONFIG_CONFIG KCONFIG_ALLCONFIG KBUILD_OUTPUT KBUILD_EXTMOD KBUILD_SRC
[[ -x "$CC" && -d "$TASK_TOOLROOT/rust/bin" && -d "$TASK_TOOLROOT/build-tools/bin" ]] ||
    fail "Extract the three toolchain directories first."
for tool in git make clang ld.lld rustc bindgen pahole llvm-ar llvm-nm llvm-objcopy llvm-readelf; do
    command -v "$tool" >/dev/null || fail "Missing build tool: $tool"
done
clang_version="$(clang --version)"
rust_version="$(rustc --version)"
lld_version="$(ld.lld --version)"
bindgen_version="$(bindgen --version)"
pahole_version="$(pahole --version)"
[[ "$clang_version" == *19.0.1* && "$clang_version" == *14043575* && "$clang_version" == *r536225* ]] ||
    fail "Expected Android Clang 19.0.1, r536225, build 14043575."
[[ "$lld_version" == *19.0.1* ]] || fail "Expected LLD 19.0.1."
[[ "$rust_version" == *1.82.0-dev* && "$rust_version" == *linux-12909517* ]] ||
    fail "Expected Android Rust linux-12909517 / 1.82.0-dev."
[[ "$bindgen_version" == "bindgen 0.69.5" ]] || fail "Expected bindgen 0.69.5."
[[ "$pahole_version" == "v1.25" ]] || fail "Expected pahole v1.25."
printf '%s\n' "$clang_version" "$lld_version" "$rust_version" "$bindgen_version" "$pahole_version"
if (( check_only )); then
    echo "Source and tool checks passed. No build was started."
    exit 0
fi

cd "$TASK_SRC"
mkdir out-control-rustfix
printf '%s\n' "$clang_version" "$lld_version" "$rust_version" "$bindgen_version" "$pahole_version" \
    > out-control-rustfix/toolchain_versions.txt
make -j"$JOBS" LLVM=1 ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- \
    CC="$CC" HOSTCC="$HOSTCC" LD=ld.lld HOSTLD=ld.lld \
    RUSTC=rustc OBJCOPY=llvm-objcopy O=out-control-rustfix gki_defconfig \
    2>&1 | tee out-control-rustfix/defconfig.log
cmp "$SCRIPT_DIR/kernel.config" out-control-rustfix/.config ||
    fail "Generated configuration differs from the successful control build."

make -j"$JOBS" LLVM=1 ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- \
    CC="$CC" HOSTCC="$HOSTCC" LD=ld.lld HOSTLD=ld.lld \
    RUSTC=rustc OBJCOPY=llvm-objcopy O=out-control-rustfix Image modules \
    2>&1 | tee out-control-rustfix/build.log
cmp "$SCRIPT_DIR/kernel.config" out-control-rustfix/.config ||
    fail "Configuration changed during the build."
for file in arch/arm64/boot/Image vmlinux Module.symvers vmlinux.symvers include/config/kernel.release; do
    [[ -s "out-control-rustfix/$file" ]] || fail "Missing build output: $file"
done
uts_crc="$(awk '$2 == "init_uts_ns" {print $1}' out-control-rustfix/Module.symvers)"
[[ "$uts_crc" == "0x16cbd34a" ]] || fail "init_uts_ns module-version CRC does not match."
grep -q 'CsdfZWD8DztAw_6kernel' out-control-rustfix/Module.symvers ||
    fail "Expected Android Rust kernel crate identifier was not generated."
if grep -q 'Cs2T9Dodn8vEz_6kernel' out-control-rustfix/Module.symvers; then
    fail "Unexpected Rust kernel crate identifier."
fi
echo "Build complete: $TASK_SRC/out-control-rustfix"
