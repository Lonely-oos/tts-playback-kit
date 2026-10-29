#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fake_engine.py —— 假引擎：把文字"合成"成一段正弦波

存在的意义有两个：

1. **给这套结构一个不需要任何模型的运行示例**。想验证"队列/OBS/播放器"这条链的人，
   不用先去装 GPT-SoVITS 或下载几百兆权重。
2. **给 CI 一个能在 Linux 上跑的引擎**。真引擎几乎都要 GPU、要权重、要几十秒加载，
   不适合放进每次提交的测试里。

## 实现原理（为什么是一个"能听出差别"的正弦波）

不用静音，是因为静音没法验证"播的确实是这一条"。这里让频率随文本变化：
同样的文字 → 同样的音高，不同的文字 → 明显不同的音高。
这样人耳一听就知道队列顺序对不对、有没有串。

用法：
    python fake_engine.py --text "你好" --out out.wav
    python fake_engine.py --text "你好" --out out.wav --ms 600
"""
import argparse
import math
import struct
import wave

SR = 24000          # 引擎输出 24k，交给 bridge 去重采样（顺便验证重采样那条路）
BASE_HZ = 220.0     # A3


def text_to_freq(text):
    """文本 → 频率：让不同文本听起来明显不同，但都落在人声附近的悦耳区间。"""
    h = 0
    for ch in text:
        h = (h * 131 + ord(ch)) % 1000003
    return BASE_HZ * (1.0 + (h % 12) / 12.0)     # 220 ~ 440 Hz


def write_wav(path, ms, text):
    n = int(SR * ms / 1000)
    f = text_to_freq(text)
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        frames = bytearray()
        for i in range(n):
            # 加一点淡入淡出，避免爆音；振幅随文本略变，进一步区分不同句子
            amp = 0.25 + 0.1 * (len(text) % 3)
            env = min(1.0, i / (SR * 0.02), (n - i) / (SR * 0.02))
            v = amp * env * math.sin(2 * math.pi * f * i / SR)
            frames += struct.pack('<h', int(v * 32767))
        w.writeframes(bytes(frames))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--text', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--ms', type=int, default=500, help='音频时长（毫秒）')
    a = ap.parse_args()
    write_wav(a.out, max(60, a.ms), a.text)
    print('ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
