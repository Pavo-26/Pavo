/* sysmon_native.c - Pixel 6 raw temperature/frequency recorder.
 *
 * Build in ARM64 ADEB:
 *   gcc -static -O2 -Wall -Wextra -o sysmon_native sysmon_native.c
 *
 * Usage:
 *   ./sysmon_native <output_dir> [duration_sec] [interval_ms] [pin_little] [batch_size]
 * Sensors are opened once. Sampling uses absolute CLOCK_BOOTTIME deadlines.
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <time.h>
#include <errno.h>
#include <sched.h>
#include <stdint.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/types.h>

/* Pixel 6 sensor paths; the host checks their type names before capture. */
static const char *TZ_PATHS[] = {
    "/sys/class/thermal/thermal_zone0/temp",
    "/sys/class/thermal/thermal_zone1/temp",
    "/sys/class/thermal/thermal_zone2/temp",
    "/sys/class/thermal/thermal_zone16/temp",
    "/sys/class/thermal/thermal_zone25/temp",
};
#define NUM_TZ 5

static const char *FREQ_CUR_PATHS[] = {
    "/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq",
    "/sys/devices/system/cpu/cpufreq/policy4/scaling_cur_freq",
    "/sys/devices/system/cpu/cpufreq/policy6/scaling_cur_freq",
};
static const char *FREQ_MAX_PATHS[] = {
    "/sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq",
    "/sys/devices/system/cpu/cpufreq/policy4/scaling_max_freq",
    "/sys/devices/system/cpu/cpufreq/policy6/scaling_max_freq",
};
#define NUM_POLICY 3

#define BATT_I_PATH "/sys/class/power_supply/battery/current_now"
#define BATT_V_PATH "/sys/class/power_supply/battery/voltage_now"

#define BUF_SIZE 64

static volatile sig_atomic_t running = 1;

static void stop_recording(int signal_number) {
    (void)signal_number;
    running = 0;
}

/* Initialize descriptors to -1 rather than accidentally reading stdin. */
static int fd_tz[NUM_TZ] = {-1, -1, -1, -1, -1};
static int fd_freq_cur[NUM_POLICY] = {-1, -1, -1};
static int fd_freq_max[NUM_POLICY] = {-1, -1, -1};
static int fd_batt_i = -1;
static int fd_batt_v = -1;

/* Read a raw integer; -1 denotes an unavailable read. */
static int read_sysfs_int(int fd) {
    char buf[BUF_SIZE];
    if (fd < 0) return -1;
    if (lseek(fd, 0, SEEK_SET) < 0) return -1;
    ssize_t n = read(fd, buf, BUF_SIZE - 1);
    if (n <= 0) return -1;
    buf[n] = '\0';
    return atoi(buf);
}

/* Open the source files once. */
static void open_all_fds(void) {
    for (int i = 0; i < NUM_TZ; i++)
        fd_tz[i] = open(TZ_PATHS[i], O_RDONLY);
    for (int i = 0; i < NUM_POLICY; i++) {
        fd_freq_cur[i] = open(FREQ_CUR_PATHS[i], O_RDONLY);
        fd_freq_max[i] = open(FREQ_MAX_PATHS[i], O_RDONLY);
    }
    fd_batt_i = open(BATT_I_PATH, O_RDONLY);
    fd_batt_v = open(BATT_V_PATH, O_RDONLY);
}

/* Close source files. */
static void close_all_fds(void) {
    for (int i = 0; i < NUM_TZ; i++)
        if (fd_tz[i] >= 0) close(fd_tz[i]);
    for (int i = 0; i < NUM_POLICY; i++) {
        if (fd_freq_cur[i] >= 0) close(fd_freq_cur[i]);
        if (fd_freq_max[i] >= 0) close(fd_freq_max[i]);
    }
    if (fd_batt_i >= 0) close(fd_batt_i);
    if (fd_batt_v >= 0) close(fd_batt_v);
}

/* Optional LITTLE affinity (CPUs 0-3); disabled by the runner. */
static void pin_to_little_cores(void) {
    cpu_set_t mask;
    CPU_ZERO(&mask);
    CPU_SET(0, &mask);
    CPU_SET(1, &mask);
    CPU_SET(2, &mask);
    CPU_SET(3, &mask);
    sched_setaffinity(0, sizeof(mask), &mask);
}

/* CLOCK_BOOTTIME timestamp in nanoseconds. */
static inline int64_t get_boottime_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_BOOTTIME, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

/* clock_nanosleep returns an error number, not -1/errno. */
static inline int sleep_until(struct timespec *ts) {
    int error;
    do {
        error = clock_nanosleep(CLOCK_BOOTTIME, TIMER_ABSTIME, ts, NULL);
    } while (error == EINTR && running);
    return error;
}

