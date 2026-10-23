/* SPDX-License-Identifier: GPL-2.0-only */
/* Pavo policy. */
/* Scheduling primitives adapted from sched-ext/scx v1.0.9; see README.md.
 * Copyright (c) Meta Platforms, Inc. and affiliates. */
#include <pavo_compat.bpf.h>
#include "pavo_policy.h"

int bpf_iter_num_new(struct bpf_iter_num *it, int start, int end) __ksym;
int *bpf_iter_num_next(struct bpf_iter_num *it) __ksym;
void bpf_iter_num_destroy(struct bpf_iter_num *it) __ksym;
void bpf_rcu_read_lock(void) __ksym;
void bpf_rcu_read_unlock(void) __ksym;

char pavo_license[] SEC("license") = "GPL";
#define PAVO_SHARED_DSQ 0ULL
#define PAVO_MAX_TASKS 131072

enum pavo_android_role {
    PAVO_ROLE_NONE = 0,
    PAVO_ROLE_MAIN_UI_CANDIDATE = 1U << 0,
    PAVO_ROLE_RENDER_THREAD = 1U << 1,
    PAVO_ROLE_INPUT_DISPATCHER = 1U << 2,
    PAVO_ROLE_HWUI_TASK = 1U << 3,
};

/* Loader-supplied CPU topology and cache groups. */
PAVO_RODATA u64 pavo_online_mask;
PAVO_RODATA u64 pavo_high_capacity_mask;
PAVO_RODATA u32 pavo_nr_cpu_ids;
PAVO_RODATA u32 pavo_nr_policies;
PAVO_RODATA u32 pavo_capacity[PAVO_MAX_CPUS];
PAVO_RODATA u32 pavo_cache_group[PAVO_MAX_CPUS];
PAVO_RODATA u32 pavo_candidate_group[PAVO_MAX_CPUS];
PAVO_RODATA u64 pavo_candidate_mask[PAVO_MAX_CPUS];
PAVO_RODATA u32 pavo_policy_id[PAVO_MAX_CPUS];
PAVO_RODATA u64 pavo_policy_mask[PAVO_MAX_CPUS];

/* Slice timing, independent of the score/Shared-DSQ load gate.
 * Userspace supplies the sampled utilization state. */
PAVO_RODATA u64 pavo_slice_underutil_ns = PAVO_DEFAULT_UNDERUTIL_SLICE_NS;
PAVO_RODATA u64 pavo_slice_overutil_ns = PAVO_DEFAULT_OVERUTIL_SLICE_NS;
volatile u32 pavo_slice_utilization;

/* Scheduler exit status for userspace. */
volatile u32 pavo_exited;
volatile s32 pavo_exit_kind;
volatile s64 pavo_exit_code;
char pavo_exit_reason[128];

struct pavo_task_state {
    u64 wake_rate_q8;
    u64 block_rate_q8;
    u64 runtime_ns;
    u64 last_wake_ns;
    u64 last_block_ns;
    u64 last_run_ns;
    u64 nvcsw;
    u64 deadline;
    u32 weight;
    u32 sched_prio;
    u32 android_roles;
    u32 last_waker_roles;
    s32 last_waker_pid;
    s32 last_waker_tgid;
    s32 selected_cpu;
    bool wake_chain;
    bool rendering_role;
};

struct pavo_task_mask {
    u64 allowed;
    u64 eligible_high;
};

struct pavo_global_state {
    struct bpf_spin_lock lock;
    u64 score_reference_q8;
    u64 occupancy_q8;
    u64 vtime;
    bool high_load;
};

struct pavo_cpu_state {
    u64 high_capacity_mask;
    u64 avg_score_q8;
    u64 avg_weight_q8;
    u64 shared_enqueues;
    u64 local_enqueues;
    u64 rapid_requests;
    u64 idle_decays;
    u32 capacity;
    u32 cache_group;
    u32 candidate_group;
    u32 policy_id;
    u32 cpuperf_target;
    u32 placement_cursor;
    bool cpuperf_initialized;
};

