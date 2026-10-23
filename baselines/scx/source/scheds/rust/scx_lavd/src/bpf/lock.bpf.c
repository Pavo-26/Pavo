/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Copyright (c) 2024 Valve Corporation.
 * Author: Changwoo Min <changwoo@igalia.com>
 */

/*
 * To be included to the main.bpf.c
 */

static void inc_futex_boost(u32 *uaddr)
{
	struct task_struct *p = bpf_get_current_task_btf();
	struct task_ctx *taskc = try_get_task_ctx(p);
	const u32 cpu_key = 0;
	/* Kprobes cannot call the sched_ext-only error kfunc in get_cpu_ctx(). */
	struct cpu_ctx *cpuc = bpf_map_lookup_elem(&cpu_ctx_stor, &cpu_key);

	if (taskc && cpuc && (taskc->futex_uaddr != uaddr)) {
		taskc->futex_boost++;
		taskc->futex_uaddr = uaddr;
		cpuc->lock_holder = is_lock_holder(taskc);
	}
	/*
	 * If taskc is null, the task is not under sched_ext so ignore the error.
	 */
}

static void dec_futex_boost(u32 *uaddr)
{
	struct task_struct *p = bpf_get_current_task_btf();
	struct task_ctx *taskc = try_get_task_ctx(p);
	const u32 cpu_key = 0;
	struct cpu_ctx *cpuc = bpf_map_lookup_elem(&cpu_ctx_stor, &cpu_key);

	if (p && taskc && cpuc && taskc->futex_boost > 0) {
		taskc->futex_boost--;
		taskc->futex_uaddr = NULL;
		cpuc->lock_holder = is_lock_holder(taskc);
	}
	/*
	 * If taskc is null, the task is not under sched_ext so ignore the error.
	 */
}

static void reset_lock_futex_boost(struct task_ctx *taskc, struct cpu_ctx *cpuc)
{
	if (taskc && cpuc) {
		if (is_lock_holder(taskc))
			taskc->need_lock_boost = true;

		taskc->futex_boost = 0;
		taskc->futex_uaddr = NULL;
		cpuc->lock_holder = false;
	}
}

/**
 * Futex for userspace synchronization primiteves (kernel/futex/)
 *
 * Trace futex_wait() and futex_wake() variants similar as in-kernel lock
 * and unlock. However, in the case of futex, the user-level implementation,
 * like NTPL, can skip futex_wait() and futex_wake() for performance
 * optimization to reduce syscall overhead. Hence, tracing only the 
 * kernel-side futex calls reveals incomplete user-level lock status. Also,
 * futex's spurious wake-up further complicates the problem; a lock holder
 * can call futex_wait() more than once for a single lock acquisition.
 * One potential approach would be using uprobe to directly hook a posix
 * library calls. However, it would incur high overhead since uprobe is based
 * on a trap causing context switching, diminishing the optimization of
 * a user-level lock implementation.
 *
 * Our approximated approach is as follows:
 * - When a futex_wait() call is skipped in the lock acquisition path, that
 *   means there is no waiter -- the lock is not contending. Hence, we don't
 *   need to boost the lock holder, so we don't care.
 * - When a task calls futex_wait() more than once before calling futex_wake(),
 *   that means a task reties futex_wait() after the spurious wake-up. Hence,
 *   we can safely ignore the second futex_wait() call onwards.
 * - When a futex_wake() is skipped, it indicates that there is no waiter and
 *   the lock is not contending. However, it also means we cannot determine
 *   when the user-space lock is released. Here, we assume that a reasonably
 *   designed critical section is short enough and too long a critical section
 *   is not worth boosting. So when a futex_wake() is not called within a one
 *   time slice, we assume futex_wake() is skipped.
 *
 * We trace the folloing futex calls:
 * - int __futex_wait(u32 *uaddr, unsigned int flags, u32 val, struct hrtimer_sleeper *to, u32 bitset)
 * - int futex_wait_multiple(struct futex_vector *vs, unsigned int count, struct hrtimer_sleeper *to)
 * - int futex_wait_requeue_pi(u32 *uaddr, unsigned int flags, u32 val, ktime_t *abs_time, u32 bitset, u32 *uaddr2)
 *
 * - int futex_wake(u32 *uaddr, unsigned int flags, int nr_wake, u32 bitset)
 * - int futex_wake_op(u32 *uaddr1, unsigned int flags, u32 *uaddr2, int nr_wake, int nr_wake2, int op)
 *
 * - int futex_lock_pi(u32 *uaddr, unsigned int flags, ktime_t *time, int trylock)
 * - int futex_unlock_pi(u32 *uaddr, unsigned int flags)
 */
