"""Build the bundled scx baselines in a new directory on native Linux."""
from __future__ import annotations

import argparse
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "source"
HEADER = "vmlinux-v6.13-rc1-g353b2a7de2a3.h"


def prepare(out: Path, arch: str) -> Path:
    out = out.resolve()
    if out.exists() or out.is_relative_to(SOURCE.resolve()):
        raise ValueError("Choose a new output directory outside the bundled source")
    out.mkdir(parents=True, exist_ok=False)
    work = out / "source"
    shutil.copytree(SOURCE, work)
    includes = work / "scheds/include"
    for name in ("arm64", "x86"):
        base = includes / "arch" / name
        shutil.copy2(base / HEADER, base / "vmlinux.h")
    shutil.copy2(includes / "arch" / arch / HEADER, includes / "vmlinux.h")
    shutil.copytree(includes, work / "rust/scx_utils/bpf_h")
    (out / "bin").mkdir()
    return work


def verify_resolved_lock(upstream: Path, resolved: Path):
    """Pruning unused packages is allowed; adding/upgrading dependencies is not."""
    original = tomllib.loads(upstream.read_text())["package"]
    selected = tomllib.loads(resolved.read_text())["package"]
    def identity(package):
        return (package["name"], package["version"], package.get("source"))
    allowed = {identity(p) for p in original}
    unexpected = [identity(p) for p in selected if identity(p) not in allowed]
    if unexpected:
        raise RuntimeError("Focused resolution changed upstream pins: " + repr(unexpected))


def build(args, out: Path, work: Path):
    host_arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x86"}.get(platform.machine())
    if sys.platform != "linux" or host_arch != args.arch:
        raise RuntimeError("Use native Linux for the selected architecture; no implicit cross-compilation")
    env = dict(os.environ, BPF_CLANG=args.clang,
               CARGO_TARGET_DIR=str(out / "cargo-target"))
    bpf_flags = ["-DSCX_LEGACY_KFUNCS"] if args.legacy_kfuncs else []
    if bpf_flags:
        env["BPF_EXTRA_CFLAGS_POST_INCL"] = " ".join(filter(None, (
            env.get("BPF_EXTRA_CFLAGS_POST_INCL", ""), *bpf_flags)))

    def run(command, *, capture=False):
        result = subprocess.run(command, cwd=work, env=env, check=True,
                                capture_output=capture, text=capture)
        return result.stdout.strip() if capture else ""

    if args.scheduler in ("scx_lavd", "all"):
        offline = ["--offline"] if args.offline else []
        # Cargo prunes unrelated workspace packages in this COPY only.
        run(["cargo", "metadata", "--format-version", "1", *offline], capture=True)
        verify_resolved_lock(SOURCE / "Cargo.lock", work / "Cargo.lock")
        run(["cargo", "build", "--locked", "--release", "-p", "scx_lavd", *offline])
        shutil.copy2(out / "cargo-target/release/scx_lavd", out / "bin/scx_lavd")
    if args.scheduler in ("scx_simple", "all"):
        run(["pkg-config", "--atleast-version=1.4", "libbpf"])
        cflags = shlex.split(run(["pkg-config", "--cflags", "libbpf"], capture=True))
        libs = shlex.split(run(["pkg-config", "--libs", "libbpf"], capture=True))
        # Same native system-include discovery and BPF flags as upstream Meson.
        sysinc = subprocess.run([args.clang, "-v", "-E", "-"], input="",
                                capture_output=True, text=True, check=True).stderr
        native_includes = []
        collecting = False
        for line in sysinc.splitlines():
            if line == "#include <...> search starts here:":
                collecting = True
            elif line == "End of search list.":
                collecting = False
            elif collecting and line.strip().startswith("/"):
                native_includes += ["-idirafter", line.strip()]
        inc = work / "scheds/include"
        includes = ["-I", str(inc / "arch" / args.arch), "-I", str(inc),
                    "-I", str(inc / "bpf-compat")]
        obj = out / "scx_simple.bpf.o"
        run([args.clang, "-g", "-O2", "-Wall", "-Wno-compare-distinct-pointer-types",
             "-target", "bpf", "-mcpu=v3", "-m" + sys.byteorder + "-endian",
             "-D__TARGET_ARCH_" + args.arch, *bpf_flags, *includes, *cflags, *native_includes,
             "-c", str(work / "scheds/c/scx_simple.bpf.c"), "-o", str(obj)])
        run(["bash", str(work / "meson-scripts/bpftool_build_skel"), args.bpftool,
             str(obj), str(out / "scx_simple.bpf.skel.h"),
             str(out / "scx_simple.bpf.subskel.h")])
        run([args.cc, "-g", "-O2", "-I", str(inc), "-I", str(out), *cflags,
             str(work / "scheds/c/scx_simple.c"), "-o", str(out / "bin/scx_simple"),
             *libs, "-lelf", "-lz", "-lzstd", "-pthread"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--arch", choices=("arm64", "x86"), default="arm64")
    parser.add_argument("--scheduler", choices=("scx_simple", "scx_lavd", "all"), default="all")
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--legacy-kfuncs", action="store_true",
                        help="Use the sched_ext 6.12 kfunc names")
    parser.add_argument("--clang", default="clang")
    parser.add_argument("--cc", default="cc")
    parser.add_argument("--bpftool", default="bpftool")
    args = parser.parse_args(argv)
    out = args.out.resolve()
    work = prepare(out, args.arch)
    build(args, out, work)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
