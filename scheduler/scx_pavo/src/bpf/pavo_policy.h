/* SPDX-License-Identifier: GPL-2.0-only */
/* Time primitives derived from scx/Pavo:
 * Copyright (c) Meta Platforms, Inc. and affiliates. */
#ifndef PAVO_POLICY_H
#define PAVO_POLICY_H

#define PAVO_MAX_CPUS 64
#define PAVO_SCORE_MAX 59U
#define PAVO_WEIGHT_REFERENCE 100ULL
#define PAVO_WEIGHT_MAX 10000ULL
#define PAVO_RATE_MAX 100000ULL
#define PAVO_RATE_INTERVAL_NS 100000000ULL
#define PAVO_RUNTIME_SCALE_NS 200000ULL
#define PAVO_SHORT_RUNTIME_NS 800000ULL
#define PAVO_NVCSW_THRESHOLD 20000ULL
#define PAVO_LOAD_ENTER 10U
#define PAVO_LOAD_EXIT 8U
#define PAVO_FIXED_SHIFT 8
#define PAVO_FIXED_ONE (1ULL << PAVO_FIXED_SHIFT)
#define PAVO_CPUPERF_ONE 1024U
#define PAVO_DEFAULT_UNDERUTIL_SLICE_NS 20000000ULL
#define PAVO_DEFAULT_OVERUTIL_SLICE_NS 1000000ULL
#define PAVO_SLICE_SCALE_CAP 150U
#define PAVO_SLICE_BASE_MAX_NS (((1ULL << 63) - 1) / PAVO_SLICE_SCALE_CAP)
#define PAVO_UTIL_NOT_SAMPLED 0U
#define PAVO_UTIL_UNDERUTIL 1U
#define PAVO_UTIL_OVERUTIL 2U
#define PAVO_PERF_DECAY_STEP (PAVO_CPUPERF_ONE / 20)
#define PAVO_VTIME_DELTA_MAX ((1ULL << 63) - 1)
#define PAVO_DEADLINE_TABLE_TOP 38U

static inline __attribute__((always_inline)) u64 pavo_min(u64 a, u64 b) { return a < b ? a : b; }
static inline __attribute__((always_inline)) u64 pavo_max(u64 a, u64 b) { return a > b ? a : b; }

/* Fixed rational EWMA: all three behavioral fields use the same 1/4 weight. */
static inline __attribute__((always_inline)) u64 pavo_behavior_ewma(u64 old, u64 sample)
{
    return old - (old >> 2) + (sample >> 2);
}

static inline __attribute__((always_inline)) u64 pavo_rate_update(u64 old_q8, u64 interval)
{
    if (!interval)
        return old_q8;
    u64 sample = pavo_min(PAVO_RATE_INTERVAL_NS / interval, PAVO_RATE_MAX);
    return pavo_behavior_ewma(old_q8, sample << PAVO_FIXED_SHIFT);
}

/* fls64-equivalent fixed sequence, then linear interpolation in Q8. */
static inline __attribute__((always_inline)) u32 pavo_log2_q8(u64 value)
{
    if (value <= 1)
        return 0;
    u64 n = value;
    u32 shift = 0;
    if (n >> 32) { n >>= 32; shift += 32; }
    if (n >> 16) { n >>= 16; shift += 16; }
    if (n >> 8) { n >>= 8; shift += 8; }
    if (n >> 4) { n >>= 4; shift += 4; }
    if (n >> 2) { n >>= 2; shift += 2; }
    if (n >> 1) { shift += 1; }
    u64 base = 1ULL << shift;
    u64 fraction = shift >= 8 ? (value - base) >> (shift - 8)
                             : (value - base) << (8 - shift);
    return (shift << 8) + (u32)fraction;
}

static inline __attribute__((always_inline)) u32 pavo_score(u64 block_q8, u64 wake_q8, u64 runtime_ns,
                             u32 weight, u64 nvcsw, bool rendering_role,
                             bool wake_chain)
{
    u64 bf = pavo_min(block_q8 >> 8, PAVO_RATE_MAX);
    u64 wf = pavo_min(wake_q8 >> 8, PAVO_RATE_MAX);
    u64 w = pavo_min(pavo_max(weight, 1), PAVO_WEIGHT_MAX);
    /* Max product is 10^19, below UINT64_MAX; division follows multiplication. */
    u64 interaction = bf * wf * wf * w / PAVO_WEIGHT_REFERENCE;
    u64 bucket = runtime_ns / PAVO_RUNTIME_SCALE_NS;
    bucket = pavo_min(bucket, (~0ULL - 1) / PAVO_WEIGHT_REFERENCE);
    u64 penalty = bucket * PAVO_WEIGHT_REFERENCE / w;
    u32 runtime_bonus = runtime_ns < PAVO_SHORT_RUNTIME_NS ? 2U : 0U;
    if (runtime_bonus && nvcsw > PAVO_NVCSW_THRESHOLD)
        runtime_bonus = 4;
    /* Apply the runtime, wake and rendering-role corrections. */
    u32 correction = runtime_bonus + (wake_chain ? 4U : 0U) +
                     (rendering_role ? 5U : 0U);
    s64 score_q8 = (s64)pavo_log2_q8(interaction + 1) -
                   pavo_log2_q8(penalty + 1) + ((s64)correction << 8);
    if (score_q8 <= 0)
        return 0;
    return (u32)pavo_min((u64)score_q8 >> 8, PAVO_SCORE_MAX);
}

static inline __attribute__((always_inline)) u64 pavo_threshold_update(u64 reference_q8, u32 score)
{
    return (reference_q8 * 7 + ((u64)score << 8) * 3) / 10;
}