struct pavo_policy_state { u64 cpus; };
struct pavo_gate_state { u64 reference_q8; u64 vtime; bool high_load; };

/* Task-object keys remain stable across PID changes. */
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, PAVO_MAX_TASKS);
    __type(key, u64);
    __type(value, struct pavo_task_state);
} pavo_tasks SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, PAVO_MAX_TASKS);
    __type(key, u64);
    __type(value, struct pavo_task_mask);
} pavo_masks SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, u32);
    __type(value, struct pavo_global_state);
} pavo_global SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, u32);
    __type(value, struct pavo_cpu_state);
} pavo_cpus SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, PAVO_MAX_CPUS);
    __type(key, u32);
    __type(value, struct pavo_policy_state);
} pavo_policies SEC(".maps");

static __always_inline struct pavo_task_state *pavo_task(struct task_struct *p)
{
    u64 key = (u64)p;
    return bpf_map_lookup_elem(&pavo_tasks, &key);
}

static __always_inline struct pavo_task_mask *pavo_mask(struct task_struct *p)
{
    u64 key = (u64)p;
    return bpf_map_lookup_elem(&pavo_masks, &key);
}

static __always_inline struct pavo_cpu_state *pavo_cpu(void)
{
    u32 zero = 0;
    return bpf_map_lookup_elem(&pavo_cpus, &zero);
}

static __always_inline u64 pavo_current_slice_ns(void)
{
    u32 utilization = pavo_slice_utilization;
    /* The loader starts with SCX_SLICE_DFL before its first sample. */
    if (utilization == PAVO_UTIL_NOT_SAMPLED)
        return SCX_SLICE_DFL;
    return utilization == PAVO_UTIL_OVERUTIL ?
           pavo_slice_overutil_ns : pavo_slice_underutil_ns;
}

static __always_inline struct pavo_gate_state pavo_gate_snapshot(u32 score, bool update)
{
    struct pavo_gate_state snapshot = {};
    u32 zero = 0;
    struct pavo_global_state *state = bpf_map_lookup_elem(&pavo_global, &zero);
    if (!state)
        return snapshot;
    bpf_spin_lock(&state->lock);
    if (update)
        state->score_reference_q8 = pavo_threshold_update(state->score_reference_q8, score);
    snapshot.reference_q8 = state->score_reference_q8;
    snapshot.high_load = state->high_load;
    snapshot.vtime = state->vtime;
    bpf_spin_unlock(&state->lock);
    return snapshot;
}

static __always_inline bool pavo_comm_equal(const char *comm, const char *name)
{
    /* Task comm is bounded to 16 bytes. */
    for (u32 i = 0; i < 16; i++) {
        if (comm[i] != name[i]) return false;
        if (!name[i]) return true;
    }
    return false;
}

static __always_inline bool pavo_comm_has_prefix(const char *comm, const char *prefix)
{
    /* TASK_COMM_LEN-bounded, case-sensitive prefix matching. */
    for (u32 i = 0; i < 16; i++) {
        if (!prefix[i]) return true;
        if (comm[i] != prefix[i]) return false;
    }
    return false;
}

static __always_inline u32 pavo_read_android_roles(struct task_struct *p)
{
    if (!p || p->pid <= 0 || p->tgid <= 0 || (p->flags & PAVO_PF_KTHREAD))
        return PAVO_ROLE_NONE;
    u32 roles = PAVO_ROLE_NONE;
    const struct cred *cred = BPF_CORE_READ(p, real_cred);
    /* Application process leaders are Main/UI candidates. */
    if (p->pid == p->tgid && cred && BPF_CORE_READ(cred, uid.val) >= 10000)
        roles |= PAVO_ROLE_MAIN_UI_CANDIDATE;
    char comm[16] = {};
    if (bpf_core_read_str(comm, sizeof(comm), &p->comm) <= 0)
        return roles;
    if (pavo_comm_equal(comm, "RenderThread"))
        roles |= PAVO_ROLE_RENDER_THREAD;
    if (pavo_comm_has_prefix(comm, "hwuiTask"))
        roles |= PAVO_ROLE_HWUI_TASK;
    if (pavo_comm_equal(comm, "InputDispatcher"))
        roles |= PAVO_ROLE_INPUT_DISPATCHER;
    return roles;
}