struct futex_vector;
struct hrtimer_sleeper;

/* Function signatures for kretprobe context access */
struct __futex_wait_ctx {
    u32 *uaddr;
    unsigned int flags;
    u32 val;
    struct hrtimer_sleeper *to;
    u32 bitset;
};

struct futex_wait_multiple_ctx {
    struct futex_vector *vs;
    unsigned int count;
    struct hrtimer_sleeper *to;
};

struct futex_wait_requeue_pi_ctx {
    u32 *uaddr;
    unsigned int flags;
    u32 val;
    ktime_t *abs_time;
    u32 bitset;
    u32 *uaddr2;
};

struct futex_wake_ctx {
    u32 *uaddr;
    unsigned int flags;
    int nr_wake;
    u32 bitset;
};

struct futex_wake_op_ctx {
    u32 *uaddr1;
    unsigned int flags;
    u32 *uaddr2;
    int nr_wake;
    int nr_wake2;
    int op;
};

struct futex_lock_pi_ctx {
    u32 *uaddr;
    unsigned int flags;
    ktime_t *time;
    int trylock;
};

struct futex_unlock_pi_ctx {
    u32 *uaddr;
    unsigned int flags;
};

/* Used to store context between entry and return */
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1024);
    __type(key, u64);
    __type(value, struct __futex_wait_ctx);
} futex_wait_ctx_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1024);
    __type(key, u64);
    __type(value, struct futex_wait_multiple_ctx);
} futex_wait_multiple_ctx_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1024);
    __type(key, u64);
    __type(value, struct futex_wait_requeue_pi_ctx);
} futex_wait_requeue_pi_ctx_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1024);
    __type(key, u64);
    __type(value, struct futex_wake_ctx);
} futex_wake_ctx_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1024);
    __type(key, u64);
    __type(value, struct futex_wake_op_ctx);
} futex_wake_op_ctx_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1024);
    __type(key, u64);
    __type(value, struct futex_lock_pi_ctx);
} futex_lock_pi_ctx_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1024);
    __type(key, u64);
    __type(value, struct futex_unlock_pi_ctx);
} futex_unlock_pi_ctx_map SEC(".maps");

/* Entry points to save context */
SEC("kprobe/__futex_wait")
int BPF_KPROBE(kprobe___futex_wait, u32 *uaddr, unsigned int flags, u32 val, struct hrtimer_sleeper *to, u32 bitset)
{
    u64 pid_tgid = bpf_get_current_pid_tgid();
    struct __futex_wait_ctx data_ctx = {
        .uaddr = uaddr,
        .flags = flags,
        .val = val,
        .to = to,
        .bitset = bitset
    };
    
    bpf_map_update_elem(&futex_wait_ctx_map, &pid_tgid, &data_ctx, BPF_ANY);
    return 0;
}

SEC("kretprobe/__futex_wait")
int BPF_KRETPROBE(kretprobe___futex_wait, int ret)
{
    u64 pid_tgid = bpf_get_current_pid_tgid();
    struct __futex_wait_ctx *data_ctx = bpf_map_lookup_elem(&futex_wait_ctx_map, &pid_tgid);
    
    if (data_ctx && ret == 0) {
        /*
         * A futex is acquired.
         */
        inc_futex_boost(data_ctx->uaddr);
    }
    
    bpf_map_delete_elem(&futex_wait_ctx_map, &pid_tgid);
    return 0;
}

