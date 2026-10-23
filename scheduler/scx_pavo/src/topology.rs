// SPDX-License-Identifier: GPL-2.0-only
// Topology grouping derived from scx_utils 1.0.11 and Pavo.
// Copyright (c) Meta Platforms, Inc. and affiliates.
//! Capacity classes, cache groups and cpufreq policies are intentionally distinct.
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::Path;

pub const PAVO_MAX_CPUS: usize = 64;
type Result<T> = std::result::Result<T, String>;

#[derive(Debug, Clone)]
pub struct PavoCache {
    pub level: u32,
    pub kind: String,
    pub cpus: u64,
}

#[derive(Debug, Clone)]
pub struct PavoCpu {
    pub id: usize,
    pub capacity: Option<u32>,
    pub core_id: Option<i32>,
    pub cluster_id: Option<i32>,
    pub package_id: Option<i32>,
    pub node_id: u32,
    pub caches: Vec<PavoCache>,
    pub cache_group: u32,
    pub candidate_group: u32,
    pub frequency_domain: Option<u32>,
}

#[derive(Debug, Clone)]
pub struct PavoFrequencyDomain {
    pub sysfs_name: String,
    pub cpus: u64,
    pub governor: String,
    pub min_khz: Option<u64>,
    pub max_khz: Option<u64>,
}

#[derive(Debug, Clone)]
pub struct PavoTopology {
    pub online: u64,
    pub higher_capacity: u64,
    pub cpus: Vec<PavoCpu>,
    pub capacity_classes: BTreeMap<u32, u64>,
    pub cache_groups: Vec<u64>,
    pub candidate_groups: Vec<u64>,
    pub explicit_groups: bool,
    pub frequency_domains: Vec<PavoFrequencyDomain>,
    pub capacity_source: String,
    pub candidate_group_source: String,
    pub warnings: Vec<String>,
}

pub fn pavo_parse_cpu_list(text: &str) -> Result<u64> {
    let mut mask = 0u64;
    for token in text.trim().split(|c: char| c == ',' || c.is_whitespace()) {
        if token.is_empty() {
            continue;
        }
        let parts: Vec<_> = token.split('-').collect();
        if parts.is_empty() || parts.len() > 2 {
            return Err(format!("Invalid CPU list: {text}"));
        }
        let start: usize = parts[0]
            .parse()
            .map_err(|_| format!("Invalid CPU: {token}"))?;
        let end: usize = parts
            .last()
            .unwrap()
            .parse()
            .map_err(|_| format!("Invalid CPU: {token}"))?;
        if start > end || end >= PAVO_MAX_CPUS {
            return Err(format!("CPU range must be within 0..63: {token}"));
        }
        for cpu in start..=end {
            mask |= 1u64 << cpu;
        }
    }
    if mask == 0 {
        return Err("Empty CPU list".into());
    }
    Ok(mask)
}

pub fn pavo_parse_hex_mask(text: &str) -> Result<u64> {
    let clean = text.trim().trim_start_matches("0x").replace(['_', ','], "");
    let value =
        u64::from_str_radix(&clean, 16).map_err(|_| format!("Invalid hex cpumask: {text}"))?;
    if value == 0 {
        return Err("An explicit CPU group cannot be empty".into());
    }
    Ok(value)
}

pub fn pavo_validate_groups(groups: &[u64], online: u64) -> Result<()> {
    let mut covered = 0;
    for group in groups {
        if *group == 0 || group & !online != 0 || group & covered != 0 {
            return Err(
                "--cpumasks must be nonempty, disjoint and contain only online CPUs".into(),
            );
        }
        covered |= group;
    }
    if covered != online {
        return Err("--cpumasks must cover every online CPU exactly once".into());
    }
    Ok(())
}

pub fn pavo_higher_capacity_mask(capacities: &[(usize, Option<u32>)]) -> (u64, &'static str) {
    // Build the higher-capacity set from positive sysfs capacities.
    let minimum = capacities
        .iter()
        .filter_map(|(_, value)| *value)
        .filter(|v| *v > 0)
        .min();
    let Some(minimum) = minimum else {
        return (0, "no observed cpu_capacity: Shared DSQ only");
    };
    let mask = capacities
        .iter()
        .filter(|(_, value)| value.is_some_and(|v| v > minimum))
        .fold(0u64, |mask, (cpu, _)| mask | (1u64 << cpu));
    (
        mask,
        "sysfs cpu_capacity (strictly above minimum observed capacity)",
    )
}