static __always_inline bool pavo_rendering_role(u32 roles)
{
    /* Multiple matching hints still contribute only one five-point bonus. */
    return roles & (PAVO_ROLE_MAIN_UI_CANDIDATE | PAVO_ROLE_RENDER_THREAD |
                    PAVO_ROLE_HWUI_TASK);
}

static __always_inline bool pavo_waker_role(u32 roles)
{
    /* InputDispatcher contributes through observed wakeups. */
    return pavo_rendering_role(roles) ||
           (roles & PAVO_ROLE_INPUT_DISPATCHER);
}

static __always_inline void pavo_clear_waker_context(struct pavo_task_state *task)
{
    task->last_waker_pid = 0;
    task->last_waker_tgid = 0;
    task->last_waker_roles = PAVO_ROLE_NONE;
    task->wake_chain = false;
}

static __always_inline void pavo_recompute_score(struct task_struct *p,
                                               struct pavo_task_state *task)
{
    task->weight = p->scx.weight;
    task->nvcsw = BPF_CORE_READ(p, nvcsw);
    task->android_roles = pavo_read_android_roles(p);
    task->rendering_role = pavo_rendering_role(task->android_roles);
    task->wake_chain = task->last_waker_pid > 0 &&
                       pavo_waker_role(task->last_waker_roles);
    task->sched_prio = pavo_score(task->block_rate_q8, task->wake_rate_q8,
        task->runtime_ns, task->weight, task->nvcsw, task->rendering_role, task->wake_chain);
}

static __always_inline struct pavo_task_mask pavo_affinity(const struct cpumask *allowed)
{
    struct pavo_task_mask mask = {};
    /* Iterate over CPUs to cache the allowed mask. */
    (void)bpf_iter_num_destroy;
    struct bpf_iter_num it __attribute__((cleanup(bpf_iter_num_destroy)));
    bpf_iter_num_new(&it, 0, PAVO_MAX_CPUS);
    int *index;
    while ((index = bpf_iter_num_next(&it))) {
        u32 cpu = (u32)*index;
        if (cpu >= pavo_nr_cpu_ids) break;
        if ((pavo_online_mask & (1ULL << cpu)) && bpf_cpumask_test_cpu(cpu, allowed))
            mask.allowed |= 1ULL << cpu;
    }
    mask.eligible_high = mask.allowed & pavo_high_capacity_mask;
    return mask;
}

static __always_inline void pavo_set_cpuperf(struct pavo_cpu_state *cpu, u32 target)
{
    if (!cpu->cpuperf_initialized || target != cpu->cpuperf_target) {
        /* Submit a per-CPU performance demand. */
        scx_bpf_cpuperf_set(bpf_get_smp_processor_id(), target);
        cpu->cpuperf_target = target;
        cpu->cpuperf_initialized = true;
    }
}

static __always_inline u64 pavo_candidates(u64 eligible, s32 previous)
{
    if (previous < 0 || previous >= (s32)pavo_nr_cpu_ids)
        return eligible;
    return pavo_prefer_group(eligible, pavo_candidate_mask[(u32)previous & 63]);
}

static __always_inline bool pavo_cpu_in_mask(u64 mask, s32 cpu)
{
    return cpu >= 0 && cpu < (s32)pavo_nr_cpu_ids && cpu < PAVO_MAX_CPUS &&
           (mask & (1ULL << ((u32)cpu & 63)));
}

static __always_inline bool pavo_has_idle_cpu(u64 mask, const struct cpumask *idle)
{
    (void)bpf_iter_num_destroy;
    struct bpf_iter_num it __attribute__((cleanup(bpf_iter_num_destroy)));
    bpf_iter_num_new(&it, 0, PAVO_MAX_CPUS);
    int *index;
    while ((index = bpf_iter_num_next(&it))) {
        u32 cpu = (u32)*index;
        if (cpu >= pavo_nr_cpu_ids) break;
        if ((mask & (1ULL << cpu)) && bpf_cpumask_test_cpu(cpu, idle))
            return true;
    }
    return false;
}

