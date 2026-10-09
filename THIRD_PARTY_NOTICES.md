# Third-party notices

`autoclip/rrdb.py` adapts the inference architecture in [BasicSR rrdbnet_arch.py](https://github.com/XPixelGroup/BasicSR/blob/master/basicsr/archs/rrdbnet_arch.py), Copyright 2018-2022 BasicSR Authors, Apache License 2.0. The full license is in `licenses/BasicSR-Apache-2.0.txt`. Training initialization, registries and x1/x2 support were removed; block construction was simplified.

The RealESRGAN_x4plus model is not distributed by this project. It is used in one of two ways: a `.safetensors` file the user already has (for example in an existing ComfyUI / StabilityMatrix installation) is read in place, or, only if the user chooses the built-in environment, `autoclip fetch-model` downloads the official `RealESRGAN_x4plus.pth` (release v0.1.0 of [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN), BSD 3-Clause License) from GitHub. The download is checked against a pinned SHA-256 value, opened only with `torch.load(weights_only=True)` and converted to `.safetensors` on the user's computer. Review the upstream license before redistributing the weights.

PyTorch, NumPy, Pillow, safetensors and OpenCV retain their respective distribution licenses. This project does not copy ComfyUI or its custom nodes into the inference environment.