static inline __attribute__((always_inline)) bool pavo_gate(u32 score, u64 reference_q8, bool high_load)
{
    return high_load && ((u64)score << 8) > reference_q8 * 3;
}

static inline __attribute__((always_inline)) u64 pavo_occupancy_update(u64 average_q8, u32 queued)
{
    /* Convert queue occupancy to Q8 before smoothing. */
    return average_q8 - (average_q8 >> 3) + (((u64)queued << 8) >> 3);
}

static inline __attribute__((always_inline)) bool pavo_load_mode(u64 occupancy_q8, bool previous)
{
    if (occupancy_q8 > ((u64)PAVO_LOAD_ENTER << 8))
        return true;
    if (occupancy_q8 < ((u64)PAVO_LOAD_EXIT << 8))
        return false;
    return previous;
}

static inline __attribute__((always_inline)) u32 pavo_cpuperf_from_score(u32 score)
{
    return (u32)pavo_min(score / 12, 4) * (PAVO_CPUPERF_ONE / 4);
}

/* Map scx.weight to CPUperf levels for the low-load path.
 * The reference scx.weight is 100. */
static inline __attribute__((always_inline)) u32 pavo_cpuperf_from_weight(u64 weight)
{
    if (weight > 800) return 1024;
    if (weight > 300) return 768;
    if (weight > 100) return 512;
    if (weight > 30) return 256;
    return 0;
}

static inline __attribute__((always_inline)) u64 pavo_slice(u64 base_ns, u32 score)
{
    score = (u32)pavo_min(score, PAVO_SCORE_MAX);
    u64 scale = score <= 10 ? 50 + score * 5 :
                score <= 30 ? 100 + (score - 10) * 2 : 140 + score - 30;
    /* Cap the slice relative to its utilization-selected base. */
    base_ns = pavo_min(base_ns, PAVO_SLICE_BASE_MAX_NS);
    return base_ns * pavo_min(scale, PAVO_SLICE_SCALE_CAP) / 100;
}

/* Linux scheduler nice-weight table used by the Pavo Time path.
 * The table index is max(38 - score, 0).
 * The weight saturates at 10000 for scores 29..59.
 */
static const u32 pavo_deadline_weights[] = {
    88761, 71755, 56483, 46273, 36291, 29154, 23254, 18705, 14949, 11916,
    9548, 7620, 6100, 4904, 3906, 3121, 2501, 1991, 1586, 1277,
    1024, 820, 655, 526, 423, 335, 272, 215, 172, 137,
    110, 87, 70, 56, 45, 36, 29, 23, 18, 15,
};

static inline __attribute__((always_inline)) u64 pavo_deadline_weight(u32 score)
{
    u32 index = PAVO_DEADLINE_TABLE_TOP -
                (u32)pavo_min(score, PAVO_DEADLINE_TABLE_TOP);
    return pavo_min(pavo_deadline_weights[index], PAVO_WEIGHT_MAX);
}

/* floor(value * 100 / weight), exact until the signed-vtime comparison limit.
 * Ordinary execution takes a one-division path. The overflow path
 * uses quotient/remainder to avoid wrapping.
 * A zero request stays zero. */
static inline __attribute__((always_inline)) u64 pavo_inverse_weight(u64 value, u64 weight)
{
    u64 w = pavo_min(pavo_max(weight, 1), PAVO_WEIGHT_MAX);
    if (value <= ~0ULL / PAVO_WEIGHT_REFERENCE)
        return pavo_min(value * PAVO_WEIGHT_REFERENCE / w, PAVO_VTIME_DELTA_MAX);
    u64 whole = value / w;
    u64 fraction = (value % w) * PAVO_WEIGHT_REFERENCE / w;
    if (whole > PAVO_VTIME_DELTA_MAX / PAVO_WEIGHT_REFERENCE)
        return PAVO_VTIME_DELTA_MAX;
    u64 scaled = whole * PAVO_WEIGHT_REFERENCE;
    return scaled > PAVO_VTIME_DELTA_MAX - fraction ?
           PAVO_VTIME_DELTA_MAX : scaled + fraction;
}

static inline __attribute__((always_inline)) u64 pavo_deadline_delta(u64 runtime_ns, u32 score)
{
    return pavo_inverse_weight(runtime_ns, pavo_deadline_weight(score));
}

static inline __attribute__((always_inline)) u64 pavo_prefer_group(u64 eligible, u64 group)
{
    u64 preferred = eligible & group;
    return preferred ? preferred : eligible;
}

/* Bounded priority encoder: input must ALREADY be affinity intersected. */
static inline __attribute__((always_inline)) u32 pavo_first_cpu(u64 mask)
{
    u32 cpu = 0;
    if (!(mask & 0xffffffffULL)) { mask >>= 32; cpu += 32; }
    if (!(mask & 0xffffULL)) { mask >>= 16; cpu += 16; }
    if (!(mask & 0xffULL)) { mask >>= 8; cpu += 8; }
    if (!(mask & 0xfULL)) { mask >>= 4; cpu += 4; }
    if (!(mask & 0x3ULL)) { mask >>= 2; cpu += 2; }
    if (!(mask & 0x1ULL)) cpu += 1;
    return cpu;
}

static inline __attribute__((always_inline)) u32 pavo_pick_cpu(u64 eligible, u32 cursor)
{
    if (!eligible)
        return PAVO_MAX_CPUS;
    u64 remaining = eligible & (~0ULL << (cursor & 63));
    return pavo_first_cpu(remaining ? remaining : eligible);
}

#endif