static __always_inline s32 pavo_pick_idle_candidate(struct task_struct *p,
    u64 allowed, u64 candidates, const struct cpumask *idle, u64 flags, u32 cursor)
{
    if (candidates == allowed)
        return scx_bpf_pick_idle_cpu(p->cpus_ptr, flags);

    /* Candidate groups narrow the search before claiming an idle CPU. */
    (void)bpf_iter_num_destroy;
    struct bpf_iter_num it __attribute__((cleanup(bpf_iter_num_destroy)));
    bpf_iter_num_new(&it, 0, PAVO_MAX_CPUS);
    int *index;
    while ((index = bpf_iter_num_next(&it))) {
        u32 cpu = (cursor + (u32)*index) & 63;
        if (pavo_cpu_in_mask(candidates, cpu) &&
            bpf_cpumask_test_cpu(cpu, idle) && scx_bpf_test_and_clear_cpu_idle(cpu))
            return cpu;
    }
    return -1;
}

static __always_inline s32 pavo_select_shared_cpu(struct task_struct *p,
    s32 previous, u64 wake_flags, u64 allowed, u32 cursor)
{
    u64 candidates = pavo_candidates(allowed, previous);
    if (!candidates)
        return previous;
    if (!(allowed & (allowed - 1)))
        return pavo_first_cpu(allowed);

    bool prev_allowed = pavo_cpu_in_mask(candidates, previous);
    const struct cpumask *idle = scx_bpf_get_idle_cpumask();
    s32 target = -1;
    if (wake_flags & SCX_WAKE_SYNC) {
        /* Preserve the single-domain preference: idle previous CPU, then waker. */
        if (prev_allowed && scx_bpf_test_and_clear_cpu_idle(previous)) {
            target = previous;
            goto out_idle;
        }
        s32 waker_cpu = bpf_get_smp_processor_id();
        struct task_struct *waker = bpf_get_current_task_btf();
        u64 group = pavo_candidate_mask[(u32)waker_cpu & 63];
        if (!group) group = pavo_online_mask;
        if (pavo_cpu_in_mask(candidates, waker_cpu) && waker &&
            !(waker->flags & PAVO_PF_EXITING) && pavo_has_idle_cpu(group, idle) &&
            scx_bpf_dsq_nr_queued(SCX_DSQ_LOCAL_ON | waker_cpu) == 0) {
            target = waker_cpu;
            goto out_idle;
        }
    }

    const struct cpumask *idle_cores = scx_bpf_get_idle_smtmask();
    if (prev_allowed && bpf_cpumask_test_cpu(previous, idle_cores) &&
        scx_bpf_test_and_clear_cpu_idle(previous)) {
        target = previous;
        goto out_cores;
    }
    target = pavo_pick_idle_candidate(p, allowed, candidates, idle_cores,
                                      SCX_PICK_IDLE_CORE, cursor);
    if (target >= 0)
        goto out_cores;
    if (prev_allowed && scx_bpf_test_and_clear_cpu_idle(previous)) {
        target = previous;
        goto out_cores;
    }
    target = pavo_pick_idle_candidate(p, allowed, candidates, idle, 0, cursor);
    if (target < 0)
        target = prev_allowed ? previous : (s32)pavo_pick_cpu(candidates, cursor);
out_cores:
    pavo_put_idle_cpumask(idle_cores);
out_idle:
    pavo_put_idle_cpumask(idle);
    return target;
}