fn pavo_read_optional<T: std::str::FromStr>(path: &Path) -> Option<T> {
    fs::read_to_string(path).ok()?.trim().parse().ok()
}

fn pavo_read_node_id(cpu_path: &Path) -> Result<u32> {
    let mut nodes = BTreeSet::new();
    if let Ok(entries) = fs::read_dir(cpu_path) {
        for entry in entries.flatten() {
            let name = entry.file_name();
            if let Some(id) = name.to_string_lossy().strip_prefix("node") {
                if let Ok(id) = id.parse::<u32>() {
                    nodes.insert(id);
                }
            }
        }
    }
    match nodes.len() {
        0 => Ok(0), // Default to node 0 when sysfs has no node entry.
        1 => Ok(*nodes.first().unwrap()),
        _ => Err(format!("Multiple NUMA nodes for {}", cpu_path.display())),
    }
}

fn pavo_read_cache_id(path: &Path, known: &mut BTreeMap<String, u64>) -> Option<u64> {
    // Matches scx_utils 1.0.11 get_cache_id: shared_cpu_list is the identity key;
    // prefer the kernel id, otherwise allocate a stable id for that sharing set.
    let shared = fs::read_to_string(path.join("shared_cpu_list")).ok()?;
    if let Some(id) = known.get(&shared) {
        return Some(*id);
    }
    let id = pavo_read_optional(&path.join("id")).unwrap_or(known.len() as u64);
    known.insert(shared, id);
    Some(id)
}

fn pavo_read_caches(cpu_path: &Path, online: u64) -> Vec<PavoCache> {
    let mut caches = Vec::new();
    if let Ok(entries) = fs::read_dir(cpu_path.join("cache")) {
        for entry in entries.flatten() {
            let path = entry.path();
            if !entry.file_name().to_string_lossy().starts_with("index") {
                continue;
            }
            let (Some(level), Ok(kind), Ok(shared)) = (
                pavo_read_optional(&path.join("level")),
                fs::read_to_string(path.join("type")),
                fs::read_to_string(path.join("shared_cpu_list")),
            ) else {
                continue;
            };
            if let Ok(mask) = pavo_parse_cpu_list(&shared) {
                caches.push(PavoCache {
                    level,
                    kind: kind.trim().into(),
                    cpus: mask & online,
                });
            }
        }
    }
    caches.sort_by_key(|cache| cache.level);
    caches
}

