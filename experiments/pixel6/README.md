# Pixel 6

## Main experiments

Use [run_main.py](run_main.py) with the shared [configuration and run commands](../README.md).
Policies: Default, Performance, scx_simple, scx_lavd, UCLAMP-GFX, frozen zTT and Pavo.
[apps.py](apps.py) contains the five App interaction routines.

## Temperature experiment

[run_thermal.py](run_thermal.py) runs Douyin with 16 matrixprod workers,
Default/Pavo, five repetitions and a 600-second PerfDog recording.
It follows the main App → PerfDog → background → interaction order, with a
60-second idle baseline before App launch and temperature sampling every second.

Compile [sysmon_native.c](sysmon_native.c) in ARM64 ADEB:

~~~sh
gcc -static -O2 -Wall -Wextra -o sysmon_native sysmon_native.c
~~~

Copy the executable to `/data/local/tmp/pavo-thermal/sysmon_native` and set
mode 755. From the host package root:

~~~powershell
python experiments/pixel6/run_thermal.py --execute --serial DEVICE_SERIAL --runtime-config pixel6.private.json --sysmon-binary /data/local/tmp/pavo-thermal/sysmon_native --output-root ../results/thermal-001
~~~

Outputs include `sysmon.csv`, phase markers and PerfDog exports.
Temperature is in millidegrees C and frequency is in kHz.
Sampling includes idle, App startup, interaction and shutdown, ending after
export, App closure and background shutdown.

## Honor of Kings

[run_game.py](run_game.py) uses Honor of Kings 11.4.1.1 and the same seven
policies, five repetitions and PerfDog version as the main entry.
Fill `apps.honor_of_kings` in the runtime configuration and the
[game layout](../configs/pixel6_game.example.json), using 2400 × 1080 landscape coordinates.

Keep graphics, hero and opponent fixed across runs. The reference is Angela
against Bian Que. [honor_of_kings.py](honor_of_kings.py) enters the training camp,
enables invincibility and no cooldown, sets the level/skills and moves to the
fixed position before recording.

Repeat five skill-1/skill-2 pairs per cycle in the same training session
during the 600-second PerfDog recording. Pair starts are two seconds apart.
Each complete cycle is at least 12 seconds. Recording stops at the deadline,
then the active cycle is interrupted; partial-cycle counts are retained.
The background and zTT readiness follow the [shared flow](../README.md).

~~~powershell
python experiments/pixel6/run_game.py --execute --serial DEVICE_SERIAL --runtime-config pixel6.private.json --output-root ../results/game-001
~~~

Use `--scenario no-load` for a run without background load, or `--policies`
to choose policies. Each run saves PerfDog exports, game actions and process logs.