static __always_inline s32 pavo_idle_wake_cpu(struct task_struct *p, s32 selected)
{
    const struct cpumask *idle = scx_bpf_get_idle_cpumask();
    s32 target = -1;

    if (selected >= 0 && selected < (s32)pavo_nr_cpu_ids &&
        bpf_cpumask_test_cpu(selected, p->cpus_ptr) &&
        bpf_cpumask_test_cpu(selected, idle)) {
        target = selected;
    } else {
        u32 candidate = bpf_cpumask_any_and_distribute(p->cpus_ptr, idle);
        if (candidate < pavo_nr_cpu_ids)
            target = candidate;
    }
    pavo_put_idle_cpumask(idle);
    return target;
}

s32 PAVO_STRUCT_OPS(pavo_select_cpu, struct task_struct *p, s32 previous, u64 wake_flags)
{
    struct pavo_task_state *task = pavo_task(p);
    struct pavo_cpu_state *cpu = pavo_cpu();
    struct pavo_task_mask *mask = pavo_mask(p);
    struct pavo_task_mask affinity = pavo_affinity(p->cpus_ptr);
    if (mask) *mask = affinity;
    if (task && mask && cpu) {
        struct pavo_gate_state gate = pavo_gate_snapshot(0, false);
        if (pavo_gate(task->sched_prio, gate.reference_q8, gate.high_load)) {
            /* Select from the intersection of affinity and higher-capacity CPUs. */
            u64 eligible = mask->allowed & cpu->high_capacity_mask;
            eligible = pavo_candidates(eligible, previous);
            u32 target = pavo_pick_cpu(eligible, cpu->placement_cursor);
            if (target < PAVO_MAX_CPUS) {
                task->selected_cpu = target;
                return target;
            }
        }
    }
    s32 target = pavo_select_shared_cpu(p, previous, wake_flags, affinity.allowed,
                                        cpu ? cpu->placement_cursor : 0);
    if (task) task->selected_cpu = target;
    /* A wake candidate only: ordinary tasks still enter the Shared DSQ. */
    return target;
}

void PAVO_STRUCT_OPS(pavo_enqueue, struct task_struct *p, u64 flags)
{
    struct pavo_task_state *task = pavo_task(p);
    struct pavo_cpu_state *cpu = pavo_cpu();
    u64 base_slice_ns = pavo_current_slice_ns();
    if (!task) {
        struct pavo_gate_state fallback = pavo_gate_snapshot(0, false);
        pavo_dsq_insert_vtime(p, PAVO_SHARED_DSQ, base_slice_ns, fallback.vtime, flags);
        return;
    }
    pavo_recompute_score(p, task);
    u64 slice_ns = pavo_slice(base_slice_ns, task->sched_prio);
    struct pavo_gate_state gate = pavo_gate_snapshot(task->sched_prio, true);
    struct pavo_task_mask *mask = pavo_mask(p);
    if (cpu && mask && pavo_gate(task->sched_prio, gate.reference_q8, gate.high_load)) {
        u64 eligible = mask->eligible_high & cpu->high_capacity_mask;
        eligible = pavo_candidates(eligible, task->selected_cpu);
        u32 target = pavo_pick_cpu(eligible, cpu->placement_cursor);
        if (target < PAVO_MAX_CPUS && bpf_cpumask_test_cpu(target, p->cpus_ptr)) {
            cpu->placement_cursor = (target + 1) & 63;
            cpu->local_enqueues++;
            pavo_dsq_insert(p, SCX_DSQ_LOCAL_ON | target, slice_ns, flags);
            scx_bpf_kick_cpu(target, SCX_KICK_IDLE);
            return;
        }
    }
    /* Shared DSQ is ordered by virtual deadline. */
    if ((s64)(p->scx.dsq_vtime - (gate.vtime - base_slice_ns)) < 0) {
        p->scx.dsq_vtime = gate.vtime - base_slice_ns;
        task->deadline = p->scx.dsq_vtime +
                         pavo_deadline_delta(task->runtime_ns, task->sched_prio);
    }
    /* Reuse the deadline until stopping or the sleep-budget clamp refreshes it.
     * Insertion writes that deadline back to the kernel's dsq_vtime. */
    pavo_dsq_insert_vtime(p, PAVO_SHARED_DSQ, slice_ns, task->deadline, flags);
    if (cpu) cpu->shared_enqueues++;
    /* Wake an eligible idle CPU to pull from the shared queue. */
    s32 wake_cpu = pavo_idle_wake_cpu(p, task->selected_cpu);
    /* select_cpu may already have claimed its candidate's idle bit. */
    if (wake_cpu < 0)
        wake_cpu = task->selected_cpu;
    if (mask && (wake_cpu < 0 || wake_cpu >= PAVO_MAX_CPUS ||
        !(mask->allowed & (1ULL << (wake_cpu & 63)))))
        wake_cpu = pavo_pick_cpu(mask->allowed, 0);
    if (wake_cpu >= 0 && wake_cpu < (s32)pavo_nr_cpu_ids)
        scx_bpf_kick_cpu(wake_cpu, SCX_KICK_IDLE);
}