impl PavoTopology {
    pub fn pavo_discover(root: &Path, explicit: &[u64]) -> Result<Self> {
        let online = pavo_parse_cpu_list(
            &fs::read_to_string(root.join("online")).map_err(|e| e.to_string())?,
        )?;
        if let Ok(possible) = fs::read_to_string(root.join("possible")) {
            if online & !pavo_parse_cpu_list(&possible)? != 0 {
                return Err("Online CPU is missing from the possible CPU set".into());
            }
        }
        if !explicit.is_empty() {
            pavo_validate_groups(explicit, online)?;
        }
        let mut result = Self {
            online,
            higher_capacity: 0,
            cpus: Vec::new(),
            capacity_classes: BTreeMap::new(),
            cache_groups: Vec::new(),
            candidate_groups: Vec::new(),
            explicit_groups: !explicit.is_empty(),
            frequency_domains: Vec::new(),
            capacity_source: String::new(),
            candidate_group_source: String::new(),
            warnings: Vec::new(),
        };
        let mut l2_ids = BTreeMap::new();
        let mut l3_ids = BTreeMap::new();
        let mut llc_groups = BTreeMap::new();
        for id in 0..PAVO_MAX_CPUS {
            if online & (1u64 << id) == 0 {
                continue;
            }
            let path = root.join(format!("cpu{id}"));
            let capacity: Option<u32> =
                pavo_read_optional(&path.join("cpu_capacity")).filter(|v| *v > 0);
            if let Some(value) = capacity {
                *result.capacity_classes.entry(value).or_default() |= 1u64 << id;
            } else {
                result.warnings.push(format!(
                    "CPU {id}: capacity unavailable or zero; excluded from the higher-capacity set"
                ));
            }
            let caches = pavo_read_caches(&path, online);
            let node_id = pavo_read_node_id(&path)?;
            // Group by index3, then index2, then the NUMA node.
            let l2 = pavo_read_cache_id(&path.join("cache/index2"), &mut l2_ids);
            let l3 = pavo_read_cache_id(&path.join("cache/index3"), &mut l3_ids);
            let llc_id = l3.or(l2);
            if llc_id.is_none() {
                result.warnings.push(format!("CPU {id}: cache/index2 and index3 unavailable; unified candidate group within node {node_id}"));
            }
            let key = (node_id, llc_id);
            let next_group = result.cache_groups.len() as u32;
            let cache_group = *llc_groups.entry(key).or_insert(next_group);
            if cache_group == next_group {
                result.cache_groups.push(0);
            }
            result.cache_groups[cache_group as usize] |= 1u64 << id;
            result.cpus.push(PavoCpu {
                id,
                capacity,
                core_id: pavo_read_optional(&path.join("topology/core_id")),
                cluster_id: pavo_read_optional(&path.join("topology/cluster_id")),
                package_id: pavo_read_optional(&path.join("topology/physical_package_id")),
                node_id,
                caches,
                cache_group,
                candidate_group: cache_group,
                frequency_domain: None,
            });
        }
        if !explicit.is_empty() {
            result.candidate_groups = explicit.to_vec();
            result.candidate_group_source =
                "explicit --cpumasks (placement preference; capacities and caches unchanged)"
                    .into();
            for cpu in &mut result.cpus {
                cpu.candidate_group = explicit
                    .iter()
                    .position(|mask| mask & (1u64 << cpu.id) != 0)
                    .unwrap() as u32;
            }
        } else {
            result.candidate_groups = result.cache_groups.clone();
            result.candidate_group_source =
                "automatic cache/index3 -> index2 -> unified per node".into();
        }
        let capacities: Vec<_> = result
            .cpus
            .iter()
            .map(|cpu| (cpu.id, cpu.capacity))
            .collect();
        let (high, source) = pavo_higher_capacity_mask(&capacities);
        result.higher_capacity = high;
        result.capacity_source = source.into();
        if high == 0 {
            result
                .warnings
                .push("No verified higher-capacity set: Core uses Shared DSQ".into());
        }
        if let Ok(entries) = fs::read_dir(root.join("cpufreq")) {
            let mut policies: Vec<_> = entries
                .flatten()
                .filter(|entry| {
                    entry
                        .file_name()
                        .to_string_lossy()
                        .strip_prefix("policy")
                        .is_some_and(|n| n.parse::<u32>().is_ok())
                })
                .collect();
            policies.sort_by_key(|entry| {
                entry.file_name().to_string_lossy()[6..]
                    .parse::<u32>()
                    .unwrap()
            });
            let mut assigned = BTreeSet::new();
            for entry in policies {
                let path = entry.path();
                let membership = fs::read_to_string(path.join("related_cpus"))
                    .or_else(|_| fs::read_to_string(path.join("affected_cpus")))
                    .map_err(|e| e.to_string())?;
                let mask = pavo_parse_cpu_list(&membership)? & online;
                if mask == 0 {
                    continue;
                }
                let index = result.frequency_domains.len() as u32;
                for cpu in &mut result.cpus {
                    if mask & (1u64 << cpu.id) != 0 {
                        if !assigned.insert(cpu.id) {
                            return Err("Overlapping cpufreq policy membership".into());
                        }
                        cpu.frequency_domain = Some(index);
                    }
                }
                result.frequency_domains.push(PavoFrequencyDomain {
                    sysfs_name: entry.file_name().to_string_lossy().into(),
                    cpus: mask,
                    governor: fs::read_to_string(path.join("scaling_governor"))
                        .unwrap_or_default()
                        .trim()
                        .into(),
                    min_khz: pavo_read_optional(&path.join("cpuinfo_min_freq")),
                    max_khz: pavo_read_optional(&path.join("cpuinfo_max_freq")),
                });
            }
        }
        Ok(result)
    }

    pub fn pavo_candidate_mask(&self, cpu: &PavoCpu) -> u64 {
        self.candidate_groups[cpu.candidate_group as usize]
    }

    pub fn pavo_validate_for_run(&self) -> Result<()> {
        if self.frequency_domains.is_empty()
            || self.cpus.iter().any(|cpu| cpu.frequency_domain.is_none())
        {
            return Err(
                "Every online CPU needs a cpufreq policy; inspect the real sysfs mount".into(),
            );
        }
        Ok(())
    }
}