SEC("kprobe/futex_wait_multiple")
int BPF_KPROBE(kprobe_futex_wait_multiple, struct futex_vector *vs, unsigned int count, struct hrtimer_sleeper *to)
{
    u64 pid_tgid = bpf_get_current_pid_tgid();
    struct futex_wait_multiple_ctx data_ctx = {
        .vs = vs,
        .count = count,
        .to = to
    };
    
    bpf_map_update_elem(&futex_wait_multiple_ctx_map, &pid_tgid, &data_ctx, BPF_ANY);
    return 0;
}

SEC("kretprobe/futex_wait_multiple")
int BPF_KRETPROBE(kretprobe_futex_wait_multiple, int ret)
{
    u64 pid_tgid = bpf_get_current_pid_tgid();
    
    if (ret == 0) {
        /*
         * All of futexes are acquired.
         *
         * We don't want to traverse futex_vector here since that's
         * a userspace address. Hence we just pass an invalid adderess
         * to consider all futex_waitv() calls are for the same address.
         * Thit is a conservative approximation boosting less.
         */
        inc_futex_boost((u32 *)0xbeefcafe); 
    }
    
    bpf_map_delete_elem(&futex_wait_multiple_ctx_map, &pid_tgid);
    return 0;
}

SEC("kprobe/futex_wait_requeue_pi")
int BPF_KPROBE(kprobe_futex_wait_requeue_pi, u32 *uaddr, unsigned int flags, u32 val, ktime_t *abs_time, u32 bitset, u32 *uaddr2)
{
    u64 pid_tgid = bpf_get_current_pid_tgid();
    struct futex_wait_requeue_pi_ctx data_ctx = {
        .uaddr = uaddr,
        .flags = flags,
        .val = val,
        .abs_time = abs_time,
        .bitset = bitset,
        .uaddr2 = uaddr2
    };
    
    bpf_map_update_elem(&futex_wait_requeue_pi_ctx_map, &pid_tgid, &data_ctx, BPF_ANY);
    return 0;
}

SEC("kretprobe/futex_wait_requeue_pi")
int BPF_KRETPROBE(kretprobe_futex_wait_requeue_pi, int ret)
{
    u64 pid_tgid = bpf_get_current_pid_tgid();
    struct futex_wait_requeue_pi_ctx *data_ctx = bpf_map_lookup_elem(&futex_wait_requeue_pi_ctx_map, &pid_tgid);
    
    if (data_ctx && ret == 0) {
        /*
         * A futex is acquired.
         */
        inc_futex_boost(data_ctx->uaddr);
    }
    
    bpf_map_delete_elem(&futex_wait_requeue_pi_ctx_map, &pid_tgid);
    return 0;
}

SEC("kprobe/futex_wake")
int BPF_KPROBE(kprobe_futex_wake, u32 *uaddr, unsigned int flags, int nr_wake, u32 bitset)
{
    u64 pid_tgid = bpf_get_current_pid_tgid();
    struct futex_wake_ctx data_ctx = {
        .uaddr = uaddr,
        .flags = flags,
        .nr_wake = nr_wake,
        .bitset = bitset
    };
    
    bpf_map_update_elem(&futex_wake_ctx_map, &pid_tgid, &data_ctx, BPF_ANY);
    return 0;
}

SEC("kretprobe/futex_wake")
int BPF_KRETPROBE(kretprobe_futex_wake, int ret)
{
    u64 pid_tgid = bpf_get_current_pid_tgid();
    struct futex_wake_ctx *data_ctx = bpf_map_lookup_elem(&futex_wake_ctx_map, &pid_tgid);
    
    if (data_ctx && ret >= 0) {
        /*
         * A futex is released.
         */
        dec_futex_boost(data_ctx->uaddr);
    }
    
    bpf_map_delete_elem(&futex_wake_ctx_map, &pid_tgid);
    return 0;
}