void PAVO_STRUCT_OPS(pavo_dispatch, s32 cpu_id, struct task_struct *previous)
{
    if (pavo_dsq_move_to_local(PAVO_SHARED_DSQ))
        return;
    /* Give the still-runnable previous task a bounded slice. */
    if (previous && (previous->scx.flags & SCX_TASK_QUEUED)) {
        struct pavo_task_state *task = pavo_task(previous);
        u64 base_slice_ns = pavo_current_slice_ns();
        previous->scx.slice = task ? pavo_slice(base_slice_ns, task->sched_prio) :
                                    base_slice_ns;
        return;
    }
    if (scx_bpf_dsq_nr_queued(SCX_DSQ_LOCAL) > 0 ||
        scx_bpf_dsq_nr_queued(PAVO_SHARED_DSQ) > 0)
        return;
    struct pavo_cpu_state *cpu = pavo_cpu();
    if (cpu) {
        u32 target = cpu->cpuperf_target > PAVO_PERF_DECAY_STEP ?
                     cpu->cpuperf_target - PAVO_PERF_DECAY_STEP : 0;
        cpu->idle_decays++;
        pavo_set_cpuperf(cpu, target);
    }
}

void PAVO_STRUCT_OPS(pavo_runnable, struct task_struct *p, u64 flags)
{
    struct pavo_task_state *task = pavo_task(p);
    if (!task) return;
    /* Reset wake context at the start of each runnable episode. */
    pavo_clear_waker_context(task);
    if (flags & SCX_ENQ_WAKEUP) {
        u64 now = bpf_ktime_get_ns();
        struct task_struct *waker = bpf_get_current_task_btf();
        if (waker && waker->pid > 0 && waker->tgid > 0 && waker->pid != p->pid) {
            task->last_waker_pid = waker->pid;
            task->last_waker_tgid = waker->tgid;
            /* Snapshot the observed waker context, including cross-process IPC. */
            task->last_waker_roles = pavo_read_android_roles(waker);
            struct pavo_task_state *producer = pavo_task(waker);
            if (producer) {
                if (producer->last_wake_ns && now > producer->last_wake_ns)
                    producer->wake_rate_q8 = pavo_rate_update(producer->wake_rate_q8, now - producer->last_wake_ns);
                producer->last_wake_ns = now;
                pavo_recompute_score(waker, producer);
            }
        }
    }
    pavo_recompute_score(p, task);
}

void PAVO_STRUCT_OPS(pavo_running, struct task_struct *p)
{
    struct pavo_task_state *task = pavo_task(p);
    struct pavo_cpu_state *cpu = pavo_cpu();
    if (!task) return;
    task->last_run_ns = bpf_ktime_get_ns();
    u32 zero = 0;
    struct pavo_global_state *global = bpf_map_lookup_elem(&pavo_global, &zero);
    if (global) {
        bpf_spin_lock(&global->lock);
        if ((s64)(p->scx.dsq_vtime - global->vtime) > 0)
            global->vtime = p->scx.dsq_vtime;
        bpf_spin_unlock(&global->lock);
    }
    struct pavo_gate_state gate = pavo_gate_snapshot(0, false);
    if (cpu && pavo_gate(task->sched_prio, gate.reference_q8, gate.high_load)) {
        u32 target = pavo_cpuperf_from_score(task->sched_prio);
        if (target > cpu->cpuperf_target) {
            cpu->rapid_requests++;
            pavo_set_cpuperf(cpu, target);
        }
    }
}

