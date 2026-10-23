#define _GNU_SOURCE

#include <errno.h>
#include <inttypes.h>
#include <sched.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#ifndef SCHED_FLAG_KEEP_POLICY
#define SCHED_FLAG_KEEP_POLICY 0x08
#endif
#ifndef SCHED_FLAG_KEEP_PARAMS
#define SCHED_FLAG_KEEP_PARAMS 0x10
#endif
#ifndef SCHED_FLAG_UTIL_CLAMP_MIN
#define SCHED_FLAG_UTIL_CLAMP_MIN 0x20
#endif

struct sched_attr_local {
    uint32_t size;
    uint32_t sched_policy;
    uint64_t sched_flags;
    int32_t sched_nice;
    uint32_t sched_priority;
    uint64_t sched_runtime;
    uint64_t sched_deadline;
    uint64_t sched_period;
    uint32_t sched_util_min;
    uint32_t sched_util_max;
};

static volatile sig_atomic_t stop_requested;

static void handle_signal(int signo)
{
    (void)signo;
    stop_requested = 1;
}

static int sched_getattr_local(pid_t tid, struct sched_attr_local *attr)
{
    memset(attr, 0, sizeof(*attr));
    attr->size = sizeof(*attr);
    return syscall(SYS_sched_getattr, tid, attr, sizeof(*attr), 0);
}

static int set_floor(pid_t tid, uint32_t floor)
{
    struct sched_attr_local attr = {
        .size = sizeof(attr),
        .sched_flags = SCHED_FLAG_KEEP_POLICY | SCHED_FLAG_KEEP_PARAMS |
                       SCHED_FLAG_UTIL_CLAMP_MIN,
        .sched_util_min = floor,
    };

    return syscall(SYS_sched_setattr, tid, &attr, 0);
}

static uint64_t timespec_to_ns(const struct timespec *value)
{
    return (uint64_t)value->tv_sec * 1000000000ULL + (uint64_t)value->tv_nsec;
}

static struct timespec ns_to_timespec(uint64_t value)
{
    struct timespec result = {
        .tv_sec = (time_t)(value / 1000000000ULL),
        .tv_nsec = (long)(value % 1000000000ULL),
    };
    return result;
}

static long parse_long(const char *text, long minimum, long maximum, const char *name)
{
    char *end = NULL;
    errno = 0;
    long value = strtol(text, &end, 10);
    if (errno || !end || *end || value < minimum || value > maximum) {
        fprintf(stderr, "invalid %s: %s\n", name, text);
        exit(2);
    }
    return value;
}

int main(int argc, char **argv)
{
    if (argc < 4) {
        fprintf(stderr, "usage: %s FLOOR INTERVAL_US TID [TID ...]\n", argv[0]);
        return 2;
    }

    uint32_t floor = (uint32_t)parse_long(argv[1], 0, 1024, "floor");
    uint64_t interval_us = (uint64_t)parse_long(
        argv[2], 500, 1000000, "interval_us");
    size_t tid_count = (size_t)(argc - 3);
    pid_t *tids = calloc(tid_count, sizeof(*tids));
    if (!tids) {
        perror("calloc");
        return 2;
    }
    for (size_t index = 0; index < tid_count; index++) {
        tids[index] = (pid_t)parse_long(argv[index + 3], 1, INT32_MAX, "tid");
    }

    struct sigaction action = {
        .sa_handler = handle_signal,
    };
    sigemptyset(&action.sa_mask);
    sigaction(SIGINT, &action, NULL);
    sigaction(SIGTERM, &action, NULL);

    uint64_t affinity_errors = 0;
    uint64_t priority_errors = 0;
    uint64_t realtime_errors = 0;
    cpu_set_t cpus;
    CPU_ZERO(&cpus);
    CPU_SET(0, &cpus);
    if (sched_setaffinity(0, sizeof(cpus), &cpus))
        affinity_errors++;
    // Runtime is tiny, but the floor must be repaired within one frame even
    // when every CPU is saturated by the controlled background workload.
    if (setpriority(PRIO_PROCESS, 0, -10))
        priority_errors++;
    struct sched_param realtime = {
        .sched_priority = 1,
    };
    if (sched_setscheduler(0, SCHED_FIFO, &realtime))
        realtime_errors++;

    uint64_t loops = 0;
    uint64_t samples = 0;
    uint64_t writes = 0;
    uint64_t get_errors = 0;
    uint64_t set_errors = 0;
    uint64_t verify_retries = 0;
    uint64_t verify_errors = 0;
    uint64_t max_gap_us = 0;
    uint64_t live_tids = tid_count;
    uint64_t interval_ns = interval_us * 1000ULL;
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    uint64_t previous_ns = timespec_to_ns(&now);
    uint64_t next_ns = previous_ns + interval_ns;

    printf("start floor=%u interval_us=%" PRIu64 " tids=%zu\n",
           floor, interval_us, tid_count);
    fflush(stdout);

    while (!stop_requested && live_tids) {
        live_tids = 0;
        for (size_t index = 0; index < tid_count; index++) {
            if (tids[index] <= 0)
                continue;

            struct sched_attr_local attr;
            if (sched_getattr_local(tids[index], &attr)) {
                if (errno == ESRCH) {
                    tids[index] = -1;
                } else {
                    get_errors++;
                    live_tids++;
                }
                continue;
            }

            live_tids++;
            samples++;
            if (attr.sched_util_min < floor) {
                if (set_floor(tids[index], floor)) {
                    set_errors++;
                } else {
                    writes++;
                    if (sched_getattr_local(tids[index], &attr) ||
                        attr.sched_util_min < floor) {
                        // Android may update requested uclamp concurrently.
                        // Retry once; the next regular poll remains the bound
                        // if the competing update wins again.
                        verify_retries++;
                        if (set_floor(tids[index], floor)) {
                            set_errors++;
                        } else {
                            writes++;
                            if (sched_getattr_local(tids[index], &attr) ||
                                attr.sched_util_min < floor) {
                                verify_errors++;
                            }
                        }
                    }
                }
            }
        }

        loops++;
        clock_gettime(CLOCK_MONOTONIC, &now);
        uint64_t current_ns = timespec_to_ns(&now);
        uint64_t gap_us = (current_ns - previous_ns) / 1000ULL;
        if (gap_us > max_gap_us)
            max_gap_us = gap_us;
        previous_ns = current_ns;

        if (current_ns >= next_ns)
            next_ns = current_ns + interval_ns;
        struct timespec wakeup = ns_to_timespec(next_ns);
        while (!stop_requested &&
               clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &wakeup, NULL) == EINTR) {
        }
        next_ns += interval_ns;
    }

    printf("summary loops=%" PRIu64 " samples=%" PRIu64
           " writes=%" PRIu64 " get_errors=%" PRIu64
           " set_errors=%" PRIu64 " verify_retries=%" PRIu64
           " verify_errors=%" PRIu64
           " max_gap_us=%" PRIu64 " affinity_errors=%" PRIu64
           " priority_errors=%" PRIu64 " realtime_errors=%" PRIu64 "\n",
           loops, samples, writes, get_errors, set_errors, verify_retries,
           verify_errors,
           max_gap_us, affinity_errors, priority_errors, realtime_errors);
    fflush(stdout);
    free(tids);
    return set_errors || verify_errors ? 1 : 0;
}
