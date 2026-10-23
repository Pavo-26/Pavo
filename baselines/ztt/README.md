# zTT: Pixel 6 adaptation

The [agent](pixel6/ztt.py), [environment](pixel6/ztt_env.py) and
[device utilities](pixel6/ztt_utils.py) use Pixel 6's three-cluster frequency mapping.
Install [requirements.txt](requirements.txt) and prepare root ADB.

## Training and evaluation

From the package root, with the runtime configuration filled:

~~~bash
python baselines/ztt/train.py --app dy --beta 2 --execute \
  --serial DEVICE_SERIAL --runtime-config /path/to/pixel-runtime.json \
  --output /path/to/new-training-directory
~~~

Training uses the selected active scene and 16 matrixprod workers.
For frozen evaluation, set `source_dir` to the absolute `pixel6/` source path
and configure checkpoint paths and `model_stem` in the
[runtime template](../runtime_config.example.json).
Use the [main runner](../../experiments/README.md) with `--policies ztt`.
Both paths perform readiness checks around background startup.

## Source and licensing

Adapted from the [EarDVFS pixel7 sources](https://github.com/0jaehunny0/EarDVFS/tree/main/pixel7), which credit
[CleanRL](https://github.com/vwxyzjn/cleanrl),
[zTT](https://github.com/ztt-21/zTT) and
[GearDVFS](https://github.com/geardvfs/GearDVFS).
EarDVFS's [package metadata](https://github.com/0jaehunny0/EarDVFS/blob/main/pyproject.toml) lists MIT.
The CleanRL-derived DQN portion retains [its MIT notice](LICENSE.CleanRL).
Third-party code retains its upstream terms.
