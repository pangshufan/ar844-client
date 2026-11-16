# AR844 噪声监测

桌面客户端，面向 Windows x64、Ubuntu 24.04 x64 和 macOS（Apple Silicon / Intel）。数据保存在本机 SQLite，无需连接服务端。

## 开发与运行

```sh
source /opt/anaconda3/etc/profile.d/conda.sh
conda activate /Volumes/T9/test/envs/ar844
python -m pip install -e '.[dev]'
python -m ar844 --demo
```

直接运行 `python -m ar844` 连接真机。添加 `--demo` 会使用独立的模拟数据和设置。使用 `--database /path/to/noise.sqlite3` 可指定本次运行的数据库。在界面修改默认数据库位置后需要重启。

设备默认用旧驱动中的 VID `1234`、PID `5678`。如果真机报告不同的十六进制 ID，请在设置中修改。Windows 和 macOS 使用系统 HID 接口。macOS 如果弹出 USB 配件访问请求，请选择允许。

Ubuntu 需要 libusb 和对应权限。运行发布包时，目标系统也需要这些运行库。若设备 ID 不同，先修改规则中的两个 ID：

```sh
sudo apt install libusb-1.0-0 libegl1 libgl1 libxcb-cursor0 libxkbcommon-x11-0
sudo install -m 644 packaging/70-ar844.rules /etc/udev/rules.d/70-ar844.rules
sudo udevadm control --reload-rules
```

Ubuntu 发布包也带有 `70-ar844.rules`，解压后可以直接安装该文件。重新插拔仪器后，从登录的桌面会话正常启动客户端。运行时同时打开旧脚本可能导致 USB 接口被占用。

## 监测和统计

- 默认每秒采样，记录设备回报的实际设置。状态栏会显示未收到有效数据、设备重连和配置回读状态。只记录旧驱动接受的 30–130 dB 数值。
- `noise_data_raw` 保存每个有效读数。峰值表为 `noise_data_5min`、`noise_data_15min`、`noise_data_30min` 和 `noise_data_1h`。同一窗口内的计权、响应速度、设备模式分别汇总。窗口采用 UTC 固定边界（在 UTC+8 与本地整点对齐），区间包含开始、不包含结束。界面显示系统本地时间。
- 统计保留最大值、首次出现时间和有效样本数。空窗口无记录。历史统计搜索会返回与所选时间段重叠的完整窗口，窗口内的峰值可能落在所选时间段之外。当前窗口会随新样本更新。悬停在曲线上可查看时间、数值和模式。长范围曲线保留峰谷，分页表可读取所有查询结果。
- 点关闭后采集继续，曲线暂停刷新。托盘中可恢复或退出。无托盘的桌面会改为最小化，可从任务栏恢复。暂停采集或退出会关闭设备句柄。退出还会取消历史查询、等待采集线程提交并关闭数据库。睡眠、关机期间没有采样。
- 数据持续保存，数据库会随运行时间增长。备份或移动 SQLite 文件前请使用程序里的「退出」，让数据库正常关闭。

## 测试与打包

```sh
python -m pytest -q
python packaging/build.py
```

构建脚本使用 PyInstaller 在当前平台生成程序，运行打包后的模拟设备测试，然后将压缩包写入 `dist/artifacts`。GitHub Actions 在推送、拉取请求或手动触发时，分别在 Ubuntu 24.04、Windows、macOS Apple Silicon 和 macOS Intel 上测试、构建并上传该次运行的 Artifacts。下载后解压，Windows 运行 `AR844-Monitor.exe`，Ubuntu 运行 `AR844-Monitor`，macOS 打开 `AR844-Monitor.app`。macOS 构建未配置 Developer ID 签名与公证，供内部测试。

正式版本的四个平台压缩包可在 [GitHub Releases](https://github.com/pangshufan/ar844-client/releases) 的 Assets 中直接下载。发布前先等 `main` 上该提交的桌面 CI 全部通过，再推送与 `pyproject.toml` 版本一致的标签（如 `v0.1.0`）。发布流程会从该次 CI 取出已通过启动验证的压缩包并上传到 Release。

CI 用模拟设备验证采集、界面、统计查询和打包后的启动，并检查各平台 USB 后端能加载。接入真机后，还需分别验证连续采样、修改参数后的状态回读、反复拔插、睡眠唤醒及托盘恢复。