void PAVO_STRUCT_OPS(pavo_stopping, struct task_struct *p, bool runnable)
{
    struct pavo_task_state *task = pavo_task(p);
    if (!task) return;
    u64 now = bpf_ktime_get_ns();
    if (task->last_run_ns && now > task->last_run_ns) {
        u64 delta = now - task->last_run_ns;
        task->runtime_ns = pavo_behavior_ewma(task->runtime_ns, delta);
        p->scx.dsq_vtime += pavo_inverse_weight(delta, p->scx.weight);
    }
    task->last_run_ns = 0;
    pavo_recompute_score(p, task);
    task->deadline = p->scx.dsq_vtime +
                     pavo_deadline_delta(task->runtime_ns, task->sched_prio);
}

void PAVO_STRUCT_OPS(pavo_quiescent, struct task_struct *p, u64 flags)
{
    struct pavo_task_state *task = pavo_task(p);
    if (!task) return;
    if (flags & SCX_DEQ_SLEEP) {
        u64 now = bpf_ktime_get_ns();
        if (task->last_block_ns && now > task->last_block_ns)
            task->block_rate_q8 = pavo_rate_update(task->block_rate_q8, now - task->last_block_ns);
        task->last_block_ns = now;
    }
    pavo_clear_waker_context(task);
    pavo_recompute_score(p, task);
}

void PAVO_STRUCT_OPS(pavo_tick, struct task_struct *p)
{
    u32 zero = 0;
    s32 queued = scx_bpf_dsq_nr_queued(PAVO_SHARED_DSQ);
    struct pavo_global_state *global = bpf_map_lookup_elem(&pavo_global, &zero);
    if (global && queued >= 0) {
        bpf_spin_lock(&global->lock);
        global->occupancy_q8 = pavo_occupancy_update(global->occupancy_q8, queued);
        global->high_load = pavo_load_mode(global->occupancy_q8, global->high_load);
        bpf_spin_unlock(&global->lock);
    }
    struct pavo_task_state *task = pavo_task(p);
    struct pavo_cpu_state *cpu = pavo_cpu();
    if (!task || !cpu) return;
    struct pavo_gate_state gate = pavo_gate_snapshot(0, false);
    u32 target;
    if (gate.high_load) {
        cpu->avg_score_q8 = pavo_behavior_ewma(cpu->avg_score_q8, (u64)task->sched_prio << 8);
        target = pavo_cpuperf_from_score(cpu->avg_score_q8 >> 8);
    } else {
        cpu->avg_weight_q8 = pavo_behavior_ewma(cpu->avg_weight_q8, (u64)task->weight << 8);
        target = pavo_cpuperf_from_weight(cpu->avg_weight_q8 >> 8);
    }
    pavo_set_cpuperf(cpu, target);
}

void PAVO_STRUCT_OPS(pavo_set_weight, struct task_struct *p, u32 weight)
{
    struct pavo_task_state *task = pavo_task(p);
    if (task) task->weight = weight;
}

void PAVO_STRUCT_OPS(pavo_set_cpumask, struct task_struct *p, const struct cpumask *allowed)
{
    struct pavo_task_mask *mask = pavo_mask(p);
    if (mask) *mask = pavo_affinity(allowed);
}

