#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_roundtrip.py —— 端到端：起 bridge（假引擎）→ 发一句话 → 断言 wav 真的进了队列

为什么用假引擎：真引擎要权重、要 GPU、加载几十秒，不适合每次提交都跑。
但**"HTTP → 合成 → 落队列 → 可被播放器看见"这条链**与引擎无关，所以用正弦波也能验。

CI 上跑这个（ubuntu + windows 都跑）。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave

# Windows 上 stdout 被重定向时默认是 charmap（cp1252/cp936），print 中文会抛
# UnicodeEncodeError。CI 里连测试脚本自己都因此挂了 —— 所有会打印中文的入口都要这一句。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BRIDGE = os.path.join(ROOT, 'bridge', 'tts_bridge.py')
ENGINE = os.path.join(ROOT, 'bridge', 'fake_engine.py')

FAILS = []


def pick_port():
    """让系统给一个空闲端口。

    为什么不写死 8791：GitHub 的 Windows runner 上有一段被 Hyper-V 保留的动态端口，
    写死端口可能直接 bind 失败（WinError 10013），而本地机器往往没有这段保留。
    第一次 CI 就栽在这里 —— 桥根本没起来。
    """
    import socket
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = None
BASE = None


def ok(cond, msg):
    print(('  ok   ' if cond else '  FAIL ') + msg)
    if not cond:
        FAILS.append(msg)


def post(path, obj, timeout=30):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(obj).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8'))


def get(path, timeout=10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def wait_up(deadline=25, logfile=None, proc=None):
    t0 = time.time()
    while time.time() - t0 < deadline:
        try:
            return get('/health')
        except Exception:
            if proc is not None and proc.poll() is not None:
                break
            time.sleep(0.3)
    detail = ''
    if logfile and os.path.exists(logfile):
        detail = '\n----- bridge 输出 -----\n' + open(logfile, encoding='utf-8', errors='replace').read()[-3000:]
    raise RuntimeError('bridge 没有起来（端口 %s）%s' % (PORT, detail))


def main():
    global PORT, BASE
    tmp = tempfile.mkdtemp(prefix='tts-kit-')
    queue = os.path.join(tmp, 'queue')
    outdir = os.path.join(tmp, 'out')
    logfile = os.path.join(tmp, 'bridge.log')
    cmd = '"%s" "%s" --text "{text}" --out "{out}" --ms 300' % (sys.executable, ENGINE)

    # 端口被系统保留时换一个再来（最多 3 次）
    proc = None
    for attempt in range(3):
        PORT = pick_port()
        BASE = 'http://127.0.0.1:%d' % PORT
        args = [sys.executable, BRIDGE, '--port', str(PORT), '--queue', queue, '--outdir', outdir,
                '--engine-cmd', cmd, '--keep', '5', '--target-sr', '0']
        lf = open(logfile, 'w', encoding='utf-8')
        proc = subprocess.Popen(args, stdout=lf, stderr=subprocess.STDOUT)
        try:
            h = wait_up(deadline=15, logfile=logfile, proc=proc)
            break
        except RuntimeError:
            if attempt == 2:
                raise
            print('  端口 %d 起不来，换一个重试' % PORT)
            try:
                proc.terminate(); proc.wait(timeout=5)
            except Exception:
                proc.kill()
    try:
        ok(h.get('ok') is True, '/health 返回 ok')
        ok(h.get('engine') == 'cmd', '/health 报告引擎形态是 cmd')
        ok(h.get('queue_len') == 0, '开始时队列为空')

        code, r = post('/tts', {'text': '第一句：你好呀'})
        ok(code == 200 and r.get('ok'), 'POST /tts 成功（%d）' % code)
        ok(isinstance(r.get('ms'), int) and r['ms'] >= 0, '返回了合成耗时 ms=%s' % r.get('ms'))

        wavs = sorted(f for f in os.listdir(queue) if f.endswith('.wav'))
        ok(len(wavs) == 1, '队列里出现 1 个 wav（实得 %d）' % len(wavs))

        p = os.path.join(queue, wavs[0])
        with open(p, 'rb') as f:
            head = f.read(4)
        ok(head == b'RIFF', '文件是合法 wav（RIFF 头）')
        with wave.open(p) as w:
            ok(w.getnchannels() == 1, '单声道')
            ok(w.getframerate() == 24000, '引擎原始采样率 24000（未重采样时保持原样）')
            ok(w.getnframes() > 1000, '有实际音频数据（%d 帧）' % w.getnframes())

        txt = p + '.txt'
        ok(os.path.exists(txt), '元数据 .txt 与 wav 成对出现')
        if os.path.exists(txt):
            ok('你好' in open(txt, encoding='utf-8').read(), '元数据里存了原文')

        h = get('/health')
        ok(h['queue_len'] == 1 and h['synth_count'] == 1, '/health 计数正确（队列1/合成1）')

        code, r = post('/tts', {'text': '第二句：抢占测试', 'preempt': True})
        ok(code == 200 and r.get('preempt') is True, '带 preempt 的请求成功')
        ok(os.path.exists(os.path.join(queue, '_preempt')), '写出了 _preempt 抢占标记')

        code, r = post('/tts', {'text': ''})
        ok(code == 400, '空文本被拒绝（%d）' % code)

        code, r = post('/tts', {'text': 'x' * 600})
        ok(code == 400, '超长文本被拒绝（%d）' % code)

        code, r = post('/warmup', {})
        ok(code == 200, '/warmup 有响应')

        # ── TTS-01：命令注入（外部审查 2026-11-01）────────────────
        marker = os.path.join(tmp, 'pwned.txt')
        evil = 'x"; touch "%s"; echo "' % marker
        code, r = post('/tts', {'text': evil})
        ok(code == 200, 'TTS-01 含引号/分号的文本仍被当作普通文本（%d）' % code)
        ok(not os.path.exists(marker), 'TTS-01 没有产生命令注入副作用文件')
        txts = sorted(f for f in os.listdir(queue) if f.endswith('.txt'))
        ok(any('touch' in open(os.path.join(queue, f), encoding='utf-8').read() for f in txts),
           'TTS-01 文本里的命令字样原样留在元数据里（说明没被 shell 执行）')

        # ── TTS-06：请求体上限 ────────────────────────────────────
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', PORT, timeout=10)
        conn.request('POST', '/tts', body=b'x' * 200000,
                     headers={'Content-Type': 'application/json', 'Content-Length': '200000'})
        resp = conn.getresponse()
        ok(resp.status == 413, 'TTS-06 超大请求体被拒绝（%d）' % resp.status)
        conn.close()

        # ── TTS-05 / TTS-02：序号单调 + 原子交接 ──────────────────
        for i in range(3):
            post('/tts', {'text': '排序测试 %d' % i})
        wavs2 = sorted(f for f in os.listdir(queue) if f.endswith('.wav'))
        ids = [int(f.split('_')[0]) for f in wavs2]
        ok(ids == sorted(ids), 'TTS-05 文件名前缀单调递增（%s）' % ids[:3])
        ok(all(len(str(i)) == 13 for i in ids), 'TTS-05 前缀是 13 位毫秒时间戳，不回绕')
        ok(all(os.path.exists(os.path.join(queue, f + '.txt')) for f in wavs2),
           'TTS-02 每个 wav 都有对应 txt（wav 出现即就绪）')
        ok(not [f for f in os.listdir(queue) if f.endswith('.part')], 'TTS-02 没有残留 .part 半成品')

        print('\n' + ('===== %d FAILED =====' % len(FAILS) if FAILS else '===== all checks passed ====='))
        return 1 if FAILS else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == '__main__':
    sys.exit(main())