int main(int argc, char *argv[]) {
    if (argc == 2 && strcmp(argv[1], "--describe") == 0) {
        puts("sysmon_native pixel6-thermal-v1");
        return 0;
    }
    if (argc < 2) {
        fprintf(stderr, "Usage: %s <output_dir> [duration_sec] [interval_ms] [pin_little] [batch_size]\n", argv[0]);
        return 1;
    }

    const char *outdir = argv[1];
    int duration_sec = argc >= 3 ? atoi(argv[2]) : 600;
    int interval_ms = argc >= 4 ? atoi(argv[3]) : 1000;
    int pin_little = argc >= 5 ? atoi(argv[4]) : 0;
    int batch_size = argc >= 6 ? atoi(argv[5]) : 1;  /* 1 = flush each row; larger values batch writes. */

    if (duration_sec < 1 || duration_sec > 86400 ||
        interval_ms < 1 || interval_ms > 60000 || batch_size < 1) {
        fprintf(stderr, "Invalid duration, interval or batch size\n");
        return 1;
    }
    if (mkdir(outdir, 0755) < 0 && errno != EEXIST) {
        perror("mkdir");
        return 1;
    }

    struct sigaction action = {0};
    action.sa_handler = stop_recording;
    sigemptyset(&action.sa_mask);
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGINT, &action, NULL);

    char csv_path[512], sync_path[512];
    snprintf(csv_path, sizeof(csv_path), "%s/sysmon.csv", outdir);
    snprintf(sync_path, sizeof(sync_path), "%s/sync_start.txt", outdir);

    if (pin_little) pin_to_little_cores();

    open_all_fds();

    FILE *csv_fp = fopen(csv_path, "wx");
    if (!csv_fp) { perror("fopen csv"); return 1; }

    /* Line buffering preserves completed rows if recording is interrupted. */
    if (batch_size <= 1) {
        setvbuf(csv_fp, NULL, _IOLBF, 0);
    } else {
        setvbuf(csv_fp, NULL, _IOFBF, 8192);
    }

    fprintf(csv_fp, "boottime_ns,scheduled_ns,late_us,"
                    "tz0_mC,tz1_mC,tz2_mC,skin_mC,batt_mC,"
                    "p0_cur,p0_max,p4_cur,p4_max,p6_cur,p6_max,"
                    "current_uA,voltage_uV\n");

    int64_t start_ns = get_boottime_ns();
    struct timespec ts_real;
    clock_gettime(CLOCK_REALTIME, &ts_real);

    FILE *sync_fp = fopen(sync_path, "w");
    if (sync_fp) {
        fprintf(sync_fp, "epoch0=%ld\nboottime_ns0=%lld\n", (long)ts_real.tv_sec, (long long)start_ns);
        fclose(sync_fp);
    }

    int total_samples = (int)(((int64_t)duration_sec * 1000) / interval_ms);
    int64_t end_ns = start_ns + (int64_t)duration_sec * 1000000000LL;
    int64_t interval_ns = (int64_t)interval_ms * 1000000LL;
    int64_t late_threshold_ns = interval_ns / 2;

    fprintf(stderr, "sysmon_native: %d samples, %dms interval, batch=%d\n", total_samples, interval_ms, batch_size);

    struct timespec next_time;
    clock_gettime(CLOCK_BOOTTIME, &next_time);

    int sample_count = 0, late_count = 0, batch_count = 0;

    int clock_error = 0;
    for (int i = 0; i < total_samples && running; i++) {
        int error = sleep_until(&next_time);
        if (!running) break;
        if (error) {
            fprintf(stderr, "clock_nanosleep: %s\n", strerror(error));
            clock_error = 1;
            break;
        }

        int64_t actual_ns = get_boottime_ns();
        if (actual_ns >= end_ns) break;
        int64_t scheduled_ns = (int64_t)next_time.tv_sec * 1000000000LL + next_time.tv_nsec;
        int64_t late_ns = actual_ns - scheduled_ns;

        next_time.tv_nsec += interval_ns;
        while (next_time.tv_nsec >= 1000000000L) {
            next_time.tv_nsec -= 1000000000L;
            next_time.tv_sec++;
        }

        if (late_ns > late_threshold_ns) {
            late_count++;
            clock_gettime(CLOCK_BOOTTIME, &next_time);
            next_time.tv_nsec += interval_ns;
            while (next_time.tv_nsec >= 1000000000L) {
                next_time.tv_nsec -= 1000000000L;
                next_time.tv_sec++;
            }
            continue;
        }

        int tz[NUM_TZ];
        for (int j = 0; j < NUM_TZ; j++) tz[j] = read_sysfs_int(fd_tz[j]);

        int freq_cur[NUM_POLICY], freq_max[NUM_POLICY];
        for (int j = 0; j < NUM_POLICY; j++) {
            freq_cur[j] = read_sysfs_int(fd_freq_cur[j]);
            freq_max[j] = read_sysfs_int(fd_freq_max[j]);
        }

        int batt_i = read_sysfs_int(fd_batt_i);
        int batt_v = read_sysfs_int(fd_batt_v);

        fprintf(csv_fp, "%lld,%lld,%lld,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d\n",
                (long long)actual_ns, (long long)scheduled_ns, (long long)(late_ns / 1000),
                tz[0], tz[1], tz[2], tz[3], tz[4],
                freq_cur[0], freq_max[0], freq_cur[1], freq_max[1], freq_cur[2], freq_max[2],
                batt_i, batt_v);
        sample_count++;

        /* Flush batched rows. */
        if (batch_size > 1) {
            batch_count++;
            if (batch_count >= batch_size) {
                fflush(csv_fp);
                batch_count = 0;
            }
        }
    }

    int write_error = ferror(csv_fp);
    if (fclose(csv_fp) != 0) write_error = 1;

    sync_fp = fopen(sync_path, "a");
    if (sync_fp) {
        clock_gettime(CLOCK_REALTIME, &ts_real);
        fprintf(sync_fp, "epoch1=%ld\nboottime_ns1=%lld\nsamples=%d\nlate_skipped=%d\n",
                (long)ts_real.tv_sec, (long long)get_boottime_ns(), sample_count, late_count);
        fprintf(sync_fp, "stopped_by_signal=%d\nclock_error=%d\nwrite_error=%d\n",
                !running, clock_error, write_error);
        if (fclose(sync_fp) != 0) write_error = 1;
    } else {
        write_error = 1;
    }

    close_all_fds();
    fprintf(stderr, "sysmon_native: done, %d samples, %d late\n", sample_count, late_count);
    return clock_error || write_error ? 1 : 0;
}