s32 PAVO_STRUCT_OPS_SLEEPABLE(pavo_init_task, struct task_struct *p, struct scx_init_task_args *args)
{
    u64 key = (u64)p;
    struct pavo_task_state initial = { .weight = p->scx.weight, .selected_cpu = -1 };
    /* Seed vtime with an unlocked snapshot in this sleepable callback. */
    u32 zero = 0;
    struct pavo_global_state *global = bpf_map_lookup_elem(&pavo_global, &zero);
    p->scx.dsq_vtime = global ? global->vtime : 0;
    initial.deadline = p->scx.dsq_vtime;
    bpf_rcu_read_lock();
    struct pavo_task_mask mask = pavo_affinity(p->cpus_ptr);
    bpf_rcu_read_unlock();
    if (bpf_map_lookup_elem(&pavo_tasks, &key) ||
        bpf_map_lookup_elem(&pavo_masks, &key))
        return -17;
    long result = bpf_map_update_elem(&pavo_tasks, &key, &initial, BPF_NOEXIST);
    if (result) return result;
    result = bpf_map_update_elem(&pavo_masks, &key, &mask, BPF_NOEXIST);
    if (result) bpf_map_delete_elem(&pavo_tasks, &key);
    return result;
}

void PAVO_STRUCT_OPS(pavo_exit_task, struct task_struct *p, struct scx_exit_task_args *args)
{
    u64 key = (u64)p;
    bpf_map_delete_elem(&pavo_masks, &key);
    bpf_map_delete_elem(&pavo_tasks, &key);
}

s32 PAVO_STRUCT_OPS_SLEEPABLE(pavo_init)
{
    if (!pavo_nr_cpu_ids || pavo_nr_cpu_ids > PAVO_MAX_CPUS || !pavo_online_mask)
        return -22;
    if (!pavo_slice_underutil_ns || pavo_slice_underutil_ns > PAVO_SLICE_BASE_MAX_NS ||
        !pavo_slice_overutil_ns || pavo_slice_overutil_ns > PAVO_SLICE_BASE_MAX_NS)
        return -22;
    s32 result = scx_bpf_create_dsq(PAVO_SHARED_DSQ, -1);
    if (result) return result;
    u32 zero = 0;
    for (u32 cpu = 0; cpu < PAVO_MAX_CPUS; cpu++) {
        if (cpu >= pavo_nr_cpu_ids) break;
        if (!(pavo_online_mask & (1ULL << cpu))) continue;
        struct pavo_cpu_state *state = bpf_map_lookup_percpu_elem(&pavo_cpus, &zero, cpu);
        if (!state) return -2;
        state->high_capacity_mask = pavo_high_capacity_mask;
        state->capacity = pavo_capacity[cpu];
        state->cache_group = pavo_cache_group[cpu];
        state->candidate_group = pavo_candidate_group[cpu];
        state->policy_id = pavo_policy_id[cpu];
        state->placement_cursor = cpu;
    }
    for (u32 id = 0; id < PAVO_MAX_CPUS; id++) {
        if (id >= pavo_nr_policies) break;
        struct pavo_policy_state policy = { .cpus = pavo_policy_mask[id] };
        u32 key = id;
        result = bpf_map_update_elem(&pavo_policies, &key, &policy, BPF_ANY);
        if (result) return result;
    }
    return 0;
}

void PAVO_STRUCT_OPS(pavo_exit, struct scx_exit_info *info)
{
    pavo_exit_kind = info->kind;
    pavo_exit_code = info->exit_code;
    bpf_probe_read_kernel_str(pavo_exit_reason, sizeof(pavo_exit_reason), info->reason);
    asm volatile ("" ::: "memory");
    pavo_exited = 1;
}

SEC(".struct_ops.link")
struct sched_ext_ops pavo_ops = {
    .select_cpu = (void *)pavo_select_cpu,
    .enqueue = (void *)pavo_enqueue,
    .dispatch = (void *)pavo_dispatch,
    .runnable = (void *)pavo_runnable,
    .running = (void *)pavo_running,
    .stopping = (void *)pavo_stopping,
    .quiescent = (void *)pavo_quiescent,
    .tick = (void *)pavo_tick,
    .set_weight = (void *)pavo_set_weight,
    .set_cpumask = (void *)pavo_set_cpumask,
    .init_task = (void *)pavo_init_task,
    .exit_task = (void *)pavo_exit_task,
    .init = (void *)pavo_init,
    .exit = (void *)pavo_exit,
    .timeout_ms = 10000,
    .name = "pavo",
};
