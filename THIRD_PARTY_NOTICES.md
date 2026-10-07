# Third-party notices

`autoclip/rrdb.py` adapts the inference architecture in [BasicSR rrdbnet_arch.py](https://github.com/XPixelGroup/BasicSR/blob/master/basicsr/archs/rrdbnet_arch.py), Copyright 2018-2022 BasicSR Authors, Apache License 2.0. The full license is in `licenses/BasicSR-Apache-2.0.txt`. Training initialization, registries and x1/x2 support were removed; block construction was simplified.

The supplied RealESRGAN_x4plus.safetensors model is read from the user's existing installation. It is not distributed by this project. Project: [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN).

PyTorch, NumPy, Pillow, safetensors and OpenCV retain their respective distribution licenses. This project does not copy ComfyUI or its custom nodes into the inference environment.
