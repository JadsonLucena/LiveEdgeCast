#!/usr/bin/env python3
import os,time
from http.server import BaseHTTPRequestHandler,HTTPServer
from prometheus_client import Gauge, Counter, generate_latest

STREAM_KEY=os.getenv('STREAM_KEY','unknown')
PROGRESS=f"/tmp/ffmpeg_{STREAM_KEY}.progress"
PID=f"/tmp/ffmpeg_{STREAM_KEY}.pid"
EXIT=f"/tmp/ffmpeg_{STREAM_KEY}.exit"

running=Gauge('worker_ffmpeg_running','FFmpeg running state')
health=Gauge('worker_ffmpeg_health_state','FFmpeg health state 0/1')
last_ts=Gauge('worker_ffmpeg_last_progress_timestamp_seconds','Last progress timestamp')
age=Gauge('worker_ffmpeg_progress_age_seconds','Seconds since last progress')
out_time=Gauge('worker_ffmpeg_out_time_seconds','FFmpeg out_time seconds')
size=Gauge('worker_ffmpeg_total_size_bytes','FFmpeg total size bytes')
speed=Gauge('worker_ffmpeg_speed','FFmpeg speed factor')
exit_total=Counter('worker_ffmpeg_exit_total','FFmpeg exits',['exit_code'])
seen=set()

def collect():
    now=time.time(); data={}
    if os.path.exists(PROGRESS):
        for ln in open(PROGRESS):
            if '=' in ln:
                k,v=ln.strip().split('=',1); data[k]=v
        last_ts.set(now); age.set(0); health.set(1)
    else:
        health.set(0)
    run=1 if os.path.exists(PID) else 0
    running.set(run)
    if run==0 and os.path.exists(EXIT):
        code=open(EXIT).read().strip() or 'unknown'
        if code not in seen:
            exit_total.labels(exit_code=code).inc(); seen.add(code)
    ot=data.get('out_time_ms','0')
    try: out_time.set(float(ot)/1_000_000)
    except: pass
    try: size.set(float(data.get('total_size','0')))
    except: pass
    try: speed.set(float((data.get('speed','0x')).replace('x','')))
    except: pass

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path!='/metrics': self.send_response(404); self.end_headers(); return
        collect(); b=generate_latest(); self.send_response(200)
        self.send_header('Content-Type','text/plain; version=0.0.4'); self.end_headers(); self.wfile.write(b)

HTTPServer(('',9113),H).serve_forever()
