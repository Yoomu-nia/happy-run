# happy_run 使用说明

模拟 MuMu 模拟器里的虚拟定位，让乐跑按预设路线自动跑。

代码修改自(https://github.com/Wzh10032/happy-run?tab=readme-ov-file)

由于 MuMu 新版虚拟定位的输入框已经没法使用经纬度，不能用输入坐标的脚本了

---

## 风险提示

本工具用于模拟 GPS 轨迹。**被平台检测到，可能影响成绩甚至被处分，后果自负。**

**使用本代码造成的后果与本人无关**

**使用本代码造成的后果与本人无关**

**使用本代码造成的后果与本人无关**

---

## 一、需要

1. **MuMu 模拟器 12**（官网下载），里面装好乐跑并登录；
2. **Python 3.8 或更高版本**（安装时勾选 "Add Python to PATH"）；
   - 判断方法：命令行输入 `python --version` 或 `py --version`，能显示版本号就行；

---

## 二、实现原理

MuMu 模拟器自带一个命令行工具 `MuMuManager.exe`，里面有直接修改虚拟定位的接口：

```
MuMuManager.exe control -v 0 tool location -lon 116.408581 -lat 39.928437
```

脚本做的就是反复调用它：改一次坐标，模拟器的位置就动一下


---

## 三、使用方法

用记事本或 VS Code 打开 `happy_run_mumu.py`，找到开头这几行：

**1. 改成你自己的 MuMu 路径**（第 34 行附近）

```python
MUMU_MANAGER = r"D:\Program Files\Netease\MuMuPlayer-12.0\nx_main\MuMuManager.exe"
```


**2. 改成你自己的路线点**

```python
LOCATIONS = [
    "116.408581,39.928437",
    ...
]
```

- 格式是 `"经度,纬度"`，**必须用百度地图拾取**（https://api.map.baidu.com/lbsapi/getpoint/index.html）；
- 用高德/腾讯的点会偏大约 1 公里；
- 按跑步顺序排列，最后一个点会自动连回第一个点，无需自行连接；


**3. 打开 MuMu，进入乐跑；**

**4. 在命令行里执行：**

```powershell
cd 脚本所在目录
py happy_run_mumu.py
```

**或者直接打开**  `happy_run_mumu.py`

**5. 脚本会倒数 3 秒后开始，沿路线逐点移动；**

**6. 跑完一圈自动结束**，把位置设回起点；中途想停按 `Ctrl+C`。


---

## 四、各参数位置

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `SPEED_MPS` | 3.0 | 速度，米/秒 |
| `TICK_SECONDS` | 1.0 | 每秒更新一次位置 |
| `WOBBLE_M` | 0.6 | 左右摆动幅度（米） |
| `LAP_LIMIT` | 1 | 跑几圈后停 |
| `MOTION_MODE` | smooth | 平滑样条；改成 `linear` 是折线 |
