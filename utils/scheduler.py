import warnings
from typing import List, Dict, Any
from yacs.config import CfgNode as CN

from torch.optim import Optimizer
from torch.optim.lr_scheduler import CosineAnnealingLR, MultiStepLR


def build_scheduler(optimizer: Optimizer, cfg: CN):
    """
    Args:
        optimizer (Optimizer): torch.optim.Optimizer
        cfg (CN): an instance of CfgNode with attributes:
            - TRAIN.SCHED.NAME: 'CosineAnnealingLR', 'MultiStepLR'
            - TRAIN.SCHED.COSINE_T_MAX
            - TRAIN.SCHED.COSINE_ETA_MIN
            - ... (other arguments of the scheduler)
    """
    cfg = cfg.TRAIN.SCHED
    if cfg.NAME == 'CosineAnnealingLR':
        scheduler = CosineAnnealingLR(
            optimizer=optimizer,
            T_max=cfg.COSINE_T_MAX,
            eta_min=cfg.COSINE_ETA_MIN,
        )
    elif cfg.NAME == 'MultiStepLR':
        scheduler = MultiStepLR(
            optimizer=optimizer,
            milestones=cfg.MULTISTEP_MILESTONES,
            gamma=cfg.MULTISTEP_GAMMA,
        )
    else:
        raise ValueError(f"Scheduler {cfg.NAME} is not supported.")

    if hasattr(cfg, 'WARMUP_STEPS') and cfg.WARMUP_STEPS > 0:
        scheduler = LRWarmupWrapper(
            torch_scheduler=scheduler,
            warmup_steps=cfg.WARMUP_STEPS,
            warmup_factor=cfg.WARMUP_FACTOR,
        )

    return scheduler


class LRWarmupWrapper:
    """
    This class wraps the standard PyTorch LR scheduler to support warmup.
    Simplified from https://github.com/serend1p1ty/core-pytorch-utils/blob/main/cpu/lr_scheduler.py
    """
    def __init__(self, torch_scheduler, warmup_steps: int = 0, warmup_factor: float = 0.1):
        """
        Args:
            torch_scheduler (_LRScheduler): torch.optim.lr_scheduler._LRScheduler
            warmup_steps (int): Number of update steps in warmup stage.
            warmup_factor (float): The factor of initial warmup lr relative to base lr. Defaults to 0.1.
        """
        self.torch_scheduler = torch_scheduler
        assert isinstance(warmup_steps, int) and isinstance(warmup_factor, float)
        assert warmup_steps > 0 and warmup_factor < 1, f'Warmup only works when warmup_steps > 0 and warmup_factor < 1'
        self.warmup_steps = warmup_steps
        self.warmup_factor = warmup_factor

        self.param_groups = self.torch_scheduler.optimizer.param_groups
        self.base_lrs = [param_group['lr'] for param_group in self.param_groups]
        self.regular_lrs_in_warmup = self._pre_compute_regular_lrs_in_warmup()

        self.last_step = 0

        self._set_lrs([base_lr * warmup_factor for base_lr in self.base_lrs])

    def _pre_compute_regular_lrs_in_warmup(self):
        regular_lrs_in_warmup = [self.base_lrs]
        for _ in range(self.warmup_steps):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self.torch_scheduler.step()
            regular_lrs_in_warmup.append([param_group['lr'] for param_group in self.param_groups])
        return regular_lrs_in_warmup

    def _set_lrs(self, lrs: List[float]):
        for param_group, lr in zip(self.param_groups, lrs):
            param_group['lr'] = lr

    def _get_warmup_lrs(self, cur_step: int, regular_lrs: List[float]):
        alpha = cur_step / self.warmup_steps
        factor = self.warmup_factor * (1 - alpha) + alpha
        return [lr * factor for lr in regular_lrs]

    def step(self):
        self.last_step += 1
        if self.last_step < self.warmup_steps:
            self._set_lrs(self._get_warmup_lrs(self.last_step, self.regular_lrs_in_warmup[self.last_step]))
        elif self.last_step == self.warmup_steps:
            self._set_lrs(self.regular_lrs_in_warmup[-1])
        else:
            self.torch_scheduler.step()

    def state_dict(self):
        state = {key: value for key, value in self.__dict__.items() if key != "torch_scheduler"}
        state["torch_scheduler"] = self.torch_scheduler.state_dict()
        return state

    def load_state_dict(self, state_dict: Dict[str, Any]):
        self.torch_scheduler.load_state_dict(state_dict.pop("torch_scheduler"))
        self.__dict__.update(state_dict)


def _test(scheduler_type='CosineAnnealingLR'):
    model = nn.Linear(3, 4)
    optimizer = optim.SGD(model.parameters(), lr=0.1)
    optimizer_warmup = optim.SGD(model.parameters(), lr=0.1)

    cfg = CN()
    cfg.TRAIN = CN()
    cfg.TRAIN.SCHED = CN()
    cfg.TRAIN.SCHED.NAME = scheduler_type
    cfg.TRAIN.SCHED.COSINE_T_MAX = 100
    cfg.TRAIN.SCHED.COSINE_ETA_MIN = 0.001
    cfg.TRAIN.SCHED.MULTISTEP_MILESTONES = [60, 80]
    cfg.TRAIN.SCHED.MULTISTEP_GAMMA = 0.1
    scheduler = build_scheduler(optimizer, cfg)
    scheduler_warmup = build_scheduler(optimizer_warmup, cfg)
    scheduler_warmup = LRWarmupWrapper(scheduler_warmup, warmup_steps=40, warmup_factor=0.01)

    epochs = range(100)
    lrs, lrs_warmup = [], []
    for _ in epochs:
        lrs.append(optimizer.param_groups[0]['lr'])
        lrs_warmup.append(optimizer_warmup.param_groups[0]['lr'])
        scheduler.step()
        scheduler_warmup.step()
    print(lrs, lrs_warmup)
    plt.plot(epochs, lrs)
    plt.plot(epochs, lrs_warmup)
    plt.show()


if __name__ == '__main__':
    import torch.nn as nn
    import torch.optim as optim
    import matplotlib.pyplot as plt

    _test('CosineAnnealingLR')
    _test('MultiStepLR')
