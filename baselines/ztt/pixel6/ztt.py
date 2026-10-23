"""Packaged Pixel 6 zTT adaptation; see ../README.md for provenance and changes."""

# docs and experiment results can be found at https://docs.cleanrl.dev/rl-algorithms/dqn/#dqnpy
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path

import gymnasium as gym
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import tyro
from stable_baselines3.common.buffers import ReplayBuffer
from ztt_env import DVFStrain
from ztt_utils import save_device_controls, restore_device_controls, set_root


@dataclass
class Args:
    exp_name: str = os.path.basename(__file__)[: -len(".py")]
    """the name of this experiment"""
    seed: int = 1
    """seed of the experiment"""
    torch_deterministic: bool = True
    """if toggled, `torch.backends.cudnn.deterministic=False`"""
    cuda: bool = True
    """if toggled, cuda will be enabled by default"""
    track: bool = False
    """if toggled, this experiment will be tracked with Weights and Biases"""
    wandb_project_name: str = "cleanRL"
    """the wandb's project name"""
    wandb_entity: str = None
    """the entity (team) of wandb's project"""
    capture_video: bool = False
    """whether to capture videos of the agent performances (check out `videos` folder)"""
    save_model: bool = False
    """whether to save model into the `runs/{run_name}` folder"""
    upload_model: bool = False
    """whether to upload the saved model to huggingface"""
    hf_entity: str = ""
    """the user or org name of the model repository from the Hugging Face Hub"""

    # Algorithm specific arguments
    env_id: str = "ztt_pixel6"
    """the id of the environment"""
    total_timesteps: int = 1201
    """total timesteps of the experiments"""
    learning_rate: float = 0.05
    """the learning rate of the optimizer"""
    num_envs: int = 1
    """the number of parallel game environments"""
    buffer_size: int = 500
    """the replay memory buffer size"""
    gamma: float = 0.99
    """the discount factor gamma"""
    tau: float = 1.0
    """the target network update rate"""
    target_network_frequency: int = 1
    """the timesteps it takes to update the target network"""
    batch_size: int = 64
    """the batch size of sample from the reply memory"""
    start_e: float = 1
    """the starting epsilon for exploration"""
    end_e: float = 0.0
    """the ending epsilon for exploration"""
    exploration_fraction: float = 0.5
    """the fraction of `total-timesteps` it takes from start-e to go end-e"""
    learning_starts: int = 150
    """timestep to start learning"""
    train_frequency: int = 10
    """the frequency of training"""

    experiment: int = 1
    """the type of experiment"""
    temperature: int = 20
    """the ouside temperature"""
    initSleep: int = 600
    """initial sleep time"""
    loadModel: str = "no"
    """the save path of model"""
    timeOut: int = 30 * 60
    """the end time"""
    qos: str = "fps"
    """Quality of Service type"""
    targetTemp: int = 65
    """target temperature"""
    beta: float = 2.0
    """energy reward weight from the public zTT implementation"""
    latency: int = 0
    """additional latency for adb communication"""
    interval: float = 1.0
    """interval between DVFSs"""
    tempSet: float = -1.0
    """initial temperature"""


def make_env(env_id, seed, idx, capture_video, run_name):
    def thunk():
        env = DVFStrain(
            args.initSleep,
            args.experiment,
            args.qos,
            args.targetTemp,
            args.latency,
            args.interval,
            args.beta,
        )
        env = gym.wrappers.RecordEpisodeStatistics(env)
        env.action_space.seed(seed)

        return env

    return thunk


# ALGO LOGIC: initialize agent here:
class QNetwork(nn.Module):
    def __init__(self, env):
        super().__init__()
        self.network = nn.Sequential(
            nn.Linear(np.array(env.single_observation_space.shape).prod(), 6),
            nn.ReLU(),
            nn.Linear(6, 6),
            nn.ReLU(),
            nn.Linear(6, env.single_action_space.n),
        )

    def forward(self, x):
        x = x.float()
        return self.network(x)


