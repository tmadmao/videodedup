# -*- coding: utf-8 -*-
"""
GUI 冒烟/回归测试（不进 mainloop，用 root.update() 驱动事件循环）

覆盖：扫描 → 分组 → 勾选 → 整组操作 → 视图切换 → 列排序 → 建议保留项一致性
      → 掩码开关 → CSV 导出 → 回收站/备份移动 → 边界情况

用法：
    python tools/make_testdata.py --out D:\\samples      :: 先生成样本
    python tools/gui_smoketest.py --dir D:\\samples
"""
from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tkinter as tk  # noqa: E402

import video_dedup as V  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {detail}" if detail else ""))
    return bool(cond)


def wait_scan(app, root, timeout=300):
    t0 = time.time()
    while app.scanning and time.time() - t0 < timeout:
        root.update()
        time.sleep(0.05)
    for _ in range(20):          # 让队列消息处理完
        root.update()
        time.sleep(0.02)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="要扫描的样本目录")
    args = ap.parse_args()

    root = tk.Tk()
    root.withdraw()
    app = V.VideoDedupApp(root)
    root.update()

    print("=" * 74)
    print("1) 扫描与分组")
    print("=" * 74)
    app.folder_var.set(args.dir)
    app.recursive_var.set(True)
    app.start_scan()
    wait_scan(app, root)
    # 注意：损坏/无法解码的文件本来就没有指纹（它们仍会完整列在清单里），
    # 所以只要求"解析成功的文件"有指纹
    parsed = [v for v in app.items.values() if not v.error]
    check("扫描完成且未在扫描中", not app.scanning)
    check("列表非空（即使没有重复也要完整列出）", len(app.items) > 0, f"{len(app.items)} 个文件")
    check("生成了重复分组", len(app.groups) >= 1, f"{len(app.groups)} 组")
    check("解析成功的文件都有 pHash 指纹",
          all(len(v.hashes) > 0 for v in parsed),
          f"{sum(1 for v in parsed if v.hashes)}/{len(parsed)}（另有 "
          f"{len(app.items) - len(parsed)} 个无法解码）")
    check("解析成功的文件都有灰度指纹（掩码通道用）",
          all(len(v.gray) > 0 for v in parsed),
          f"{sum(1 for v in parsed if v.gray)}/{len(parsed)}")

    print("=" * 74)
    print("2) 建议保留项：界面与命令行必须一致")
    print("=" * 74)
    for g in app.groups:
        if len(g) < 2:
            continue
        gg = [app.items[p] for p in g if p in app.items]
        keeper, why = V.pick_keeper(gg)
        shown = app.best_of.get(id(g))
        check(f"组内建议保留项一致（{keeper.name} / {why}）", shown == keeper.path,
              f"界面={os.path.basename(str(shown))} 计算={keeper.name}")
        break
    check("组内第一行就是建议保留项",
          all(app._order_group([p for p in g if p in app.items])[0] == app.best_of.get(id(g))
              for g in app.groups if len([p for p in g if p in app.items]) > 1))

    print("=" * 74)
    print("3) 勾选与整组操作")
    print("=" * 74)
    app.select_all()
    check("全选", len(app.checked) == len(app.items))
    app.select_none()
    check("全不选", len(app.checked) == 0)

    app.select_worse_in_groups()
    n_checked = len(app.checked)
    keepers = set(app.best_of.values())
    check("每组仅勾选低质量项", n_checked > 0)
    check("建议保留项没有被勾选", not (keepers & app.checked),
          f"误勾 {len(keepers & app.checked)} 个")

    app.select_invert()
    check("反选后保留项被勾上", keepers <= app.checked)
    app.select_none()

    print("=" * 74)
    print("4) 视图切换与列排序")
    print("=" * 74)
    for view in (V.VIEW_ALL, V.VIEW_DUP_ONLY, V.VIEW_TILE):
        app.filter_var.set(view)
        root.update()
        rows = len(app.tree.get_children())
        check(f"视图「{view}」能渲染", rows > 0, f"{rows} 个顶层行")
    app.filter_var.set(V.VIEW_ALL)
    root.update()
    for col in ("dur", "size", "res", "bitrate", "name"):
        app.sort_by(col)
        root.update()
        check(f"按「{col}」排序不报错", True)
    app.sort_by("dur")
    root.update()
    check("排序后建议保留项标记仍在", bool(app.best_of))

    print("=" * 74)
    print("5) 掩码开关与阈值")
    print("=" * 74)
    app.ignore_bw_var.set(False)
    app._on_gray_toggle()
    root.update()
    check("关闭掩码后界面仍正常", len(app.tree.get_children()) > 0,
          app.gray_hint.get()[:24])
    app.ignore_bw_var.set(True)
    app._on_gray_toggle()
    root.update()
    check("重新开启掩码", bool(app.ignore_bw_var.get()))
    opts = app._snapshot_options()
    check("选项快照含灰度通道参数",
          all(k in opts for k in ("gray_thr", "use_gray", "ignore_bw")), str(
              {k: opts[k] for k in ("gray_thr", "use_gray", "ignore_bw")}))

    print("=" * 74)
    print("6) CSV 导出")
    print("=" * 74)
    csv_path = os.path.join(tempfile.gettempdir(), "vd_gui_export.csv")
    # 与界面 export_csv_ui 相同的数据组织方式（那个方法带文件对话框，无法自动化）
    group_no = {}
    for gi, g in enumerate(app.groups):
        for pth in g:
            group_no[pth] = f"重复组{gi + 1}"
    rows_report = []
    for pth, v in app.items.items():
        rows_report.append([group_no.get(pth, "唯一"), v.name, v.path, v.size,
                            V.human_size(v.size), v.res_text, round(v.duration, 2),
                            V.fmt_duration(v.duration), v.bitrate, round(v.fps, 3),
                            v.codec, len(v.hashes), v.error])
    V.export_csv(rows_report, csv_path)
    ok_csv = os.path.exists(csv_path) and os.path.getsize(csv_path) > 0
    rows = 0
    if ok_csv:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            rows = sum(1 for _ in csv.reader(f))
    check("CSV 导出成功", ok_csv and rows == len(app.items) + 1,
          f"{rows} 行（含表头），文件数 {len(app.items)}")
    try:
        os.remove(csv_path)
    except OSError:
        pass

    print("=" * 74)
    print("7) 文件操作：备份移动 / 回收站兜底")
    print("=" * 74)
    src = tempfile.mkdtemp(prefix="vd_ops_")
    dst = tempfile.mkdtemp(prefix="vd_ops_bak_")
    p = os.path.join(src, "重复视频.mp4")
    with open(p, "wb") as f:
        f.write(os.urandom(4096))
    ok, fails = V.move_to_backup([p], dst, keep_structure=True)
    check("移动到备份文件夹", ok == 1 and not os.path.exists(p), f"失败={fails}")
    check("备份目录里能找到该文件",
          any(Path(dst).rglob("重复视频.mp4")))
    shutil.rmtree(src, ignore_errors=True)
    shutil.rmtree(dst, ignore_errors=True)

    print("=" * 74)
    print("8) 边界情况")
    print("=" * 74)
    empty = tempfile.mkdtemp(prefix="vd_empty_")
    check("空目录收集结果为空", V.collect_videos(empty, True, 0) == [])
    r = V.process_one(os.path.join(empty, "不存在.mp4"), 6, False)
    check("不存在的文件不抛异常，返回错误信息", bool(r.get("error")))
    check("掩码比对对空输入安全",
          V.masked_gray_distance([], []) == (1.0, 0))
    check("质量判据对空列表安全", V.pick_keeper([]) == (None, ""))
    check("质量判据对单元素安全", V.pick_keeper([V.VideoItem(path="x")])[0].path == "x")
    shutil.rmtree(empty, ignore_errors=True)

    root.destroy()

    print()
    print("=" * 74)
    print(f"GUI 回归：通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  失败：{f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
