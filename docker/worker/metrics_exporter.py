#!/usr/bin/env python3
import os,time
from http.server import BaseHTTPRequestHandler, HTTPServer
from prometheus_client import CollectorRegistry, Gauge, Counter, generate_latest

STREAM_KEY=os.getenv('STREAM_KEY','unknown')
PROGRESS_FILE=f"/tmp/ffmpeg_{STREAM_KEY}.progress"
PID_FILE=f"/tmp/ffmpeg_{STREAM_KEY}.pid"
EXIT_FILE=f"/tmp/ffmpeg_{STREAM_KEY}.exit"

reg=CollectorRegistry()
g_running=Gauge('worker_ffmpeg_running','FFmpeg process running',registry=reg)
g_state=Gauge('worker_ffmpeg_health_state','0=down,1=running,2=stale',registry=reg)
g_last=Gauge('worker_ffmpeg_last_progress_timestamp_seconds','Last progress timestamp',registry=reg)
g_age=Gauge('worker_ffmpeg_progress_age_seconds','Age of latest progress',registry=reg)
g_out=Gauge('worker_ffmpeg_out_time_seconds','FFmpeg out_time in seconds',registry=reg)
g_size=Gauge('worker_ffmpeg_total_size_bytes','FFmpeg total_size bytes',registry=reg)
g_speed=Gauge('worker_ffmpeg_speed','FFmpeg speed ratio',registry=reg)
c_exit=Counter('worker_ffmpeg_exit_total','FFmpeg exits',['exit_code'],registry=reg)
_seen=set()

def parse_progress():
    data={}
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            for line in f:
                if '=' in line:
                    k,v=line.strip().split('=',1); data[k]=v
    return data

def update():
    now=time.time(); p=parse_progress()
    pid_running=False
    if os.path.exists(PID_FILE):
        try:
            pid=int(open(PID_FILE).read().strip()); os.kill(pid,0); pid_running=True
        except Exception: pid_running=False
    g_running.set(1 if pid_running else 0)
    if p:
        ts=os.path.getmtime(PROGRESS_FILE); age=max(0,now-ts)
        g_last.set(ts); g_age.set(age)
        g_state.set(1 if age < 15 and pid_running else 2)
        out_us=float(p.get('out_time_us','0') or 0); g_out.set(out_us/1_000_000)
        g_size.set(float(p.get('total_size','0') or 0))
        s=(p.get('speed','0x') or '0x').replace('x','')
        try:g_speed.set(float(s))
        except: g_speed.set(0)
    else:
        g_state.set(0 if not pid_running else 2)
    if os.path.exists(EXIT_FILE):
        code=open(EXIT_FILE).read().strip() or 'unknown'
        if code not in _seen:
            c_exit.labels(exit_code=code).inc(); _seen.add(code)

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path!='/metrics': self.send_response(404); self.end_headers(); return
        update(); body=generate_latest(reg)
        self.send_response(200)
        self.send_header('Content-Type','text/plain; version=0.0.4')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers(); self.wfile.write(body)

HTTPServer(('0.0.0.0',9113),H).serve_forever()
