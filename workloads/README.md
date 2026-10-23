# Workloads

The main runner uses stress-ng. FFmpeg, fio and dex2oat have standalone
launchers. Copy their directories to `/data/local/tmp/pavo-workloads/`.
Use the same tool versions and input files across runs.

## stress-ng

Build [stress-ng](https://github.com/ColinIanKing/stress-ng) as a static
Android ARM64 executable on a Linux host with the Android NDK:

~~~bash
cd /path/to/stress-ng-source
export NDK=/path/to/android-ndk
export PATH="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin:$PATH"
export CC=aarch64-linux-android27-clang
STATIC=1 make -j2
adb -s YOUR_DEVICE_SERIAL push stress-ng /data/local/tmp/stress-ng
adb -s YOUR_DEVICE_SERIAL shell chmod 755 /data/local/tmp/stress-ng
~~~

The main runner manages this Android command:

~~~sh
/data/local/tmp/stress-ng --cpu 16 --cpu-method matrixprod \
  --oom-avoid --metrics-brief --times --timeout 780s
~~~

The runner starts and stops the process.
For standalone capture, use [record_raw.py](../experiments/collectors/README.md)
with [stress-ng-matrixprod.json](stress-ng-matrixprod.json).

## FFmpeg

Install `ffmpeg` inside ARM64 ADEB (`apt-get install -y ffmpeg`) with libx264
and AAC support. Prepare a 1920 × 1080, 30-fps, 120-second video with audio:

~~~sh
adb -s YOUR_DEVICE_SERIAL push input_1080p30_120s.mp4 /data/local/tmp/input_1080p30_120s.mp4
~~~

Run [ffmpeg/run.sh](ffmpeg/run.sh) inside ADEB to start two encoding instances.
The final argument sets the media duration to encode, in seconds:

~~~sh
bash /data/local/tmp/pavo-workloads/ffmpeg/run.sh \
  /data/local/tmp/input_1080p30_120s.mp4 \
  /data/local/tmp/ffmpeg-run-001 60
~~~

## fio

Build a selected [fio release](https://github.com/axboe/fio) statically in ARM64
ADEB with libaio. The executable runs outside ADEB:

~~~sh
apt-get install -y build-essential libaio-dev zlib1g-dev
cd /path/to/fio-source
./configure --build-static
make -j2
./fio --enghelp=libaio
install -m 755 fio /data/local/tmp/fio
~~~

[fio/run.sh](fio/run.sh) uses [randrw.fio](fio/randrw.fio) and creates a 2-GiB file.
In an Android root shell:

~~~sh
sh /data/local/tmp/pavo-workloads/fio/run.sh \
  /data/local/tmp/fio /data/local/tmp/fio-run-001 60
~~~

The final argument is runtime in seconds. Output includes `fio.json` and I/O logs.

## dex2oat

Install WeChat 8.0.64, versionCode 2940 (`com.tencent.mm`).
Official website: [Weixin](https://weixin.qq.com/).

~~~sh
adb -s YOUR_DEVICE_SERIAL install wechat-8.0.64.apk
~~~

In an Android root shell, run [dex2oat/run.sh](dex2oat/run.sh).
It resets package compilation, compiles with `speed` / `PRIORITY_BACKGROUND`
and samples process state until completion:

~~~sh
sh /data/local/tmp/pavo-workloads/dex2oat/run.sh \
  /data/local/tmp/dex2oat-run-001
~~~

## MobileNet

Use the [TensorFlow Lite Android benchmark APK](https://github.com/tensorflow/tensorflow/tree/master/tensorflow/lite/tools/benchmark/android)
([AArch64 download](https://storage.googleapis.com/tensorflow-nightly-public/prod/tensorflow/release/lite/tools/nightly/latest/android_aarch64_benchmark_model.apk))
and the [MobileNet V1 1.0 / 224 FLOAT32 model](https://storage.googleapis.com/download.tensorflow.org/models/mobilenet_v1_2018_08_02/mobilenet_v1_1.0_224.tgz).
Extract `mobilenet_v1_1.0_224.tflite` and retain the same APK/model across runs.

~~~sh
adb -s YOUR_DEVICE_SERIAL install -g benchmark_model.apk
adb -s YOUR_DEVICE_SERIAL shell mkdir -p /data/local/tmp/pavo_ml
adb -s YOUR_DEVICE_SERIAL push mobilenet_v1_1.0_224.tflite /data/local/tmp/pavo_ml/
adb -s YOUR_DEVICE_SERIAL shell chmod 755 /data/local/tmp/pavo_ml
adb -s YOUR_DEVICE_SERIAL shell chmod 644 /data/local/tmp/pavo_ml/mobilenet_v1_1.0_224.tflite
~~~

Use `org.tensorflow.lite.benchmark/.BenchmarkModelActivity` in the foreground
with these arguments:

~~~text
--graph=/data/local/tmp/pavo_ml/mobilenet_v1_1.0_224.tflite
--num_threads=4
--use_nnapi=false
--use_gpu=false
--use_xnnpack=false
--warmup_runs=5
--num_runs=1
--min_secs=60
--max_secs=70
--run_frequency=5
~~~

This uses a resident process/model and a native 5-Hz loop.
For foreground game runs, see
[Honor of Kings](../experiments/pixel6/README.md).
