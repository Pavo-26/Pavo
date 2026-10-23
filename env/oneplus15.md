# OnePlus 15 deployment

Target: PLK110_16.0.8.302(CN01), ColorOS 16, and a Linux 6.12.23 kernel
built from source based on OPPO's official open-source release.
Use a matching full OTA, official Deep Testing and Magisk 30.7.

Prepare the kernel and matching modules using the
[kernel build guide](oneplus15-kernel/README.md).

## Images and unlock

Obtain the [matching OTA](https://bbs.oneplus.com/thread/2156278485054652419),
[payload-dumper-go](https://github.com/ssut/payload-dumper-go/releases),
[official Deep Testing app](https://oxygenos.oneplus.com/DeepTesting-636bfae-all-all-domestic-all-release.apk)
and [Magisk 30.7](https://github.com/topjohnwu/Magisk/releases/tag/v30.7).
Extract stock images on Ubuntu:

~~~bash
unzip FULL_OTA.zip payload.bin
./payload-dumper-go -o stock-images payload.bin
~~~

Keep these images for recovery. On the phone, enable OEM unlocking, apply
for Deep Testing and select “Start deep testing” after approval.
In the connected host terminal:

~~~sh
fastboot -s YOUR_ONEPLUS_SERIAL getvar is-userspace
fastboot -s YOUR_ONEPLUS_SERIAL getvar unlocked
fastboot -s YOUR_ONEPLUS_SERIAL flashing get_unlock_ability
~~~

With bootloader fastboot (`is-userspace=no`) and unlock eligibility confirmed,
unlock only if still locked. **Unlocking erases the phone.**

~~~sh
fastboot -s YOUR_ONEPLUS_SERIAL flashing unlock
~~~

Confirm on the phone, complete setup and re-enable USB debugging.

## Magisk

Install Magisk and copy the init_boot image matching the installed firmware:

~~~sh
adb -s YOUR_ONEPLUS_SERIAL install Magisk-v30.7.apk
adb -s YOUR_ONEPLUS_SERIAL push stock-images/init_boot.img /sdcard/Download/
~~~

In Magisk, choose Install → Select and Patch a File → init_boot.img.
Replace the output filename below with the actual patched filename:

~~~sh
adb -s YOUR_ONEPLUS_SERIAL pull /sdcard/Download/magisk_patched-ACTUAL_NAME.img .
adb -s YOUR_ONEPLUS_SERIAL reboot bootloader
fastboot -s YOUR_ONEPLUS_SERIAL getvar current-slot
~~~

Flash the **current slot** only. For slot b (use init_boot_a for slot a):

~~~sh
fastboot -s YOUR_ONEPLUS_SERIAL flash init_boot_b magisk_patched-ACTUAL_NAME.img
fastboot -s YOUR_ONEPLUS_SERIAL reboot
~~~

Open Magisk to complete setup and grant Shell root access.

## Persistent adb root

This setup makes the su domain permissive. Use a dedicated experiment phone.
Create `oneplus15_adbd_root/` on the host with the following LF-terminated files.

`module.prop`

~~~text
id=oneplus15_adbd_root
name=OnePlus 15 Persistent ADB Root
version=1.0
versionCode=1
author=Pavo
description=Enable root adbd.
~~~

`post-fs-data.sh`

~~~sh
#!/system/bin/sh
magiskpolicy --live 'allow adbd adbd process setcurrent'
magiskpolicy --live 'allow adbd su process dyntransition'
magiskpolicy --live 'permissive { su }'
resetprop ro.debuggable 1
resetprop ro.secure 0
resetprop service.adb.root 1
~~~

`service.sh`

~~~sh
#!/system/bin/sh
magiskpolicy --live 'allow adbd adbd process setcurrent'
magiskpolicy --live 'allow adbd su process dyntransition'
magiskpolicy --live 'permissive { su }'
resetprop ro.debuggable 1
resetprop ro.secure 0
resetprop service.adb.root 1

i=0
while [ "$(getprop sys.boot_completed)" != "1" ] && [ "$i" -lt 180 ]; do
    sleep 1
    i=$((i + 1))
done
resetprop service.adb.root 1
setprop ctl.restart adbd
~~~

Copy from the host and enter an Android shell:

~~~sh
adb -s YOUR_ONEPLUS_SERIAL push oneplus15_adbd_root /data/local/tmp/
adb -s YOUR_ONEPLUS_SERIAL shell
~~~

Enter `su`, grant access on the phone, and install into a new module directory.
If the directory already exists, inspect the existing module instead of overwriting it.

~~~sh
su
mkdir /data/adb/modules/oneplus15_adbd_root
cp /data/local/tmp/oneplus15_adbd_root/* /data/adb/modules/oneplus15_adbd_root/
chown -R 0:0 /data/adb/modules/oneplus15_adbd_root
chmod 755 /data/adb/modules/oneplus15_adbd_root
chmod 755 /data/adb/modules/oneplus15_adbd_root/*.sh
chmod 644 /data/adb/modules/oneplus15_adbd_root/module.prop
exit
exit
~~~

From the host, reboot and wait for Android startup to finish:

~~~sh
adb -s YOUR_ONEPLUS_SERIAL reboot
adb -s YOUR_ONEPLUS_SERIAL wait-for-device
adb -s YOUR_ONEPLUS_SERIAL root
adb -s YOUR_ONEPLUS_SERIAL wait-for-device
adb -s YOUR_ONEPLUS_SERIAL shell id
adb -s YOUR_ONEPLUS_SERIAL shell ls /sys/kernel/btf/vmlinux
adb -s YOUR_ONEPLUS_SERIAL shell cat /sys/kernel/sched_ext/state
~~~

With `uid=0` and readable BTF/sched_ext nodes, continue with [ADEB](adeb.md).

## Restore

To disable this module while keeping Magisk:

~~~sh
adb -s YOUR_ONEPLUS_SERIAL shell su -c 'touch /data/adb/modules/oneplus15_adbd_root/disable'
adb -s YOUR_ONEPLUS_SERIAL reboot
~~~

To remove Magisk, restore the matching stock init_boot to the current slot.
Keep the bootloader unlocked while modified images are installed.
