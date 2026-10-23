// SPDX-License-Identifier: GPL-2.0-only
fn main() {
    println!("cargo:rerun-if-changed=src/bpf");
    println!("cargo:rerun-if-changed=include");
    println!("cargo:rerun-if-env-changed=BPF_CLANG");
    #[cfg(all(target_os = "linux", feature = "scheduler"))]
    {
        let out = std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
        let mut args = vec![
            "-Iinclude",
            "-Isrc/bpf",
            "-D__TARGET_ARCH_arm64",
            "-Wall",
            "-Werror",
        ];
        if cfg!(feature = "modern-kfuncs") {
            args.push("-DPAVO_MODERN_KFUNCS=1");
        }
        let mut builder = libbpf_cargo::SkeletonBuilder::new();
        builder.source("src/bpf/pavo.bpf.c").clang_args(args);
        if let Ok(clang) = std::env::var("BPF_CLANG") {
            builder.clang(clang);
        }
        builder
            .build_and_generate(out.join("pavo.skel.rs"))
            .expect("build Pavo BPF skeleton");
    }
}