SEC("kprobe/futex_wake_op")
int BPF_KPROBE(kprobe_futex_wake_op, u32 *uaddr1, unsigned int flags, u32 *uaddr2, int nr_wake, int nr_wake2, int op)
{
    u64 pid_tgid = bpf_get_current_pid_tgid();
    struct futex_wake_op_ctx data_ctx = {
        .uaddr1 = uaddr1,
        .flags = flags,
        .uaddr2 = uaddr2,
        .nr_wake = nr_wake,
        .nr_wake2 = nr_wake2,
        .op = op
    };
    
    bpf_map_update_elem(&futex_wake_op_ctx_map, &pid_tgid, &data_ctx, BPF_ANY);
    return 0;
}

SEC("kretprobe/futex_wake_op")
int BPF_KRETPROBE(kretprobe_futex_wake_op, int ret)
{
    u64 pid_tgid = bpf_get_current_pid_tgid();
    struct futex_wake_op_ctx *data_ctx = bpf_map_lookup_elem(&futex_wake_op_ctx_map, &pid_tgid);
    
    if (data_ctx && ret >= 0) {
        /*
         * A futex is released.
         */
        dec_futex_boost(data_ctx->uaddr1);
    }
    
    bpf_map_delete_elem(&futex_wake_op_ctx_map, &pid_tgid);
    return 0;
}

SEC("kprobe/futex_lock_pi")
int BPF_KPROBE(kprobe_futex_lock_pi, u32 *uaddr, unsigned int flags, ktime_t *time, int trylock)
{
    u64 pid_tgid = bpf_get_current_pid_tgid();
    struct futex_lock_pi_ctx data_ctx = {
        .uaddr = uaddr,
        .flags = flags,
        .time = time,
        .trylock = trylock
    };
    
    bpf_map_update_elem(&futex_lock_pi_ctx_map, &pid_tgid, &data_ctx, BPF_ANY);
    return 0;
}

SEC("kretprobe/futex_lock_pi")
int BPF_KRETPROBE(kretprobe_futex_lock_pi, int ret)
{
    u64 pid_tgid = bpf_get_current_pid_tgid();
    struct futex_lock_pi_ctx *data_ctx = bpf_map_lookup_elem(&futex_lock_pi_ctx_map, &pid_tgid);
    
    if (data_ctx && ret == 0) {
        /*
         * A futex is acquired.
         */
        inc_futex_boost(data_ctx->uaddr);
    }
    
    bpf_map_delete_elem(&futex_lock_pi_ctx_map, &pid_tgid);
    return 0;
}

SEC("kprobe/futex_unlock_pi")
int BPF_KPROBE(kprobe_futex_unlock_pi, u32 *uaddr, unsigned int flags)
{
    u64 pid_tgid = bpf_get_current_pid_tgid();
    struct futex_unlock_pi_ctx data_ctx = {
        .uaddr = uaddr,
        .flags = flags
    };
    
    bpf_map_update_elem(&futex_unlock_pi_ctx_map, &pid_tgid, &data_ctx, BPF_ANY);
    return 0;
}

SEC("kretprobe/futex_unlock_pi")
int BPF_KRETPROBE(kretprobe_futex_unlock_pi, int ret)
{
    u64 pid_tgid = bpf_get_current_pid_tgid();
    struct futex_unlock_pi_ctx *data_ctx = bpf_map_lookup_elem(&futex_unlock_pi_ctx_map, &pid_tgid);
    
    if (data_ctx && ret == 0) {
        /*
         * A futex is released.
         */
        dec_futex_boost(data_ctx->uaddr);
    }
    
    bpf_map_delete_elem(&futex_unlock_pi_ctx_map, &pid_tgid);
    return 0;
}

/**
 * TODO: NTsync driver in recent kernel (when ntsync is fully mainlined)
 * - https://lore.kernel.org/lkml/20240519202454.1192826-28-zfigura@codeweavers.com/
 * - https://github.com/torvalds/linux/blob/master/drivers/misc/ntsync.c
 * - https://www.youtube.com/watch?v=NjU4nyWyhU8
 */
