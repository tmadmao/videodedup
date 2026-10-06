# -*- coding: utf-8 -*-
"""
抽帧性能基准：OpenCV vs ffmpeg CLI vs PyAV
统一任务：从视频里取 n=12 个均匀分布的采样帧（与 video_dedup.py 一致）
输出：每种方法的中位耗时（3 次取中位）、以及相对最快者的倍数

用法：
    set VD_BENCH_DIR=D:\\样本目录      & python tools/bench_decode.py
    set FFMPEG=C:\\ffmpeg\\bin\\ffmpeg.exe & python tools/bench_decode.py
目录里放任意 .mp4 即可，脚本会逐个跑。

注意（血泪教训）：本脚本只测耗时。**必须**再用 bench_decode_verify.py 校验
每种方法取到的帧位置是否正确——只比时间会得出完全相反的结论。
"""
import os, sys, time, subprocess, statistics, json

# 样本目录与 ffmpeg 路径都可用环境变量覆盖，默认值仅为本地调试方便
B = os.environ.get("VD_BENCH_DIR", r"C:\Users\Administrator\AppData\Local\Temp\vd_bench")
FFMPEG = os.environ.get("FFMPEG", r"C:\ffmpeg\bin\ffmpeg.exe")

N = 12
REPS = 3
CREATE_NO_WINDOW = 0x08000000

import cv2
import av

VIDEOS = [f for f in sorted(os.listdir(B)) if f.endswith(".mp4")]
if not VIDEOS:
    print("没有基准视频"); sys.exit(1)


def positions(total):
    return [max(0, min(total - 1, int((k + 0.5) * total / N))) for k in range(N)]


# ---------- 1. OpenCV：逐个位置 cap.set() 定位 ----------
def cv2_seek_read(path, total, idxs):
    cap = cv2.VideoCapture(str(path))
    frames = []
    for i in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, fr = cap.read()
        if ok:
            frames.append(fr)
    cap.release()
    return frames


# ---------- 2. OpenCV：顺序 grab() 跳过，只在目标处 retrieve() ----------
def cv2_grab_seq(path, total, idxs):
    cap = cv2.VideoCapture(str(path))
    want = set(idxs)
    frames = []
    i = 0
    while True:
        if not cap.grab():
            break
        if i in want:
            ok, fr = cap.retrieve()
            if ok:
                frames.append(fr)
        i += 1
    cap.release()
    return frames


# ---------- 3. OpenCV：顺序 read() 全解码 ----------
def cv2_read_seq(path, total, idxs):
    cap = cv2.VideoCapture(str(path))
    want = set(idxs)
    frames = []
    i = 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if i in want:
            frames.append(fr)
        i += 1
    cap.release()
    return frames


# ---------- 4. ffmpeg CLI：每个位置起一个进程，输出原生尺寸 bgr24 ----------
def ffmpeg_per_pos_native(path, total, idxs, fps):
    frames = []
    for i in idxs:
        t = i / fps
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-ss", f"{t:.6f}", "-i", str(path),
               "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
        out = subprocess.run(cmd, capture_output=True, timeout=120,
                             creationflags=CREATE_NO_WINDOW).stdout
        frames.append(out)
    return frames


# ---------- 5. ffmpeg CLI：每个位置一个进程，直接输出 32x32 灰度（VDF 做法）----------
def ffmpeg_per_pos_gray32(path, total, idxs, fps):
    frames = []
    for i in idxs:
        t = i / fps
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-ss", f"{t:.6f}", "-i", str(path),
               "-vf", "scale=32:32:flags=bicubic,format=gray",
               "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
        out = subprocess.run(cmd, capture_output=True, timeout=120,
                             creationflags=CREATE_NO_WINDOW).stdout
        frames.append(out)
    return frames


# ---------- 6. ffmpeg CLI：单进程 + fps 滤镜一次产出 12 帧（原生尺寸）----------
def ffmpeg_oneshot_native(path, total, idxs, fps, duration):
    rate = N / duration
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
           "-i", str(path), "-vf", f"fps={rate:.8f}",
           "-frames:v", str(N), "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    return [subprocess.run(cmd, capture_output=True, timeout=300,
                           creationflags=CREATE_NO_WINDOW).stdout]


# ---------- 7. ffmpeg CLI：单进程 + fps + 缩放成 32x32 灰度 ----------
def ffmpeg_oneshot_gray32(path, total, idxs, fps, duration):
    rate = N / duration
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
           "-i", str(path), "-vf", f"fps={rate:.8f},scale=32:32:flags=bicubic,format=gray",
           "-frames:v", str(N), "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    return [subprocess.run(cmd, capture_output=True, timeout=300,
                           creationflags=CREATE_NO_WINDOW).stdout]