def linear_schedule(start_e: float, end_e: float, duration: int, t: int):
    slope = (end_e - start_e) / duration
    return max(slope * t + start_e, end_e)


def vector_info_at(info, index: int = 0):
    """Unpack Gymnasium vector-info dictionaries for one environment."""
    if not isinstance(info, dict):
        return {}

    unpacked = {}
    for key, value in info.items():
        if str(key).startswith("_") or isinstance(value, dict):
            continue
        mask = info.get(f"_{key}")
        if isinstance(mask, np.ndarray) and mask.shape and not bool(mask[index]):
            continue
        if isinstance(value, np.ndarray) and value.shape and value.shape[0] > index:
            unpacked[key] = value[index]
        elif isinstance(value, (list, tuple)) and len(value) > index:
            unpacked[key] = value[index]
        else:
            unpacked[key] = value
    return unpacked


def run():
    global args
    args = tyro.cli(Args)
    if os.environ.get("EARDVFS_MANAGED_RUN") != "1":
        raise RuntimeError(
            "Use the artifact evaluation runner or baselines/ztt/train.py"
        )
    if not os.environ.get("ANDROID_SERIAL") or not os.environ.get(
        "EARDVFS_TARGET_PACKAGE"
    ):
        raise RuntimeError(
            "An explicit device serial and foreground package are required"
        )
    if args.num_envs != 1 or args.qos != "fps" or args.tempSet != -1.0:
        raise ValueError(
            "Expected num_envs=1, qos=fps and tempSet=-1.0"
        )
    frozen_eval = os.environ.get("EARDVFS_FROZEN_EVAL", "0") == "1"
    if frozen_eval and args.loadModel == "no":
        raise ValueError("Frozen evaluation requires a pinned checkpoint")
    if not frozen_eval and args.timeOut != 900:
        raise ValueError("Training requires a 900-second budget")

    save_dir = Path(os.environ["EARDVFS_SAVE_DIR"])
    save_dir.mkdir(parents=True, exist_ok=True)
    run_name = f"{int(time.time())}__{args.env_id}__{args.seed}__{args.tempSet}__exp{args.experiment}__temp{args.temperature}__target{args.targetTemp}__beta{args.beta:g}"
    epsilon = 0 if frozen_eval else 1
    epsilon_decay = 0.08

    # Check the root shell before saving device controls.
    set_root()
    save_device_controls()
    envs = None
    completed = False
    try:
        random.seed(args.seed)
        np.random.seed(args.seed)
        torch.manual_seed(args.seed)
        torch.backends.cudnn.deterministic = args.torch_deterministic
        device = torch.device(
            "cuda" if torch.cuda.is_available() and args.cuda else "cpu"
        )
        env_fns = [
            make_env(args.env_id, args.seed + i, i, args.capture_video, run_name)
            for i in range(args.num_envs)
        ]
        try:
            envs = gym.vector.SyncVectorEnv(
                env_fns, autoreset_mode=gym.vector.AutoresetMode.SAME_STEP
            )
        except (AttributeError, TypeError):
            envs = gym.vector.SyncVectorEnv(env_fns)
        assert isinstance(envs.single_action_space, gym.spaces.Discrete)
        q_network = QNetwork(envs).to(device)
        optimizer = optim.Adam(q_network.parameters(), lr=args.learning_rate)
        target_network = QNetwork(envs).to(device)
        target_network.load_state_dict(q_network.state_dict())
        if args.loadModel != "no":
            for network, suffix in (
                (q_network, "_q_network.pt"),
                (target_network, "_target_network.pt"),
            ):
                # Only a trusted state_dict is accepted.
                network.load_state_dict(
                    torch.load(
                        save_dir / (args.loadModel + suffix),
                        map_location=device,
                        weights_only=True,
                    )
                )
        rb = ReplayBuffer(
            args.buffer_size,
            envs.single_observation_space,
            envs.single_action_space,
            device,
            handle_timeout_termination=False,
        )
        start_time = time.monotonic()
        if not frozen_eval:
            print("TRAIN_START budget_s=900", flush=True)
        obs, _ = envs.reset(seed=args.seed)
        for global_step in range(args.total_timesteps):
            if time.monotonic() - start_time > args.timeOut:
                break
            if not frozen_eval:
                epsilon *= epsilon_decay
            if random.random() < epsilon:
                actions = np.array(
                    [envs.single_action_space.sample() for _ in range(envs.num_envs)]
                )
            else:
                with torch.no_grad():
                    q_values = q_network(torch.Tensor(obs).to(device))
                    actions = torch.argmax(q_values, dim=1).cpu().numpy()
            if frozen_eval:
                print(
                    f"FROZEN_EVAL_ACTION step={global_step} action={int(actions[0])}",
                    flush=True,
                )
            else:
                print(
                    f"TRAIN_ACTION step={global_step} action={int(actions[0])}",
                    flush=True,
                )
            next_obs, rewards, terminations, truncations, infos = envs.step(actions)
            final_info = infos.get("final_info")
            if isinstance(final_info, dict):
                step_info = vector_info_at(final_info)
            elif (
                isinstance(final_info, np.ndarray)
                and final_info.size
                and final_info[0] is not None
            ):
                step_info = final_info[0]
            elif (
                isinstance(final_info, (list, tuple))
                and final_info
                and final_info[0] is not None
            ):
                step_info = final_info[0]
            else:
                step_info = vector_info_at(infos)
            real_next_obs = next_obs.copy()
            final_observation = infos.get("final_observation", infos.get("final_obs"))
            if final_observation is not None:
                for idx, trunc in enumerate(truncations):
                    if trunc:
                        real_next_obs[idx] = final_observation[idx]
            rb.add(obs, real_next_obs, actions, rewards, terminations, infos)
            obs = next_obs
            if "little" not in step_info:
                print(
                    f"warning: incomplete step info keys={list(infos.keys())}",
                    flush=True,
                )
                continue
            # Preserve the local adaptation's optimizer/target update schedule.
            if not frozen_eval and global_step > args.learning_starts:
                if global_step % args.train_frequency == 0:
                    data = rb.sample(args.batch_size)
                    with torch.no_grad():
                        target_max, _ = target_network(data.next_observations).max(
                            dim=1
                        )
                        td_target = data.rewards.flatten() + args.gamma * target_max * (
                            1 - data.dones.flatten()
                        )
                    old_val = (
                        q_network(data.observations).gather(1, data.actions).squeeze()
                    )
                    loss = F.mse_loss(td_target, old_val)
                    optimizer.zero_grad()
                    loss.backward()
                    optimizer.step()
                    print(
                        f"TRAIN_LOSS step={global_step} loss={float(loss.detach())}",
                        flush=True,
                    )
                if global_step % args.target_network_frequency == 0:
                    for target_param, param in zip(
                        target_network.parameters(), q_network.parameters()
                    ):
                        target_param.data.copy_(
                            args.tau * param.data + (1.0 - args.tau) * target_param.data
                        )
            elif not frozen_eval:
                # Retain the local warmup sampling sequence (no optimizer step).
                # Dropping this would change the replay-buffer RNG stream.
                data = rb.sample(args.batch_size)
                with torch.no_grad():
                    target_max, _ = target_network(data.next_observations).max(dim=1)
                    td_target = data.rewards.flatten() + args.gamma * target_max * (
                        1 - data.dones.flatten()
                    )
                old_val = q_network(data.observations).gather(1, data.actions).squeeze()
                loss = F.mse_loss(td_target, old_val)
        completed = True
        if not frozen_eval:
            print(
                f"TRAIN_COMPLETE elapsed_s={time.monotonic() - start_time:.6f}",
                flush=True,
            )
    finally:
        if envs is not None:
            envs.close()
        restore_device_controls()
    if completed and not frozen_eval:
        torch.save(q_network.state_dict(), save_dir / f"{run_name}_q_network.pt")
        torch.save(
            target_network.state_dict(), save_dir / f"{run_name}_target_network.pt"
        )


if __name__ == "__main__":
    run()
