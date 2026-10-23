# Experiment scripts

Run on Windows with Python 3.11+ and PerfDog 11.2.250408. Commands below
start from the package root.

~~~powershell
python -m pip install -r requirements.txt
~~~

Prepare [the device environment](../env/README.md), then fill local copies of
the [runtime templates](../baselines/README.md) and
[App/PerfDog configuration](configs/README.md). Scheduler binaries run in ADEB.
stress-ng and UCLAMP run in Android. Keep Pavo's `arguments: []`.

## Application naming

The TikTok workload label refers to Douyin (`dy`, `com.ss.android.ugc.aweme`).

## Run

~~~powershell
python experiments/pixel6/run_main.py --execute --serial DEVICE_SERIAL --runtime-config pixel6.private.json --output-root ../results/pixel6-001
python experiments/oneplus15/run_main.py --execute --serial DEVICE_SERIAL --runtime-config oneplus15.private.json --output-root ../results/oneplus15-001
~~~

Use a new output directory. Select Apps and policies with `--apps` and `--policies`.
For example, `--apps dy --policies pavo --scenario no-load` runs Douyin/Pavo
without the background workload.

## Flow

The five Apps are Douyin, iQIYI, Taobao, Weibo and Kuaishou. Each policy
runs five consecutive repetitions per App, with a 600-second PerfDog recording.
Use Wi-Fi, fixed 50% brightness and approximately 20 C ambient temperature.
The runner requires at least 25% battery.

The usual order is **policy → App → PerfDog → background → interaction**.
Mixed runs use 16 stress-ng matrixprod workers. Browsing Apps repeat in-App
rounds within the same session. iQIYI plays the selected video continuously.
The recording clock starts at the PerfDog record click and includes background
startup. At 600 seconds, the runner stops recording first, then interrupts the
App even mid-round. Host stop latency above one second invalidates the run.
Capture metadata records the actual recording duration, its overlap with App
interaction and any interaction shutdown time after recording stops.
The runner exports the capture, stops its processes and restores controls.

For zTT, first enter the active scene and obtain three consecutive valid
foreground/layer/FPS observations before and after background startup.
Start frozen zTT and PerfDog after both checks pass.

## Other entries

- [Pixel 6](pixel6/README.md): main, temperature and Honor of Kings runs.
- [OnePlus 15](oneplus15/README.md): device entry.
- [Collectors](collectors/README.md): Perfetto, background and ODPM.
- [Metrics](analysis/README.md): WJ and average power.
- [Baselines](../baselines/README.md) and [workloads](../workloads/README.md).
