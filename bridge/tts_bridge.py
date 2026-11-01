#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tts_bridge.py —— 把任意本地 TTS 引擎接成一个「可被直播软件单独采集」的语音输出

## 为什么需要这一层（实现原理）

做 TTS 最容易卡住的不是"合成"，是**"怎么让 OBS（或别的录制软件）只录到它"**。
三条常见路线，各有硬伤：

  ① 直接让 TTS 出声 → OBS 录「桌面音频」
     会把音乐、游戏、系统提示音一起录进去。不可用。
  ② 装虚拟声卡（VB-Audio 之类），TTS 输出到虚拟设备，OBS 采那个设备
     能用，但要装驱动、要改系统默认设备、多程序共存时容易互相抢。对普通用户门槛高。
  ③ **合成到文件 → 由一个"可见窗口"播放 → OBS 用「应用音频采集」抓这个窗口**
     不需要驱动、不影响系统音量、多个程序可以各占一个窗口。
     **本仓库走这条。**

代价是必须有一个**真实的、可见的窗口**在播（OBS 的窗口音频采集依赖进程的音频会话）。
所以这套结构天然是"两段式"：

    调用方 --HTTP--> [bridge 合成] --文件--> [player 窗口播放] <--采集-- OBS

## 为什么用「文件队列」而不是管道/内存队列

- **跨进程**：合成是 Python，播放是 PowerShell，两边语言不同，最省事的接口就是目录
- **可观察**：`ls 队列目录` 就能知道积压了几条，排查时不用加日志
- **可重放**：出问题时那几个 wav 还在，能直接听、直接复现
- **可恢复**：bridge 崩了，播放器继续把队列播完，反过来也一样

缺点也认了：磁盘上会留文件（所以有 `--keep` 限制条数）、元数据只能靠文件名。

## 预热（冷启动 12 秒）

TTS 模型第一次加载很慢（实测首句约 12 秒，之后 2–3 秒）。
所以提供 `POST /warmup`：服务起来后先合成一句固定短语，把模型拉进内存，
**别让用户/观众等到第一句话时才付这笔时间**。

## 抢占（preempt）

语音场景里"新的一句话盖掉旧的"是常态（直播弹幕、语音助手被打断）。
这里用标记文件实现：`POST /tts {"preempt": true}` 会写一个 `_preempt` 标记，
播放器看到就清空队列并停止当前播放。用文件而不是信号，是为了不依赖平台。

## 引擎适配

引擎只要满足一种形态就能接：

- **命令行**：给一个模板，例如 `--engine-cmd 'python engine.py --text "{text}" --out "{out}"'`
- **HTTP**：给一个地址，例如 `--engine-http http://127.0.0.1:9880/tts`（POST JSON）

两者都不关心引擎是什么：GPT-SoVITS、Edge-TTS、Piper、自训练模型都行。
**本仓库不附带任何模型、权重或音频素材**（见 README 的使用条件）。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = '1.0.0'

# Windows 上 stdout 被重定向到文件时，默认编码是 charmap（cp1252/cp936 之类），
# print() 中文会直接抛 UnicodeEncodeError 把服务干掉。
# 这不是"日志难看"，是**起不来** —— CI 上真栽过一次。
# 所以：能改就改成 UTF-8，改不了就退化成 replace，绝不让日志拖死主流程。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# ---------------------------------------------------------------- 配置


class Config:
    def __init__(self, a):
        self.port = a.port
        self.queue = os.path.abspath(a.queue)
        self.outdir = os.path.abspath(a.outdir)
        self.engine_cmd = a.engine_cmd or ''
        # 启动时就切好，运行期不再让 shell 参与（见 synth 里的 TTS-01 说明）
        import shlex
        self.engine_argv = shlex.split(self.engine_cmd) if self.engine_cmd else []
        self.max_body = a.max_body
        self.engine_http = a.engine_http or ''
        self.engine_timeout = a.engine_timeout
        self.keep = a.keep
        self.ffmpeg = a.ffmpeg or shutil.which('ffmpeg') or ''
        self.target_sr = a.target_sr
        self.warmup_text = a.warmup_text
        os.makedirs(self.queue, exist_ok=True)
        os.makedirs(self.outdir, exist_ok=True)

    @property
    def status_file(self):
        return os.path.join(self.outdir, '_status.txt')

    @property
    def preempt_file(self):
        return os.path.join(self.queue, '_preempt')


