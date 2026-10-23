// SPDX-License-Identifier: GPL-2.0-only
// CPU accounting and slice selection derived from scx/Pavo.
// Copyright (c) Meta Platforms, Inc. and affiliates.
// Sample CPU utilization to select the base slice.
use std::collections::BTreeMap;
use std::time::Duration;

pub const PAVO_SLICE_UNDERUTIL_NS: u64 = 20_000_000;
pub const PAVO_SLICE_OVERUTIL_NS: u64 = 1_000_000;
// Keep base * percentage representable, including virtual-time comparisons.
pub const PAVO_SLICE_BASE_MAX_NS: u64 = (i64::MAX as u64) / 150;
pub const PAVO_UTIL_UNDERUTIL: u32 = 1;
pub const PAVO_UTIL_OVERUTIL: u32 = 2;
const PAVO_FULL_UTILIZATION: f64 = 0.99999;

#[derive(Clone, Debug)]
pub struct PavoSliceConfig {
    pub underutil_ns: u64,
    pub overutil_ns: u64,
    pub interval: Duration,
}

impl Default for PavoSliceConfig {
    fn default() -> Self {
        Self {
            underutil_ns: PAVO_SLICE_UNDERUTIL_NS,
            overutil_ns: PAVO_SLICE_OVERUTIL_NS,
            interval: Duration::from_millis(100),
        }
    }
}

pub fn pavo_slice_ns_from_us(value: &str) -> Result<u64, String> {
    let ns = value
        .parse::<u64>()
        .ok()
        .and_then(|us| us.checked_mul(1000))
        .filter(|ns| *ns > 0 && *ns <= PAVO_SLICE_BASE_MAX_NS)
        .ok_or_else(|| {
            "slice duration must be positive microseconds within the arithmetic range".to_owned()
        })?;
    Ok(ns)
}

pub fn pavo_slice_interval(value: &str) -> Result<Duration, String> {
    let seconds = value.parse::<f64>().map_err(|_| "invalid slice interval")?;
    let interval = Duration::try_from_secs_f64(seconds).map_err(|_| "invalid slice interval")?;
    if interval.is_zero() {
        return Err("slice interval must be positive seconds".into());
    }
    Ok(interval)
}

#[derive(Clone, Debug)]
struct PavoCpuTime([u64; 8]);

type PavoCpuTimes = BTreeMap<u32, PavoCpuTime>;

fn pavo_parse_cpu_times(text: &str) -> Result<PavoCpuTimes, String> {
    let mut cpus = BTreeMap::new();
    for line in text.lines() {
        let mut fields = line.split_whitespace();
        let Some(name) = fields.next() else { continue };
        let Some(id) = name.strip_prefix("cpu") else {
            continue;
        };
        if id.is_empty() {
            continue; // Skip the aggregate CPU line.
        }
        let id: u32 = id.parse().map_err(|_| "invalid CPU id in /proc/stat")?;
        let mut ticks = [0; 8];
        for tick in &mut ticks {
            *tick = fields
                .next()
                .ok_or("missing CPU field in /proc/stat")?
                .parse()
                .map_err(|_| "invalid CPU counter in /proc/stat")?;
        }
        // guest and guest_nice, if present, are already in user/nice.
        if cpus.insert(id, PavoCpuTime(ticks)).is_some() {
            return Err("duplicate CPU in /proc/stat".into());
        }
    }
    if cpus.is_empty() {
        return Err("no per-CPU counters in /proc/stat".into());
    }
    Ok(cpus)
}

fn pavo_cpu_utilization(current: &PavoCpuTime, previous: &PavoCpuTime) -> f64 {
    let delta: [u64; 8] = std::array::from_fn(|i| current.0[i].saturating_sub(previous.0[i]));
    // Per-field saturated deltas account for counter resets.
    let busy: u128 = [0, 1, 2, 5, 6, 7].iter().map(|i| delta[*i] as u128).sum();
    let total = busy + delta[3] as u128 + delta[4] as u128;
    if total == 0 {
        1.0 // Zero elapsed CPU time is treated as fully utilized.
    } else {
        (busy as f64 / total as f64).clamp(0.0, 1.0)
    }
}

pub struct PavoSliceSampler {
    online: u64,
    previous: PavoCpuTimes,
}

impl PavoSliceSampler {
    pub fn pavo_new(online: u64, initial: &str) -> Result<Self, String> {
        if online == 0 {
            return Err("cannot sample an empty CPU set".into());
        }
        Ok(Self {
            online,
            previous: pavo_parse_cpu_times(initial)?,
        })
    }

    pub fn pavo_sample(&mut self, text: &str) -> Result<u32, String> {
        let current = pavo_parse_cpu_times(text)?;
        let mut utilization = 0.0;
        for cpu in 0..64 {
            if self.online & (1u64 << cpu) == 0 {
                continue;
            }
            if let (Some(curr), Some(prev)) = (current.get(&cpu), self.previous.get(&cpu)) {
                utilization += pavo_cpu_utilization(curr, prev);
            }
            // A CPU missing from either
            // snapshot contributes zero, but remains in the denominator.
        }
        utilization /= self.online.count_ones() as f64;
        self.previous = current;
        Ok(if utilization >= PAVO_FULL_UTILIZATION {
            PAVO_UTIL_OVERUTIL
        } else {
            PAVO_UTIL_UNDERUTIL
        })
    }
}
