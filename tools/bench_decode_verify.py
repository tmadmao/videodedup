# -*- coding: utf-8 -*-
"""
基准复核：耗时之外，必须校验「取到的帧对不对」
  1) 核对 ffmpeg 单进程输出到底是不是 12 帧（按字节数，而不是按返回值个数）
  2) 核对各方法的取帧位置准确性——请求的帧号 vs 实际拿到的帧号/时间戳

背景：初测时 PyAV 只取 seek 后的第一帧，显示快 9 倍，实际取到的是关键帧附近的帧，
      误差最大 8.7 秒。改成「解码推进到目标时刻」后变慢 4.75~9 倍。
      结论：性能对比必须同时校验输出正确性。

用法：set VD_BENCH_DIR=D:\\样本目录 & python tools/bench_decode_verify.py
"""
import os, time, subprocess, statistics, sys, hashlib
import cv2, av

B = os.environ.get("VD_BENCH_DIR", r"C:\Users\Administrator\AppData\Local\Temp\vd_bench")
FFMPEG = os.environ.get("FFMPEG", r"C:\ffmpeg\bin\ffmpeg.exe")

N = 12
CNW = 0x08000000

VIDEOS = ["v1080_60s.mp4", "v720_30s.mp4"]

print("=" * 96)
print("第 1 项：ffmpeg 单进程方案到底输出了几帧？（按字节数核对）")
print("=" * 96)
for vname in VIDEOS:
    path = os.path.join(B, vname)
    cap = cv2.VideoCapture(path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25)
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)); H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    dur = total / fps
    rate = N / dur
    print(f"\n{vname}  {W}x{H}  {total} 帧 / {dur:.0f} 秒")
    # 原生尺寸 bgr24
    out = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
                          "-i", path, "-vf", f"fps={rate:.8f}", "-frames:v", str(N),
                          "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
                         capture_output=True, timeout=300, creationflags=CNW).stdout
    per = W * H * 3
    print(f"  原生 bgr24 : {len(out):>11,} 字节 ÷ {per:,} = {len(out)/per:.2f} 帧  -> {'正确' if abs(len(out)/per - N) < 0.01 else '不对!'}")
    # 32x32 灰度
    out2 = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
                           "-i", path, "-vf", f"fps={rate:.8f},scale=32:32:flags=bicubic,format=gray",
                           "-frames:v", str(N), "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
                          capture_output=True, timeout=300, creationflags=CNW).stdout
    print(f"  32x32 灰度 : {len(out2):>11,} 字节 ÷ 1,024 = {len(out2)/1024:.2f} 帧  -> {'正确' if abs(len(out2)/1024 - N) < 0.01 else '不对!'}")

print()
print("=" * 96)
print("第 2 项：取帧位置准确性（请求帧号 vs 实际拿到）")
print("=" * 96)
for vname in VIDEOS:
    path = os.path.join(B, vname)
    cap = cv2.VideoCapture(path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25)
    cap.release()
    idxs = [max(0, min(total - 1, int((k + 0.5) * total / N))) for k in range(N)]

    # --- OpenCV: set(POS_FRAMES) 后实际落在哪一帧 ---
    cap = cv2.VideoCapture(path)
    cv_actual = []
    for i in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, fr = cap.read()
        # 读过之后 POS_FRAMES 指向"下一帧"，所以实际帧号 = 当前值 - 1
        cv_actual.append(int(cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1 if ok else -1)
    cap.release()

    # --- OpenCV: grab 顺序法（只记 index，天然精确）---
    cv_seq_err = 0   # 顺序法按帧号计数，误差恒为 0

    # --- PyAV: seek 后取第一帧（近似）---
    av_first = []
    with av.open(path) as c:
        st = c.streams.video[0]
        for i in idxs:
            t = i / fps
            c.seek(int(t * av.time_base), backward=True)
            got = None
            for fr in c.decode(st):
                got = fr.time
                break
            av_first.append(got)

    # --- PyAV: seek 后解码推进到目标时间（精确）---
    av_exact = []
    with av.open(path) as c:
        st = c.streams.video[0]
        for i in idxs:
            t = i / fps
            c.seek(int(t * av.time_base), backward=True)
            got = None
            for fr in c.decode(st):
                if fr.time is not None and fr.time >= t - 1e-9:
                    got = fr.time
                    break
            av_exact.append(got)

    print(f"\n{vname}  fps={fps:g}")
    print(f"  {'请求帧号':>8} {'请求时刻':>9} | {'OpenCV实际':>10} {'误差帧':>7} | {'PyAV首帧时刻':>12} {'误差帧':>7} | {'PyAV精确时刻':>12} {'误差帧':>7}")
    for k, i in enumerate(idxs):
        want_t = i / fps
        ca = cv_actual[k]
        af = av_first[k]
        ae = av_exact[k]
        af_err = (af * fps - i) if af is not None else float("nan")
        ae_err = (ae * fps - i) if ae is not None else float("nan")
        print(f"  {i:>8} {want_t:>9.3f} | {ca:>10} {ca - i:>7} | "
              f"{(af if af is not None else -1):>12.3f} {af_err:>7.1f} | "
              f"{(ae if ae is not None else -1):>12.3f} {ae_err:>7.1f}")

print()
print("=" * 96)
print("第 3 项：公平重测 —— PyAV '解码推进到目标时刻' vs OpenCV")
print("=" * 96)
def timeit(fn, reps=3):
    fn()
    ts = []
    for _ in range(reps):
        t0 = time.perf_counter(); fn(); ts.append((time.perf_counter() - t0) * 1000)
    return statistics.median(ts)

for vname in VIDEOS:
    path = os.path.join(B, vname)
    cap = cv2.VideoCapture(path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25)
    cap.release()
    idxs = [max(0, min(total - 1, int((k + 0.5) * total / N))) for k in range(N)]

    def cv_seek():
        c = cv2.VideoCapture(path); out = []
        for i in idxs:
            c.set(cv2.CAP_PROP_POS_FRAMES, i)
            ok, f = c.read()
            if ok: out.append(f)
        c.release(); return out

    def cv_grab():
        c = cv2.VideoCapture(path); want = set(idxs); out = []; i = 0
        while True:
            if not c.grab(): break
            if i in want:
                ok, f = c.retrieve()
                if ok: out.append(f)
            i += 1
        c.release(); return out

    def av_seek_exact():
        out = []
        with av.open(path) as c:
            st = c.streams.video[0]
            for i in idxs:
                t = i / fps
                c.seek(int(t * av.time_base), backward=True)
                for fr in c.decode(st):
                    if fr.time is not None and fr.time >= t - 1e-9:
                        out.append(fr.to_ndarray(format="bgr24")); break
        return out

    print(f"\n{vname}")
    r = {"① OpenCV seek+read": timeit(cv_seek),
         "② OpenCV grab+retrieve": timeit(cv_grab),
         "④ PyAV seek+解码到目标(精确)": timeit(av_seek_exact)}
    fast = min(r.values())
    for k, v in r.items():
        print(f"  {k:<30}{v:>9.1f} ms   {v/fast:>5.2f}×")