STATE = {
    'boot_at': time.time(),
    'engine_ready': False,
    'last_error': '',
    'synth_count': 0,
    'last_ms': None,
}
LOCK = threading.Lock()


def log(msg):
    line = '%s  %s' % (time.strftime('%H:%M:%S'), msg)
    try:
        print(line, flush=True)
    except Exception:
        try:
            print(line.encode('utf-8', 'replace').decode('ascii', 'replace'), flush=True)
        except Exception:
            pass   # 日志怎么写不是业务；业务不能因为日志死
    try:
        with open(os.path.join(CFG.outdir, 'bridge.log'), 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def write_status(text):
    """给人看的单行状态（排障第一现场，不用翻日志）"""
    try:
        with open(CFG.status_file, 'w', encoding='utf-8') as f:
            f.write('%s  %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), text))
    except Exception:
        pass


# ---------------------------------------------------------------- 引擎


def synth(text, out_wav):
    """
    合成一句话到 out_wav。返回耗时毫秒。

    两种引擎形态都走这里，调用方不关心底下是什么。
    """
    t0 = time.time()
    if CFG.engine_cmd:
        # TTS-01（外部审查 2026-11-01，高）：原来把请求文本插进字符串再交给 shell 解释，
        # 只要 text 里有引号/分号/`$()` 就能注入命令，而这个 HTTP 端点默认没有认证。
        # 现在：模板按 shell 语法**切分成 argv 一次**，占位符在**单个参数内部**替换，
        # 然后 `shell=False` 直接 exec —— 文本里有什么字符都只是参数内容。
        argv = [a.replace('{text}', text).replace('{out}', out_wav)
                for a in CFG.engine_argv]
        r = subprocess.run(argv, shell=False, capture_output=True, timeout=CFG.engine_timeout)
        if r.returncode != 0:
            err = (r.stderr or b'').decode('utf-8', 'replace')[-400:]
            raise RuntimeError('engine 退出码 %d: %s' % (r.returncode, err))
    elif CFG.engine_http:
        import urllib.request
        body = json.dumps({'text': text, 'out': out_wav}).encode('utf-8')
        req = urllib.request.Request(CFG.engine_http, data=body,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=CFG.engine_timeout) as resp:
            raw = resp.read()
            # HTTP 引擎可以直接回音频字节，也可以回 JSON 里带路径
            if raw[:4] == b'RIFF':
                with open(out_wav, 'wb') as f:
                    f.write(raw)
            else:
                j = json.loads(raw.decode('utf-8'))
                p = j.get('out') or j.get('path')
                if p and os.path.abspath(p) != os.path.abspath(out_wav):
                    shutil.copyfile(p, out_wav)
    else:
        raise RuntimeError('没有配置引擎（--engine-cmd 或 --engine-http 至少给一个）')

    if not os.path.exists(out_wav) or os.path.getsize(out_wav) < 128:
        raise RuntimeError('引擎没有产出有效的 wav')

    # 可选：重采样到播放器能直接吃的格式（有些引擎回 24k/32k，SoundPlayer 会拒）
    if CFG.ffmpeg and CFG.target_sr:
        tmp = out_wav + '.conv.wav'
        try:
            subprocess.run([CFG.ffmpeg, '-v', 'error', '-y', '-i', out_wav,
                            '-ar', str(CFG.target_sr), '-ac', '1', '-c:a', 'pcm_s16le', tmp],
                           check=True, capture_output=True, timeout=60)
            os.replace(tmp, out_wav)
        except Exception as e:
            log('重采样失败（保留原文件）: %s' % e)
            if os.path.exists(tmp):
                os.remove(tmp)
    return int((time.time() - t0) * 1000)


def enqueue(wav, text):
    """
    放进队列。

    TTS-05（外部审查 2026-11-01）：原来用 `count % 100000` 做前缀，取模会回绕 ——
    绕回之后新任务的编号比旧积压任务小，按文件名排序就插到队首去了。
    现在用 **13 位毫秒时间戳**做前缀（单调、不回绕、跨重启也对），
    文件名排序即时间顺序。

    TTS-02：原来先 move wav 再写 .txt，播放器有机会看到"有音频没文本"的半成品。
    现在**先写 txt，最后把 wav rename 进队列** —— wav 的出现就是"任务已就绪"的信号。
    """
    with LOCK:
        STATE['synth_count'] += 1
    name = '%013d_%06d.wav' % (int(time.time() * 1000), STATE['synth_count'] % 1000000)
    dest = os.path.join(CFG.queue, name)
    txt = dest + '.txt'
    tmp_txt = txt + '.part'
    with open(tmp_txt, 'w', encoding='utf-8') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_txt, txt)          # 元数据先就位
    tmp_wav = dest + '.part'
    shutil.move(wav, tmp_wav)
    os.replace(tmp_wav, dest)         # wav 最后出现 = 就绪信号
    prune()
    return dest


def prune():
    """只留最近 keep 条，别把磁盘塞满（队列+元数据成对删）"""
    try:
        wavs = sorted(f for f in os.listdir(CFG.queue) if f.endswith('.wav'))
        for old in wavs[:-CFG.keep] if CFG.keep > 0 else []:
            for p in (os.path.join(CFG.queue, old), os.path.join(CFG.queue, old + '.txt')):
                if os.path.exists(p):
                    os.remove(p)
        # 清理崩溃留下的半成品（.part 超过 1 分钟才算残留）
        now = time.time()
        for f in os.listdir(CFG.queue):
            if f.endswith('.part'):
                p = os.path.join(CFG.queue, f)
                try:
                    if now - os.path.getmtime(p) > 60:
                        os.remove(p)
                except Exception:
                    pass
    except Exception:
        pass


def warmup():
    """把模型拉进内存。冷启动实测首句约 12 秒，别让第一个用户付这笔钱。"""
    if not CFG.warmup_text:
        return {'ok': False, 'skipped': 'no warmup text'}
    out = os.path.join(CFG.outdir, '_warmup.wav')
    try:
        ms = synth(CFG.warmup_text, out)
        STATE['engine_ready'] = True
        log('预热完成：%d ms' % ms)
        if os.path.exists(out):
            os.remove(out)
        return {'ok': True, 'ms': ms}
    except Exception as e:
        STATE['last_error'] = str(e)
        log('预热失败: %s' % e)
        return {'ok': False, 'error': str(e)}


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = 'tts-bridge/' + VERSION
    protocol_version = 'HTTP/1.1'

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *a):
        pass  # 用我们自己的日志，别让 http.server 刷屏

    def do_GET(self):
        if self.path == '/health':
            q = len([f for f in os.listdir(CFG.queue) if f.endswith('.wav')])
            return self._send(200, {
                'ok': True, 'version': VERSION,
                'uptime_s': int(time.time() - STATE['boot_at']),
                'engine_ready': STATE['engine_ready'],
                'queue_len': q, 'synth_count': STATE['synth_count'],
                'last_ms': STATE['last_ms'], 'last_error': STATE['last_error'],
                'engine': 'cmd' if CFG.engine_cmd else ('http' if CFG.engine_http else 'none'),
            })
        return self._send(404, {'ok': False, 'error': 'not found'})

    def do_POST(self):
        # TTS-06（外部审查 2026-11-01）：原来按 Content-Length 一次性读完整请求体，
        # 任何客户端都能用一个超大长度把内存吃干（端点默认无认证）。
        raw_len = self.headers.get('Content-Length')
        try:
            n = int(raw_len or 0)
        except ValueError:
            return self._send(400, {'ok': False, 'error': 'Content-Length 不是整数'})
        if n > CFG.max_body:
            return self._send(413, {
                'ok': False,
                'error': '请求体过大：%d 字节 > 上限 %d' % (n, CFG.max_body),
            })
        raw = self.rfile.read(n) if n else b'{}'
        try:
            body = json.loads(raw.decode('utf-8'))
        except Exception:
            return self._send(400, {'ok': False, 'error': 'body 不是合法 JSON'})

        if self.path == '/warmup':
            return self._send(200, warmup())

        if self.path != '/tts':
            return self._send(404, {'ok': False, 'error': 'not found'})

        text = re.sub(r'\s+', ' ', str(body.get('text') or '')).strip()
        if not text:
            return self._send(400, {'ok': False, 'error': 'text 为空'})
        if len(text) > 500:
            return self._send(400, {'ok': False, 'error': 'text 超过 500 字，请先分段'})

        try:
            if body.get('preempt'):
                # 抢占：标记文件，播放器看到就清队并停当前
                with open(CFG.preempt_file, 'w', encoding='utf-8') as f:
                    f.write(str(time.time()))
            out = os.path.join(CFG.outdir, '_tmp_%d.wav' % int(time.time() * 1000))
            ms = synth(text, out)
            dest = enqueue(out, text)
            with LOCK:
                STATE['last_ms'] = ms
            STATE['engine_ready'] = True
            write_status('合成 %d ms · 队列 %s' % (ms, os.path.basename(dest)))
            return self._send(200, {
                'ok': True, 'ms': ms, 'queued': os.path.basename(dest),
                'preempt': bool(body.get('preempt')),
            })
        except Exception as e:
            STATE['last_error'] = str(e)
            write_status('合成失败：%s' % e)
            log('合成失败: %s' % e)
            return self._send(500, {'ok': False, 'error': str(e)})