# ---------- 8. PyAV：单次打开，逐位置 seek ----------
def pyav_seek(path, total, idxs, fps):
    frames = []
    with av.open(str(path)) as c:
        st = c.streams.video[0]
        for i in idxs:
            t = i / fps
            c.seek(int(t * av.time_base), backward=True)
            for fr in c.decode(st):
                frames.append(fr)
                break
    return frames


# ---------- 9. PyAV：单次打开，顺序解码 ----------
def pyav_seq(path, total, idxs):
    want = set(idxs)
    frames = []
    with av.open(str(path)) as c:
        st = c.streams.video[0]
        i = 0
        for fr in c.decode(st):
            if i in want:
                frames.append(fr)
                if len(frames) == len(want):
                    break
            i += 1
    return frames


import cv2 as _cv2  # resize 用

def cv2_resize32(frames):
    """把已拿到的帧缩成 32x32 灰度（附加成本）"""
    out = []
    for fr in frames:
        g = _cv2.cvtColor(fr, _cv2.COLOR_BGR2GRAY)
        g = _cv2.resize(g, (32, 32), interpolation=_cv2.INTER_CUBIC)
        out.append(g)
    return out


def run(name, fn, *a, **kw):
    try:
        fn(*a, **kw)                     # 预热
    except Exception as e:
        return {"method": name, "error": f"{type(e).__name__}: {e}"[:60]}
    times = []
    ok_n = 0
    for _ in range(REPS):
        t0 = time.perf_counter()
        res = fn(*a, **kw)
        times.append((time.perf_counter() - t0) * 1000)
        ok_n = len(res)
    return {"method": name, "ms": round(statistics.median(times), 1),
            "frames": ok_n, "runs": [round(x, 1) for x in times]}


all_rows = {}
for vname in VIDEOS:
    path = os.path.join(B, vname)
    cap = cv2.VideoCapture(path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25) or 25
    cap.release()
    duration = total / fps
    idxs = positions(total)
    print("=" * 92)
    print(f"{vname}   共 {total} 帧 / {fps:.2f} fps / {duration:.1f} 秒   采样位置 {idxs[:4]}…")
    print("=" * 92)
    rows = []
    rows.append(run("① OpenCV seek+read（我们现在的做法）", cv2_seek_read, path, total, idxs))
    rows.append(run("② OpenCV 顺序 grab+retrieve", cv2_grab_seq, path, total, idxs))
    rows.append(run("③ OpenCV 顺序 read 全解码", cv2_read_seq, path, total, idxs))
    rows.append(run("④ ffmpeg 12 个进程·原生尺寸", ffmpeg_per_pos_native, path, total, idxs, fps))
    rows.append(run("⑤ ffmpeg 12 个进程·32×32 灰度", ffmpeg_per_pos_gray32, path, total, idxs, fps))
    rows.append(run("⑥ ffmpeg 单进程·原生尺寸", ffmpeg_oneshot_native, path, total, idxs, fps, duration))
    rows.append(run("⑦ ffmpeg 单进程·32×32 灰度", ffmpeg_oneshot_gray32, path, total, idxs, fps, duration))
    rows.append(run("⑧ PyAV 单次打开·逐位置 seek", pyav_seek, path, total, idxs, fps))
    rows.append(run("⑨ PyAV 单次打开·顺序解码", pyav_seq, path, total, idxs))
    rows.append(run("⑩ OpenCV seek 后再缩 32×32", lambda p, t, i: cv2_resize32(cv2_seek_read(p, t, i)), path, total, idxs))
    valid = [r for r in rows if "ms" in r]
    fastest = min(r["ms"] for r in valid) if valid else 1
    for r in rows:
        if "ms" not in r:
            print(f"  {r['method']:<38} 失败: {r['error']}")
            continue
        bar = "#" * max(1, int(r["ms"] / fastest * 2)) if fastest else ""
        flag = "" if r["frames"] == N else f"  ⚠只拿到{r['frames']}帧"
        print(f"  {r['method']:<38}{r['ms']:>9.1f} ms   {r['ms'] / fastest:>5.2f}×  {bar}{flag}")
    all_rows[vname] = {"total": total, "fps": fps, "duration": duration, "rows": rows}
    print()

json.dump(all_rows, open(os.path.join(B, "_bench.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print("结果已存 _bench.json")
