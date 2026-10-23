// SPDX-License-Identifier: GPL-2.0-only
use scx_pavo::slice::{pavo_slice_interval, pavo_slice_ns_from_us, PavoSliceConfig};
use scx_pavo::topology::{pavo_parse_hex_mask, PavoTopology};
use std::path::PathBuf;

fn main() {
    if let Err(error) = pavo_main() {
        eprintln!("scx_pavo: {error}");
        std::process::exit(1);
    }
}

fn pavo_main() -> Result<(), Box<dyn std::error::Error>> {
    let mut root = PathBuf::from("/sys/devices/system/cpu");
    let mut groups = Vec::new();
    let mut run = false;
    let mut inspect = false;
    let mut slice_config = PavoSliceConfig::default();
    let mut args = std::env::args().skip(1).peekable();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--run" => run = true,
            "--inspect" => inspect = true,
            "--sysfs-root" => root = args.next().ok_or("--sysfs-root needs a directory")?.into(),
            "--slice-us-underutil" | "-u" => {
                slice_config.underutil_ns = pavo_slice_ns_from_us(
                    &args
                        .next()
                        .ok_or("--slice-us-underutil needs microseconds")?,
                )?;
            }
            "--slice-us-overutil" | "-o" => {
                slice_config.overutil_ns = pavo_slice_ns_from_us(
                    &args
                        .next()
                        .ok_or("--slice-us-overutil needs microseconds")?,
                )?;
            }
            "--slice-update-interval" | "--tune-interval" | "-I" => {
                slice_config.interval = pavo_slice_interval(
                    &args.next().ok_or("--slice-update-interval needs seconds")?,
                )?;
            }
            "--cpumasks" | "-C" => {
                let before = groups.len();
                while args.peek().is_some_and(|next| !next.starts_with('-')) {
                    groups.push(pavo_parse_hex_mask(&args.next().unwrap())?);
                }
                if groups.len() == before {
                    return Err("--cpumasks needs at least one hex mask".into());
                }
            }
            "--version" => {
                println!("scx_pavo {}", env!("CARGO_PKG_VERSION"));
                return Ok(());
            }
            "--help" | "-h" => {
                pavo_help();
                return Ok(());
            }
            _ => return Err(format!("Unknown option {arg}; use --help").into()),
        }
    }
    // Start the scheduler by default.
    if !inspect {
        run = true;
    }
    if run && inspect {
        pavo_help();
        return Err("Do not combine --run and --inspect".into());
    }
    if run && root != std::path::Path::new("/sys/devices/system/cpu") {
        return Err("--sysfs-root is allowed only with --inspect, never for attachment".into());
    }
    let topology = PavoTopology::pavo_discover(&root, &groups)?;
    println!("{topology:#?}");
    println!("{slice_config:#?}");
    if inspect {
        return Ok(());
    }
    topology.pavo_validate_for_run()?;
    for policy in &topology.frequency_domains {
        if policy.governor != "schedutil" {
            eprintln!(
                "{} governor is {:?}; set schedutil before measurement.",
                policy.sysfs_name, policy.governor
            );
        }
    }
    #[cfg(all(target_os = "linux", feature = "scheduler"))]
    {
        pavo_linux::pavo_run(topology, slice_config)
    }
    #[cfg(not(all(target_os = "linux", feature = "scheduler")))]
    {
        Err("Attachment requires a Linux build with the scheduler feature".into())
    }
}

fn pavo_help() {
    println!(
        "scx_pavo [--run | --inspect] [--cpumasks HEX]...\n\
        No mode specified: load/attach the scheduler. Ctrl-C detaches.\n\
        --inspect                 Show CPU topology\n\
        --run                     Optional alias for the default load/attach mode\n\
        --cpumasks HEX... / -C    Override the automatic placement groups\n\
                                  Disjoint groups covering all online CPUs\n\
                                  Read CPU capacity from sysfs\n\
        --sysfs-root DIRECTORY    Offline topology inspection only\n\
        --slice-us-underutil / -u Microseconds when not fully utilized (20000)\n\
        --slice-us-overutil / -o  Microseconds when fully utilized (1000)\n\
        --slice-update-interval   CPU-utilization sampling interval, seconds (0.1)\n\
                                  Aliases: --tune-interval / -I\n\
        Slice utilization is independent of the Shared-DSQ High/Low gate."
    );
}

