# Pixel 6 deployment

Use AOSP Android 15 userdebug with a sched_ext-capable 6.12 kernel.
An already configured phone can proceed directly to **ADB root** below.

## AOSP

On Ubuntu, install the [AOSP build dependencies and repo](https://source.android.com/docs/setup/start)
and obtain the matching [Pixel drivers](https://developers.google.com/android/drivers).

~~~bash
mkdir aosp
cd aosp
repo init -u https://android.googlesource.com/platform/manifest -b android-15.0.0_r11
repo sync
~~~

Extract the matching Pixel 6 drivers in this source tree, then build:

~~~bash
source build/envsetup.sh
lunch aosp_oriole-ap4a-userdebug
m
~~~

For a fresh system installation, enable OEM unlocking and enter the bootloader.
**Unlocking and flashall -w erase the phone.** Skip unlocking if already unlocked.

~~~sh
adb -s YOUR_PIXEL_SERIAL reboot bootloader
fastboot -s YOUR_PIXEL_SERIAL getvar product
fastboot -s YOUR_PIXEL_SERIAL flashing unlock
~~~

Confirm unlocking on the phone. From the AOSP build directory, with the phone
back in the bootloader and product confirmed as oriole:

~~~bash
export ANDROID_PRODUCT_OUT="$PWD/out/target/product/oriole"
fastboot -s YOUR_PIXEL_SERIAL flashall -w
~~~

Complete Android setup and re-enable USB debugging.

## Kernel

The included [release](pixel6-kernel/kernel-release.txt) and
[configuration](pixel6-kernel/kernel.config) describe
`6.12.0-mainline-g8ea06136d1fa-dirty-4k`.
A source build requires its corresponding manifest and configuration.
Running Pavo requires sched_ext, BPF and BTF support.

In a separate Ubuntu source directory, use the manifest exported from the
chosen 6.12 build tree (`repo manifest -r -o pixel6-kernel.xml`):

~~~bash
mkdir android-kernel
cd android-kernel
repo init -u https://android.googlesource.com/kernel/manifest -b gs-android-gs-raviole-mainline
cp /path/to/pixel6-kernel.xml .repo/manifests/pixel6-kernel.xml
repo init -m pixel6-kernel.xml
repo sync -d -j8
tools/bazel run //common:kernel_aarch64_config -- menuconfig
~~~

Apply the corresponding configuration. Enable sched_ext, BPF and
BTF. The included configuration disables Clang CFI.

~~~bash
tools/bazel run --config=fast --config=stamp //private/google-modules/soc/gs:slider_dist
~~~

If `lib/test_bpf.ko` is missing from module outputs, add it to
`_COMMON_GKI_MODULES_LIST` in `common/modules.bzl`.
Use one complete build from `out/slider/dist/` for installation:

~~~sh
adb -s YOUR_PIXEL_SERIAL reboot bootloader
fastboot -s YOUR_PIXEL_SERIAL flash boot out/slider/dist/boot.img
fastboot -s YOUR_PIXEL_SERIAL flash dtbo out/slider/dist/dtbo.img
fastboot -s YOUR_PIXEL_SERIAL flash vendor_boot out/slider/dist/vendor_boot.img
fastboot -s YOUR_PIXEL_SERIAL reboot fastboot
fastboot -s YOUR_PIXEL_SERIAL getvar is-userspace
~~~

With `is-userspace=yes`, install the module partition:

~~~sh
fastboot -s YOUR_PIXEL_SERIAL flash vendor_dlkm out/slider/dist/vendor_dlkm.img
fastboot -s YOUR_PIXEL_SERIAL reboot
~~~

If the build supplies `initramfs.img` and `dtb.img` instead of
`vendor_boot.img`, replace only the vendor_boot command with:

~~~sh
fastboot -s YOUR_PIXEL_SERIAL flash vendor_boot:dlkm out/slider/dist/initramfs.img --dtb out/slider/dist/dtb.img
~~~

Stop on a flashing error. Keep matching recovery images and leave the
bootloader unlocked while using modified images.
See [Pixel kernel builds](https://source.android.com/docs/setup/build/building-pixel-kernels).

## ADB root

On the connected host:

~~~sh
adb -s YOUR_PIXEL_SERIAL root
adb -s YOUR_PIXEL_SERIAL wait-for-device
adb -s YOUR_PIXEL_SERIAL shell id
adb -s YOUR_PIXEL_SERIAL shell uname -r
adb -s YOUR_PIXEL_SERIAL shell ls /sys/kernel/btf/vmlinux
adb -s YOUR_PIXEL_SERIAL shell cat /sys/kernel/sched_ext/state
~~~

Expect `uid=0`, kernel 6.12 and readable BTF/sched_ext nodes.
Continue with [ADEB](adeb.md).
