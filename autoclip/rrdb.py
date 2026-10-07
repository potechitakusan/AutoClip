"""Inference-only x4 RRDBNet compatible with BasicSR/Real-ESRGAN weights.

Adapted from BasicSR's rrdbnet_arch.py, Copyright 2018-2022 BasicSR Authors,
Apache-2.0 (see THIRD_PARTY_NOTICES.md and licenses/BasicSR-Apache-2.0.txt).
Changes: x4/RGB-only, compact block construction, no registry/training utilities.
"""
import torch
from torch import nn
from torch.nn import functional as F


class DenseBlock(nn.Module):
    def __init__(self):
        super().__init__()
        for i in range(5):
            setattr(self, f"conv{i+1}", nn.Conv2d(64+i*32, 64 if i == 4 else 32, 3, padding=1))

    def forward(self, x):
        features = [x]
        for i in range(4):
            features.append(F.leaky_relu(getattr(self, f"conv{i+1}")(torch.cat(features, 1)), .2))
        return x + .2 * self.conv5(torch.cat(features, 1))


class RRDB(nn.Module):
    def __init__(self):
        super().__init__()
        self.rdb1, self.rdb2, self.rdb3 = DenseBlock(), DenseBlock(), DenseBlock()

    def forward(self, x):
        return x + .2 * self.rdb3(self.rdb2(self.rdb1(x)))


class RRDBNet(nn.Module):
    def __init__(self, num_block=23):
        super().__init__()
        self.conv_first = nn.Conv2d(3, 64, 3, padding=1)
        self.body = nn.Sequential(*(RRDB() for _ in range(num_block)))
        for name in ("conv_body", "conv_up1", "conv_up2", "conv_hr"):
            setattr(self, name, nn.Conv2d(64, 64, 3, padding=1))
        self.conv_last = nn.Conv2d(64, 3, 3, padding=1)

    def forward(self, x):
        feature = self.conv_first(x)
        feature = feature + self.conv_body(self.body(feature))
        for layer in (self.conv_up1, self.conv_up2):
            feature = F.leaky_relu(layer(F.interpolate(feature, scale_factor=2, mode="nearest")), .2)
        return self.conv_last(F.leaky_relu(self.conv_hr(feature), .2))