#[cfg(all(target_os = "linux", feature = "scheduler"))]
mod pavo_linux {
    mod pavo_skeleton {
        include!(concat!(env!("OUT_DIR"), "/pavo.skel.rs"));
    }
    use super::*;
    use libbpf_rs::skel::{OpenSkel, Skel, SkelBuilder};
    use pavo_skeleton::PavoSkelBuilder;
    use scx_pavo::slice::PavoSliceSampler;
    use std::mem::MaybeUninit;
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    };
    use std::time::{Duration, Instant};

    pub fn pavo_run(
        topology: PavoTopology,
        slice_config: PavoSliceConfig,
    ) -> Result<(), Box<dyn std::error::Error>> {
        if std::env::consts::ARCH != "aarch64" {
            return Err("This artifact targets ARM64 Pixel 6 / OnePlus 15; host builds are compile checks only".into());
        }
        let sched_ext = std::path::Path::new("/sys/kernel/sched_ext");
        let state = std::fs::read_to_string(sched_ext.join("state"))?;
        if state.trim() != "disabled" {
            return Err(
                "Another sched_ext policy is active; it was not stopped or replaced".into(),
            );
        }
        let sequence: u64 = std::fs::read_to_string(sched_ext.join("hotplug_seq"))?
            .trim()
            .parse()?;
        // Read the accounting baseline before attaching.
        let mut slice_sampler =
            PavoSliceSampler::pavo_new(topology.online, &std::fs::read_to_string("/proc/stat")?)?;
        if Instant::now().checked_add(slice_config.interval).is_none() {
            return Err("slice sampling interval exceeds the host clock range".into());
        }
        let stop = Arc::new(AtomicBool::new(false));
        let stop_signal = Arc::clone(&stop);
        ctrlc::set_handler(move || stop_signal.store(true, Ordering::Relaxed))?;
        let mut object = MaybeUninit::uninit();
        let builder = PavoSkelBuilder::default();
        let mut open = builder.open(&mut object)?;
        let data = &mut open.maps.rodata_data;
        data.pavo_slice_underutil_ns = slice_config.underutil_ns;
        data.pavo_slice_overutil_ns = slice_config.overutil_ns;
        data.pavo_online_mask = topology.online;
        data.pavo_high_capacity_mask = topology.higher_capacity;
        data.pavo_nr_cpu_ids = (64 - topology.online.leading_zeros()) as u32;
        data.pavo_nr_policies = topology.frequency_domains.len() as u32;
        for cpu in &topology.cpus {
            data.pavo_capacity[cpu.id] = cpu.capacity.unwrap_or(0);
            data.pavo_cache_group[cpu.id] = cpu.cache_group;
            data.pavo_candidate_group[cpu.id] = cpu.candidate_group;
            data.pavo_candidate_mask[cpu.id] = topology.pavo_candidate_mask(cpu);
            data.pavo_policy_id[cpu.id] = cpu.frequency_domain.unwrap();
        }
        for (id, policy) in topology.frequency_domains.iter().enumerate() {
            data.pavo_policy_mask[id] = policy.cpus;
        }
        open.struct_ops.pavo_ops_mut().hotplug_seq = sequence;
        let mut skel = open.load()?;
        if std::fs::read_to_string(sched_ext.join("state"))?.trim() != "disabled" {
            return Err("A scheduler started during preparation; refusing attachment".into());
        }
        skel.attach()?;
        let link = skel.maps.pavo_ops.attach_struct_ops()?;
        let mut next_slice_sample = Instant::now() + slice_config.interval;
        println!("scx_pavo attached as pavo. Ctrl-C / SIGTERM detaches it.");
        // BSS is mmap'd and shared with the kernel, outside Rust's alias model.
        while !stop.load(Ordering::Relaxed)
            && unsafe { std::ptr::read_volatile(&raw const skel.maps.bss_data.pavo_exited) } == 0
        {
            let now = Instant::now();
            if now >= next_slice_sample {
                let utilization =
                    slice_sampler.pavo_sample(&std::fs::read_to_string("/proc/stat")?)?;
                unsafe {
                    std::ptr::write_volatile(
                        &raw mut skel.maps.bss_data.pavo_slice_utilization,
                        utilization,
                    );
                }
                // Advance the periodic deadline, accounting for missed intervals.
                next_slice_sample += slice_config.interval;
                if next_slice_sample < now {
                    next_slice_sample = now + slice_config.interval;
                }
            }
            std::thread::sleep(
                next_slice_sample
                    .saturating_duration_since(Instant::now())
                    .min(Duration::from_millis(200)),
            );
        }
        drop(link);
        if skel.maps.bss_data.pavo_exited != 0 {
            let bytes: Vec<u8> = skel
                .maps
                .bss_data
                .pavo_exit_reason
                .iter()
                .map(|b| *b as u8)
                .take_while(|b| *b != 0)
                .collect();
            let reason = String::from_utf8_lossy(&bytes);
            eprintln!(
                "pavo exit kind={} code={}: {}",
                skel.maps.bss_data.pavo_exit_kind, skel.maps.bss_data.pavo_exit_code, reason
            );
            if !stop.load(Ordering::Relaxed) {
                return Err(
                    "Pavo exited before a user stop; preserve the kernel verifier/exit logs".into(),
                );
            }
        }
        Ok(())
    }
}
