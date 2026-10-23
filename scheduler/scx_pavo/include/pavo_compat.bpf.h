/* SPDX-License-Identifier: GPL-2.0-only */
/* sched_ext declarations/macros adapted from sched-ext/scx common.bpf.h.
 * Copyright (c) 2022 Meta Platforms, Inc. and affiliates.
 * Copyright (c) 2022 Tejun Heo <tj@kernel.org>
 * Copyright (c) 2022 David Vernet <dvernet@meta.com> */
#ifndef PAVO_COMPAT_BPF_H
#define PAVO_COMPAT_BPF_H
#define BPF_NO_KFUNC_PROTOTYPES
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_tracing.h>

#define PAVO_STRUCT_OPS(name, args...) SEC("struct_ops/" #name) BPF_PROG(name, ##args)
#define PAVO_STRUCT_OPS_SLEEPABLE(name, args...) SEC("struct_ops.s/" #name) BPF_PROG(name, ##args)
#define PAVO_PF_KTHREAD 0x00200000
#define PAVO_PF_EXITING 0x00000004
#define PAVO_RODATA const volatile

s32 scx_bpf_create_dsq(u64 dsq_id, s32 node) __ksym;
s32 scx_bpf_dsq_nr_queued(u64 dsq_id) __ksym;
void scx_bpf_kick_cpu(s32 cpu, u64 flags) __ksym;
void scx_bpf_cpuperf_set(s32 cpu, u32 perf) __ksym;
bool bpf_cpumask_test_cpu(u32 cpu, const struct cpumask *mask) __ksym;
u32 bpf_cpumask_any_and_distribute(const struct cpumask *src1, const struct cpumask *src2) __ksym;
const struct cpumask *scx_bpf_get_idle_cpumask(void) __ksym;
const struct cpumask *scx_bpf_get_idle_smtmask(void) __ksym;
bool scx_bpf_test_and_clear_cpu_idle(s32 cpu) __ksym;
s32 scx_bpf_pick_idle_cpu(const struct cpumask *allowed, u64 flags) __ksym;

/* Fixed at BUILD time to avoid unresolved weak-kfunc/module-BTF probing on
 * the vendor 6.12 kernel. No runtime scheduler behavior depends on this switch. */
#ifdef PAVO_MODERN_KFUNCS
void scx_bpf_put_cpumask(const struct cpumask *mask) __ksym;
#define pavo_put_idle_cpumask scx_bpf_put_cpumask
void scx_bpf_dsq_insert(struct task_struct *p, u64 dsq, u64 slice, u64 flags) __ksym;
void scx_bpf_dsq_insert_vtime(struct task_struct *p, u64 dsq, u64 slice, u64 vtime, u64 flags) __ksym;
bool scx_bpf_dsq_move_to_local(u64 dsq) __ksym;
#define pavo_dsq_insert scx_bpf_dsq_insert
#define pavo_dsq_insert_vtime scx_bpf_dsq_insert_vtime
#define pavo_dsq_move_to_local scx_bpf_dsq_move_to_local
#else
void scx_bpf_put_idle_cpumask(const struct cpumask *mask) __ksym;
#define pavo_put_idle_cpumask scx_bpf_put_idle_cpumask
void scx_bpf_dispatch(struct task_struct *p, u64 dsq, u64 slice, u64 flags) __ksym;
void scx_bpf_dispatch_vtime(struct task_struct *p, u64 dsq, u64 slice, u64 vtime, u64 flags) __ksym;
bool scx_bpf_consume(u64 dsq) __ksym;
#define pavo_dsq_insert scx_bpf_dispatch
#define pavo_dsq_insert_vtime scx_bpf_dispatch_vtime
#define pavo_dsq_move_to_local scx_bpf_consume
#endif
#endif
