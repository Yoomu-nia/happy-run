# -*- coding: utf-8 -*-
"""
happy-run 命令行版（v2）：直接调用 MuMu 自带虚拟定位接口，支持平滑轨迹。

原理：MuMuManager.exe control -v <索引> tool location -lon <经度> -lat <纬度>
      直接修改模拟器虚拟定位，不碰搜索框 / 鼠标 / 键盘 / 剪贴板。

两种运动模式：
  smooth（默认）：Catmull-Rom 样条穿过航点 + 按弧长重采样 + 恒定速度 + 正弦微摆
  linear        ：航点之间直线插值（拐角是硬折线），速度同样恒定

用法：
  py happy_run_mumu.py --dry-run --ticks 10   # 只打印坐标，先看间距对不对
  py happy_run_mumu.py                        # 正式跑，跑完一圈自动结束
  py happy_run_mumu.py --laps 2               # 跑两圈后停
  py happy_run_mumu.py --loop                 # 一直循环，不自动停
  py happy_run_mumu.py --mode linear          # 退回直线模式
  py happy_run_mumu.py --speed 2.5            # 换速度（米/秒）
  py happy_run_mumu.py --check                # 看模拟器状态

按 Ctrl+C 结束。
"""
import argparse
import bisect
import json
import math
import subprocess
import sys
import time

# ======================== 需要你确认的配置 ========================

# MuMuManager.exe 的完整路径（MuMu 12 默认在 nx_main 下）
MUMU_MANAGER = r"D:\Program Files\Netease\MuMuPlayer-12.0\nx_main\MuMuManager.exe"

# 模拟器索引，多开时用 info 命令查（--check 会打印出来）
VM_INDEX = "0"

# 依次经过的点，格式 "经度,纬度"。原来的值来自百度地图拾取。
LOCATIONS = [
    "117.188708,31.771860",
    "117.186965,31.772447",
    "117.188820,31.772946",
    "117.192112,31.775816",
    "117.192997,31.778993",
    "117.192692,31.768817",
    "117.192629,31.771810",
]

# 坐标系：坐标是怎么来的。MuMu 接口内部会把收到的点按「百度 BD-09」处理，
# 所以脚本最终统一换算成 BD-09 再发过去。
#   "bd09"  -> 百度地图拾取（你现在这组坐标，默认，原样发送）
#   "gcj02" -> 高德 / 腾讯地图拾取（会先转成 BD-09）
#   "wgs84" -> GPS 原始坐标（会先转成 BD-09）
COORD_SOURCE = "bd09"

# ---------------- 运动参数 ----------------

# 模式：smooth = 样条平滑（推荐），linear = 航点直线
MOTION_MODE = "smooth"

# 速度，米/秒。2.5 约 9 km/h，3.0 约 10.8 km/h，3.5 约 12.6 km/h
SPEED_MPS = 3.0

# 每次更新的时间间隔（秒），也就是每个坐标点间隔多少秒
TICK_SECONDS = 1.0

# 左右摆动幅度（米）。0 表示完全不摆动，0.5~0.8 比较自然
WOBBLE_M = 0.6

# 速度轻微波动幅度，0.06 表示 ±6%
SPEED_WAVE = 0.06

# 样条每段采样多少点，越大曲线越精细（60 约等于每 0.3 米一个采样点）
SPLINE_SAMPLES = 60

# 样条参数：0.5 是向心 Catmull-Rom，点距不均匀时也不会甩出去（推荐）
# 0 是均匀 Catmull-Rom，0.5 向心，1 是弦长参数化
SPLINE_ALPHA = 0.5

# 跑几圈后自动停。1 = 跑完一遍路径点就结束（默认），0 = 一直循环跑
LAP_LIMIT = 1

# =================================================================

A = 6378245.0
EE = 0.00669342162296594323


