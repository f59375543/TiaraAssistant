# 蒂亚拉小助手 项目地图与故障排查

本目录是正在使用的 Windows x64 便携版。以下 PowerShell 命令以本文件所在目录为工作目录。

**启动链**：`蒂亚拉小助手.exe` 创建 WebView2 窗口，调用随包 Python 运行 `start_hka.pyw --desktop-service`。本地服务 `hka/web.py` 提供 `hka/ui/index.html` 和 API；点击运行后，`hka/runner.py` 根据任务配置调用游戏流程与 ADB。

`hka/ui/` 下的 `index.html` 定义页面结构，`styles.css` 定义样式，`app.js` 处理按钮、截图和 API 请求。`hka/assets/` 保存程序内置的识别图片，用户在界面裁剪的模板由设置中的 `template_dir` 指定。

## 按现象定位

| 看到的现象 | 先看什么 | 源码或测试入口 |
| --- | --- | --- |
| 窗口打不开、白屏、关窗后进程仍在 | 窗口报错、`logs/assistant-service.log` 末尾。服务成功监听后才会生成当前的 `logs/assistant-url.txt`，正常退出会清除它 | `hka/desktop/Assistant.cs`、`start_hka.pyw`、`hka/web.py`。修改 C# 后，在助手退出时执行 `.\hka\desktop\build.ps1` 重建 EXE |
| 设置保存后丢失、任务删了又回来 | 比较界面提交值、`config.json` 里的对应字段和重开后的值 | `hka/ui/app.js` → `hka/web.py` → `hka/config.py`。预设在 `hka/defaults.py`，回归见 `tests/test_task_presets.py` |
| 任务被跳过、同日重复执行 | 运行报告中的 `not_run`、任务启用状态、频率、日期条件及任务日刷新时间 | `hka/runner.py` 的 `preview`；`state.json` 记录完成项，`.daily-attempts/` 记录尝试。回归见 `tests/test_task_frequency.py` |
| BONUS 进错活动、用券或结算计数异常 | 找到对应任务 ID 和时间的截图，核对难度标签、剩余时间、普通体力、详情页消耗或结算按钮 | `hka/game_flows.py` 的 `_activity`、`_prepare_bonus`、`_combat`；识别在 `hka/ocr.py`、`hka/vision.py`，测试见 `tests/test_activity_priority.py`、`tests/test_bonus.py` |
| 模拟器连接失败、截图中断或关闭了错误实例 | 先核对 `config.json` 中的 ADB 路径、设备序列号和游戏包名。设备已启动时可运行 `.\runtime\python.exe -B -m hka doctor` 检查连接 | `hka/adb.py`、`tests/test_emulator_exit.py`。本机曾验证的设备为雷电 `emulator-5554`，画面 1920×1080；游戏流程坐标统一到 1600×900 |

## 从一次故障开始

1. 根据用户描述的发生时间选报告。用户说“刚才”或未指定轮次时，先取最新一份。
2. 先看整轮 `status` 和 `setup_error`，再看出错任务的 `id`、`message`、`details`。没有执行的任务看 `not_run`。通知失败单独记录在 `notification_errors`。
3. 用同一时段、同一任务的截图或模拟输入复现。旧报告和截图可能来自旧版本；修复前后用相同输入比较。任务无待办时可能没有新的 `run_*.json`，可用下方的 `plan` 检查。
4. 修改后先跑对应专项测试，再跑全量测试。汇报触发条件、修改的文件和实际验证结果，注明使用的是历史截图、模拟设备还是真实雷电。

```powershell
# 读取最近一轮的结果；有用户指定时间时，改选对应报告
$latest = Get-ChildItem .\logs -File -Filter 'run_*.json' |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($latest) {
    $report = Get-Content -Raw -Encoding utf8 -LiteralPath $latest.FullName | ConvertFrom-Json
    $report | Select-Object started_at, finished_at, status, setup_error
    $report.tasks | Select-Object id, status, message
    $report.not_run | Select-Object id, reason
}
```

```powershell
# 当前任务计划、活动选择专项测试、全部离线回归
.\runtime\python.exe -B -X utf8 -m hka plan
.\runtime\python.exe -B -X utf8 -m unittest discover -s tests -p 'test_activity_priority.py' -v
.\runtime\python.exe -B -X utf8 -m unittest discover -s tests -v
```

## 排查时保留现场

- `config.json` 是用户当前设置，`config.example.json` 用于首次创建配置。复现配置保存、清空列表或重置当天记录时，使用临时副本。
- `assistant.lock` 和 `.task-run.lock` 是锁文件。文件可以在程序退出后继续存在；判断是否占用，应检查进程或操作系统文件锁。
- `run` 命令会执行真实任务。排查体力扣费、领取消耗或重复出击时，先用截图和离线测试；实机验证按用户当次授权的任务和场次执行。
