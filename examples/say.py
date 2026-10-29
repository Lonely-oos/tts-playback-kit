#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""say.py —— 最小调用示例：让 bridge 说一句话，并打印耗时。"""
import json
import sys
import urllib.request

BASE = sys.argv[2] if len(sys.argv) > 2 else 'http://127.0.0.1:8770'
text = sys.argv[1] if len(sys.argv) > 1 else '你好，这是一句测试。'

body = json.dumps({'text': text, 'preempt': False}).encode('utf-8')
req = urllib.request.Request(BASE + '/tts', data=body,
                             headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(req, timeout=180) as r:
    print(json.dumps(json.loads(r.read().decode('utf-8')), ensure_ascii=False, indent=2))