def _out_of_china(lon, lat):
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x, y):
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x, y):
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lon, lat):
    """WGS-84 -> GCJ-02（国测局坐标）"""
    if _out_of_china(lon, lat):
        return lon, lat
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = 1 - EE * math.sin(radlat) ** 2
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((A * (1 - EE)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (A / sqrtmagic * math.cos(radlat) * math.pi)
    return lon + dlon, lat + dlat


def gcj02_to_bd09(lon, lat):
    z = math.sqrt(lon * lon + lat * lat) + 0.00002 * math.sin(lat * math.pi * 3000.0 / 180.0)
    theta = math.atan2(lat, lon) + 0.000003 * math.cos(lon * math.pi * 3000.0 / 180.0)
    return z * math.cos(theta) + 0.0065, z * math.sin(theta) + 0.006


def to_bd09(lon, lat):
    """把配置里的坐标统一换算成 BD-09（MuMu 接口按 BD-09 处理）"""
    src = COORD_SOURCE.lower()
    if src == "bd09":
        return lon, lat
    if src == "gcj02":
        return gcj02_to_bd09(lon, lat)
    if src == "wgs84":
        return gcj02_to_bd09(*wgs84_to_gcj02(lon, lat))
    raise ValueError(f"未知的 COORD_SOURCE: {COORD_SOURCE}")


def set_location(lon, lat, dry_run=False):
    """调用 MuMuManager 修改模拟器虚拟定位"""
    cmd = [
        MUMU_MANAGER, "control", "-v", str(VM_INDEX), "tool", "location",
        "-lon", f"{lon:.6f}", "-lat", f"{lat:.6f}",
    ]
    if dry_run:
        return
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"MuMuManager 返回码 {proc.returncode}: {out or proc.stderr.strip()}")
    try:
        errcode = json.loads(out).get("errcode", 0)
    except (ValueError, AttributeError):
        errcode = 0  # 输出不是 JSON 时以返回码为准
    if errcode != 0:
        raise RuntimeError(f"设置定位失败: {out}")


# ==================== 轨迹生成（核心） ====================
#
# 思路：经纬度先换算到「以第一个点为原点的米制平面」，在平面上做平滑和
#       等距离取点，最后再换算回经纬度。直接在经纬度上插值会让轨迹变形。

def _plane(points):
    """经纬度 -> 局部米制平面（等距圆柱投影，小范围误差可忽略）"""
    lon0, lat0 = points[0]
    kx = 111320.0 * math.cos(math.radians(lat0))
    ky = 110540.0
    xy = [((lon - lon0) * kx, (lat - lat0) * ky) for lon, lat in points]
    return xy, (lon0, lat0, kx, ky)


def _unplane(x, y, frame):
    """局部米制平面 -> 经纬度"""
    lon0, lat0, kx, ky = frame
    return lon0 + x / kx, lat0 + y / ky


def _lerp_point(a, b, ta, tb, t):
    """在参数 t 处对 a、b 两点做线性插值（t 位于 ta~tb 之间）"""
    if tb == ta:
        return a
    w = (t - ta) / (tb - ta)
    return (a[0] + (b[0] - a[0]) * w, a[1] + (b[1] - a[1]) * w)


def _catmull_rom_closed(xy, samples, alpha=0.5):
    """
    闭合 Catmull-Rom 样条（Barry-Goldman 形式）。

    alpha=0   -> 均匀参数化，点距相差大时容易甩出尖角
    alpha=0.5 -> 向心参数化（推荐），点距不均匀也不会自交或外甩
    alpha=1   -> 弦长参数化
    """
    n = len(xy)
    out = []
    for i in range(n):
        p0 = xy[(i - 1) % n]
        p1 = xy[i]
        p2 = xy[(i + 1) % n]
        p3 = xy[(i + 2) % n]
        d01 = max(math.hypot(p1[0] - p0[0], p1[1] - p0[1]), 1e-6) ** alpha
        d12 = max(math.hypot(p2[0] - p1[0], p2[1] - p1[1]), 1e-6) ** alpha
        d23 = max(math.hypot(p3[0] - p2[0], p3[1] - p2[1]), 1e-6) ** alpha
        t0 = 0.0
        t1 = t0 + d01
        t2 = t1 + d12
        t3 = t2 + d23
        for j in range(samples):
            t = t1 + (t2 - t1) * j / samples
            a1 = _lerp_point(p0, p1, t0, t1, t)
            a2 = _lerp_point(p1, p2, t1, t2, t)
            a3 = _lerp_point(p2, p3, t2, t3, t)
            b1 = _lerp_point(a1, a2, t0, t2, t)
            b2 = _lerp_point(a2, a3, t1, t3, t)
            out.append(_lerp_point(b1, b2, t1, t2, t))
    out.append(out[0])  # 闭合
    return out


def _straight_closed(xy):
    """直线模式：直接拿航点当折线"""
    return list(xy) + [xy[0]]


def _cumulative(dense):
    """累积弧长，用于『走了多少米』的换算"""
    cum = [0.0]
    for i in range(1, len(dense)):
        dx = dense[i][0] - dense[i - 1][0]
        dy = dense[i][1] - dense[i - 1][1]
        cum.append(cum[-1] + math.hypot(dx, dy))
    return cum


def _point_at(dense, cum, s):
    """取曲线上距起点 s 米处的坐标（超过一圈自动绕回）"""
    total = cum[-1]
    s = s % total
    i = bisect.bisect_right(cum, s) - 1
    if i < 0:
        i = 0
    if i >= len(dense) - 1:
        i = len(dense) - 2
    seg = cum[i + 1] - cum[i]
    t = 0.0 if seg <= 0 else (s - cum[i]) / seg
    x = dense[i][0] + (dense[i + 1][0] - dense[i][0]) * t
    y = dense[i][1] + (dense[i + 1][1] - dense[i][1]) * t
    return x, y


def build_path(locations, mode, alpha=SPLINE_ALPHA):
    """把航点变成『可按米取点』的闭合路径"""
    bd_points = [to_bd09(lon, lat) for lon, lat in locations]
    xy, frame = _plane(bd_points)
    if mode == "smooth" and len(xy) >= 3:
        dense = _catmull_rom_closed(xy, SPLINE_SAMPLES, alpha)
    else:
        dense = _straight_closed(xy)
    return dense, _cumulative(dense), frame


def run(args, locations):
    dense, cum, frame = build_path(locations, args.mode, args.alpha)
    total = cum[-1]
    wave = max(25.0, total / 10.0)  # 摆动的波长，一圈大约摆 10 次

    # 跑几圈：--loop 不设上限，--laps N 指定圈数，否则用配置里的 LAP_LIMIT
    if args.loop:
        limit = 0
    elif args.laps:
        limit = args.laps
    else:
        limit = LAP_LIMIT
    plan = f"跑完 {limit} 圈自动结束" if limit else "一直跑，按 Ctrl+C 结束"

    print(f"路线全长 {total:.1f} 米，一圈约 {total / args.speed:.0f} 秒"
          f"（{total / args.speed / 60:.1f} 分钟）")
    print(f"模式 {args.mode}，速度 {args.speed} m/s，摆动 {args.wobble} 米，{plan}。")
    time.sleep(3)
    print("开始。")

    traveled = 0.0
    lap = 0
    ticks = 0
    finished = False
    start_time = time.time()
    next_time = start_time
    try:
        while True:
            x, y = _point_at(dense, cum, traveled)
            x2, y2 = _point_at(dense, cum, traveled + 0.5)
            dx, dy = x2 - x, y2 - y
            norm = math.hypot(dx, dy) or 1.0
            nx, ny = -dy / norm, dx / norm            # 法线方向
            off = args.wobble * math.sin(2 * math.pi * traveled / wave)
            lon, lat = _unplane(x + nx * off, y + ny * off, frame)
            print(f"{lon:.6f},{lat:.6f}")
            set_location(lon, lat, args.dry_run)

            ticks += 1
            step = args.speed * (1 + SPEED_WAVE * math.sin(traveled / 40.0)) * TICK_SECONDS
            traveled += step

            new_lap = int(traveled // total)
            if new_lap > lap:
                lap = new_lap
                print(f"--- 完成第 {lap} 圈 ---")
                if limit and lap >= limit:
                    finished = True
                    break

            if args.ticks and ticks >= args.ticks:
                break

            next_time += TICK_SECONDS
            delay = next_time - time.time()
            if delay > 0:
                time.sleep(delay)
    except KeyboardInterrupt:
        print("\n已停止。")
        return

    if finished:
        # 收尾：把位置精确设回起点，避免停在起点前几米
        x0, y0 = _point_at(dense, cum, 0.0)
        lon0, lat0 = _unplane(x0, y0, frame)
        print(f"回到起点 {lon0:.6f},{lat0:.6f}")
        set_location(lon0, lat0, args.dry_run)
        print(f"结束：共 {ticks} 个点，用时 {time.time() - start_time:.0f} 秒，"
              f"里程约 {traveled:.0f} 米。")


def print_route_info(locations, speed):
    """检查路线：每段长度、总长、潜在问题"""
    bd_points = [to_bd09(lon, lat) for lon, lat in locations]
    xy, _ = _plane(bd_points)
    n = len(xy)

    legs = []
    for i in range(n):
        a, b = xy[i], xy[(i + 1) % n]
        legs.append(math.hypot(b[0] - a[0], b[1] - a[1]))

    dense, cum, _ = build_path(locations, "smooth")
    total = cum[-1]

    print(f"航点数：{n}")
    for i, d in enumerate(legs):
        print(f"  第 {i + 1} → {(i + 1) % n + 1} 段：{d:8.1f} 米")
    print(f"折线全长：{sum(legs):.1f} 米")
    print(f"平滑后全长：{total:.1f} 米，按 {speed} m/s 一圈约 "
          f"{total / speed:.0f} 秒（{total / speed / 60:.1f} 分钟）")

    warnings = []
    if n < 3:
        warnings.append("航点少于 3 个，smooth 模式会退化成直线")
    for i, d in enumerate(legs):
        if d < 5:
            warnings.append(f"第 {i + 1} 段只有 {d:.1f} 米，两个点几乎重合，建议删掉一个")
        elif d > 500:
            warnings.append(f"第 {i + 1} 段有 {d:.0f} 米，偏长，拐弯处可能需要补点")

    if warnings:
        print("提示：")
        for w in warnings:
            print("  - " + w)
    else:
        print("检查通过：点距和数量都正常。")


def check_player():
    """打印模拟器状态，帮助排查『定位没生效』"""
    if not MUMU_MANAGER:
        return
    proc = subprocess.run(
        [MUMU_MANAGER, "info", "-v", str(VM_INDEX)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    print("模拟器状态:")
    print((proc.stdout or proc.stderr).strip())


def load_locations():
    pts = []
    for p in LOCATIONS:
        parts = p.replace("，", ",").split(",")
        if len(parts) != 2:
            sys.exit(f"坐标格式错误：{p}（应为『经度,纬度』）")
        pts.append((float(parts[0]), float(parts[1])))
    if not pts:
        sys.exit("LOCATIONS 是空的，请先填点。")
    return pts


def main():
    parser = argparse.ArgumentParser(description="MuMu 模拟器虚拟定位跑步脚本")
    parser.add_argument("--mode", choices=["smooth", "linear"], default=MOTION_MODE,
                        help="smooth=样条平滑（默认），linear=航点直线")
    parser.add_argument("--speed", type=float, default=SPEED_MPS, help="速度，米/秒")
    parser.add_argument("--wobble", type=float, default=WOBBLE_M, help="左右摆动幅度，米")
    parser.add_argument("--alpha", type=float, default=SPLINE_ALPHA,
                        help="样条参数：0.5=向心（默认），0=均匀")
    parser.add_argument("--info", action="store_true", help="只检查路线（每段长度、总长），不跑")
    parser.add_argument("--laps", type=int, default=0, help="跑几圈后停，0=用配置里的 LAP_LIMIT（默认 1）")
    parser.add_argument("--loop", action="store_true", help="一直循环跑，不自动结束")
    parser.add_argument("--ticks", type=int, default=0, help="只跑多少个点后停（测试用）")
    parser.add_argument("--dry-run", action="store_true", help="只打印坐标，不修改定位")
    parser.add_argument("--once", action="store_true", help="只设置第一个点后退出（用于校准坐标）")
    parser.add_argument("--check", action="store_true", help="打印模拟器状态后退出")
    args = parser.parse_args()

    if args.check:
        check_player()
        return

    locations = load_locations()

    if args.info:
        print_route_info(locations, args.speed)
        return

    if args.dry_run:
        print("dry-run 模式：只打印坐标，不会真的改定位。\n")

    if args.once:
        lon, lat = to_bd09(*locations[0])
        print(f"设置第一个点: {lon:.6f},{lat:.6f}")
        set_location(lon, lat, args.dry_run)
        print("完成。")
        return

    if args.speed <= 0:
        sys.exit("--speed 必须大于 0。")

    try:
        run(args, locations)
    except RuntimeError as exc:
        sys.exit(f"\n出错了：{exc}\n请确认 MuMu 模拟器已经启动、索引 {VM_INDEX} 正确（可用 --check 查看）。")


if __name__ == "__main__":
    main()