CFG = None


def main():
    global CFG
    ap = argparse.ArgumentParser(description='本地 TTS HTTP 桥：合成 → 队列 → 窗口播放 → OBS 采集')
    ap.add_argument('--port', type=int, default=8770)
    ap.add_argument('--queue', default='./播放队列', help='播放器监视的目录')
    ap.add_argument('--outdir', default='./输出', help='临时文件与日志')
    ap.add_argument('--engine-cmd', default='', help='命令行引擎模板，含 {text} 与 {out}')
    ap.add_argument('--engine-http', default='', help='HTTP 引擎地址（POST JSON）')
    ap.add_argument('--engine-timeout', type=float, default=120)
    ap.add_argument('--keep', type=int, default=200, help='队列最多保留多少条（0=不限）')
    ap.add_argument('--max-body', type=int, default=65536,
                    help='TTS-06：请求体字节上限，超过返回 413（默认 64 KiB）')
    ap.add_argument('--ffmpeg', default='', help='可选：用于统一采样率')
    ap.add_argument('--target-sr', type=int, default=32000, help='重采样目标（0=不转）')
    ap.add_argument('--warmup-text', default='预热。', help='启动后自动预热的一句；空字符串则跳过')
    ap.add_argument('--warmup', action='store_true', help='启动时自动预热')
    a = ap.parse_args()

    if not a.engine_cmd and not a.engine_http:
        print('必须提供 --engine-cmd 或 --engine-http', file=sys.stderr)
        return 2

    CFG = Config(a)
    log('tts-bridge %s 启动：port=%d queue=%s' % (VERSION, CFG.port, CFG.queue))
    write_status('已启动，等待合成')
    srv = ThreadingHTTPServer(('127.0.0.1', CFG.port), Handler)
    if a.warmup:
        threading.Thread(target=warmup, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
