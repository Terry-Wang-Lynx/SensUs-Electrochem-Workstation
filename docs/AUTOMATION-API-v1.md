# 高通量自动化实验 / Agent API v1

**2026-10-24 路径扩展**：普通模板新增 `guided-1.1` 与 `execution:{mode,count,route}`，支持一次性、有限重复、自定义列顺序 / 回访；不是无限模式。GET `state` 新增来自实际运行快照的 `progress`，样本和 manifest 保存轮次 / 访问身份。旧动作及 `guided-1.0` 的无扩展计划哈希不变。字段、示例和限制见 [GUIDED-ROUTES.md](GUIDED-ROUTES.md)。

**最新普通入口为键盘标定**：POST`guided/calibration/begin`用`{control_mode:"keyboard",motion_confirmed:true}`，保持锁轴、不松轴；新增GET`guided/calibration/jog`和POST其`start/heartbeat/stop/config`。350ms租约、有限FD段、速度硬限与旧请求撤销由后端实施，四点不变。读取/完成/取消优先停止点动，自动实验不接受键盘。详情[键盘接口](KEYBOARD-ALIGNMENT.md)，下文release_confirmed/z_supported为旧手拧兼容接口。

**2026-10-23最新：一次上电四点**。生产普通模板以物理1列X、12列X、working Z、safe Z作为唯一实验标定，不再强制下文独立机械四极限。`guided/calibration/begin`必须含布尔`release_confirmed:true,z_supported:true`；capture新增`working`，finish先核目标再允许使能；current连接/profile/参考不匹配就拒绝实物运行。旧三点可查看/模拟，原始动作入口不能冒充已标定实物运动；GET`devices`含缓存calibration.ready/display_valid，GET`twin`自动派生四点显示，不另登记。详情[同次上电四点](POWERED-FOUR-POINT-CALIBRATION.md)，下方同日限位/端点阶段仅保留历史。

2026-10-19 开始，2026-10-20 验证，0.5.0 本地开发增量。入口为工作站导航「高通量自动化实验」。这不是已发布版本，也不是双板、双轴实物验收报告。验证边界见 [验证记录](VALIDATION-AUTOMATION-v1.md)。

**2026-10-22普通页已简化**：默认使用独立`guided-1.0`模板，共用采样参数、自由列名/时长/窗口和固定1/12标定模型。服务端编译为动作schema1.1，支持`concentration:null`、带符号列距、通道窗口与按板续采；原schema1.0/下文原始动作接口仍保留。普通页不再要求浓度数值/手工JSON；新入口及限制见[模板与标定说明](GUIDED-AUTOMATION.md)。取动作1.1 schema用 `/api/automation/v1/schema?version=1.1`，缺省仍为1.0。

**2026-10-23限位门禁**：工作站真实运动强制要求当前连接上有效的独立四边界记录，不能从动作/模板JSON注入或扩大。新增GET`limits`和POST`limits/begin|capture|save|cancel`；`capture`只接收边界名称，`save`要求布尔`clearance_confirmed:true`。GET`capabilities`声明策略。详情见[绝对运行限位](ABSOLUTE-MOTION-LIMITS.md)。无轴运动的旧板采集与模拟不借此声明机械安全验收。

**2026-10-23反馈孪生**：明确连接后后台每0.5秒只读已有运动总线；新增独立设备缓存与基于可信原始计数的CAD映射，不使用模拟位置或目标位置假装实物反馈。快速显示基准只需当前可信连接的左/上或右/下两点，明确采用名义CAD角点且不启用安全限位；高级精确对应保留完整四边界及CAD坐标。断线/过期/身份变化时pose为空；详见[反馈孪生](LIVE-FEEDBACK-TWIN.md)。

## 1. 页面与运行模型

中央为鼠标拖动旋转、滚轮缩放的覆膜装置；点击孔板列只选择编辑对象，不移动实物。整机采用保存的 Fusion 装配及后续 CAD 修订网格，X 移动孔板托盘、后方 Z 背架固定；详细来源与坐标见 [模型来源](AUTOMATION-MODEL-SOURCES.md)。采购滑台/孔板仍是原设计的参考外形，**不是 3DGS 扫描重建，也不承担碰撞检查**。Three.js 0.160.1 随应用本地提供，不依赖 CDN。

- 每列设置溶液、浓度及单位、驻留、稳定等待、上下速度、横移速度和坐标覆盖。
- 默认值优先级：动作显式参数 > 当前列覆盖 > 实验默认参数。
- 动作积木追加声明式 JSON；可直接在编辑窗口编程，也可完全通过 API 驱动。
- 修改任何参数后旧计划失效，必须重新预览；未应用的表单/JSON 草稿不能直接运行。
- A–D = 板 A 的 CH1–4；E–H = 板 B 的 CH1–4。每板共用电位、量程与周期，不是八路独立电位设置。
- 默认 `simulate`。模拟数据、运行目录和日志均标记 `synthetic`；页面模拟约 50 倍加速，不模拟真实采样率、噪声或时序精度。

## 2. 对接入口

正常服务地址 `http://127.0.0.1:8765`，以下相对地址均以 `/api/automation/v1/` 开头。服务必须保留本机监听；本版本没有给远程 LLM 的认证/多租户设计，不能直接公网转发。

| 方法 | 路径 | 用途 / 请求体 |
| --- | --- | --- |
| GET | `capabilities` | 操作白名单和实现边界 |
| GET | `schema` | 实验文档 JSON Schema |
| GET | `openapi.json` | OpenAPI 接口描述，供 Agent 导入 |
| GET | `state` | 状态、最近实验文档、A–H 最新值/有界序列、运动状态 |
| GET | `devices` | 缓存的X/Z原始计数、速度、反馈年龄、板A/B及限位状态；不执行物理I/O |
| GET | `twin` | 孪生state/registered/fresh/pose/registration/error；仅synced有有效pose |
| GET | `events?after=0` | 按事件序号增量读取；完整日志在运行目录 |
| GET | `ports` | 枚举串口，不打开端口 |
| POST | `validate` / `preview` | `{"experiment": {...}}`；纯验证和展开，无硬件操作 |
| POST | `experiment` | `{"experiment": {...}}`；保存文档 |
| POST | `run` | `{"experiment": {...}, "mode":"simulate", "plan_hash":"...", "idempotency_key":"..."}` |
| POST | `stop` | `{}`；请求停止，继续轮询确认结果 |
| POST | `resume` | `{}`；仅继续 `wait_operator`，不恢复故障任务 |
| POST | `motion/connect` | `{"motion": {...}}`；仅连接、读取和核对配置 |
| POST | `motion/refresh` | `{}`；只读刷新已有连接，真实运动占用时只返回缓存 |
| POST | `twin/register` | `{cad_bounds:{x_left_mm,x_right_mm,z_up_lift_mm,z_down_lift_mm},confirmed:true}`；不移动实物，不接受客户端原始编码器值 |
| POST | `twin/register-datum` | `{datum:"left_up"或"right_down",confirmed:true}`；默认left_up，只取当前可信边界锚点，独立名义显示基准不启用运行限位 |
| POST | `motion/reference` | `{"confirmed":true}`；人工确认当前位置为软件参考点，不执行回零 |
| POST | `motion/enable` | `{"axis":"X"}` 或 `Z`；显式使能 |
| POST | `motion/stop` | `{}`；请求双轴停止并检查新鲜反馈 |
| POST | `motion/disconnect` | `{}`；停止后断开，未确认停止不能当安全释放 |

POST 必须使用 `Content-Type: application/json`。不接受 Python/JS/shell 代码。错误返回 HTTP 4xx/5xx 和 `{"error":{"code":"...","message":"...","path":"..."},"request_id":"..."}`，可能附带 `diagnostic_id`。

`preview` 返回 `experiment`、`plan_hash`、`steps`、`duration_s`、`warnings`。运行必须提交原计划哈希和幂等键；相同键、相同计划及模式不重复启动；换计划或模式必须换键，否则 409。不要在超时后随机生成新键重试实物任务。

## 3. 动作接口

| `op` | 主要字段 | 行为 |
| --- | --- | --- |
| `move_to_column` | `column`，可覆盖上下/横移速度、安全坐标、首列坐标、列距 | 编译为先抬 Z 到安全位置，再移动 X |
| `immerse` | 当前列；可覆盖 `descend_speed_mm_s`、`z_immersed_mm`、`settle_s` | 下降并等待稳定 |
| `withdraw` | 可覆盖 `ascend_speed_mm_s`、`z_safe_mm` | 提拉到安全位置 |
| `dwell` | `seconds`、`acquire:["A",...,"H"]`，可给 `column` | 驻留；指定通道才采集，空数组仅等待 |
| `wait` | `seconds` | 等待，不启动采集 |
| `wait_operator` | `message` | 等待人工确认，Agent 调用 resume 才继续 |
| `repeat` | `count`、`body:[动作...]` | 有界重复，展开后执行 |

精确可用字段见实时 Schema，未知字段会拒绝。最多 3 层重复、500 个展开步骤、8 小时运行预算。采集驻留为 10–3600 秒。`dwell` 可单独给列元数据做静止采集；这不证明实物已经位于该列。几何默认值仅作编辑起点，必须现场标定。

示例文件：[automation-eight-electrodes.json](examples/automation-eight-electrodes.json)。其中第一列采集 A–H，提拉后移动到第八列，180 秒只采 A/C/E/G，提拉速度被第八列的 `0.4 mm/s` 覆盖。

下面是仅使用标准库、**只运行模拟**的 Agent 客户端；从仓库根目录运行即可，不连接滑台或采集板：

```python
import json
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

base = "http://127.0.0.1:8765/api/automation/v1/"

def request(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    call = Request(base + path, data=data, headers={"Content-Type": "application/json"})
    with urlopen(call, timeout=15) as response:
        return json.load(response)

experiment = json.loads(Path("docs/examples/automation-eight-electrodes.json").read_text())
plan = request("preview", {"experiment": experiment})
run_key = str(uuid.uuid4())
started = request("run", {"experiment": experiment, "mode": "simulate",
                         "plan_hash": plan["plan_hash"], "idempotency_key": run_key})
while True:
    state = request("state")
    if state["run_id"] != started["run_id"]:
        raise RuntimeError("The active run changed")
    if not state["busy"]:
        break
    time.sleep(0.5)
print(state["state"], state["manifest_path"])
```

生产 Agent 应另设自己的超时，超时请求 `stop` 后检查 `stop_confirmed`，不能仅凭 HTTP 请求成功或进程退出就认为硬件已停机。

## 4. 实物门禁与停机

2026-10-22 增量：独立双轴首次小幅点动及最终 STOP 已验证，详见[联调记录](RS485-BRINGUP-2026-10-22.md)，但完整高通量实验和机械校准仍未验收。到位判断使用实读驱动角窗（超过一个整步拒绝连接），围绕实际量化命令目标检查，同时要求新鲜零转速/到位标志；该窗口不代表直线精度。软限位和 STOP 静止检查没有按该窗口放宽。

本轮未执行真实双轴运动或双板联合测量。首轮联调先无电极、无液体核实设备固件、地址、方向、行程、丝杆导程、限位及机械间隙。软件停止不是安全认证急停，仍需可触及的物理断电措施。

两轴共享一条串行事务队列，地址必须不同且非广播。协议为参考程序的 Emm 私有二进制协议、固定 `0x6B`，不是 Modbus。连接不会自动使能、归零或改写驱动器配置；不支持的应答/速度配置拒绝运行。固定结束字节不是 CRC，协议没有事务序号，这些限制仍需实机验证。

实物运动计划必须：绑定准确端口/双轴参数 → 显式连接只读核验 → 人工登记参考点 → 分别使能 → 确认运动和电极电气/极化策略。断线/故障后重新核验，不能复用旧参考。旧实时测量、定时任务、升级/烧录和高通量共享互斥；已绑定运动端口不参加旧 USB ASCII 探测。

蓝牙阶段先并行配置两块实际用到的板；两边确认后按本轮 token 放行 START。`acquire` 转成真正的 START mask，不是全采再隐藏。两板软件并行，**没有硬件级同步**；记录各自 START 请求/ACK、板上 uptime 与主机收包时间。任一板失败会停止同伴，不会在未确认停止时继续横移。

每个采集阶段结束会发送固件 STOP，此行为会关闭电极输出，不能解释为“只暂停显示但维持极化”。用户必须确认这种策略适合当前电化学实验；需要持续极化的流程应另行设计，不能跳过门禁。

`stop` 是异步请求；以 `busy=false` 且 `stop_confirmed=true` 判断软件确认完成。失败时保持资源占用；应用重启也不自动续跑。`recovery_required=true` 时显式重试 STOP，必要时按原绑定重新连接运动总线（只读），再停止；不要篡改持久状态文件清除故障。

## 5. 数据与指纹

2026-10-20 用户确认同时需要溯源指纹和电化学特征指纹；下面已实现的是前者。后者的判别用途和特征算法尚未确定，不将当前哈希当成电化学响应特征。

运行目录为输出根下的 `simulate/<run_id>` 或 `live/<run_id>`；留空输出根时使用应用状态目录的 `automation-runs`。每次实验独立，不覆盖旧数据。

- `experiment.json` / `plan.json`：原始实验文档与解析后的动作。
- `events.jsonl`：动作、配置门禁、停止、错误、开始/结束事件和主机时间；串口审计事件也可追踪。
- `samples.jsonl`：每个收到的样本，含 A–H/样品/列/溶液/浓度、板槽/地址/物理通道、seq、uptime_ms、host_unix_s/UTC、counts/current_fa/flags，及 device/electrode/config/step/sample 指纹。
- 各阶段/各板目录保留 collector 原始 CSV、四路文件、温压监测、状态、命令和审计日志。
- `manifest.json`：计划指纹、实验指纹、终态、STOP 确认和文件 SHA256。`hardware_synchronized=false`，`fingerprint_kind=provenance_only`。

哈希采用排序键、紧凑分隔符、UTF-8、非 ASCII 原样输出的规范 JSON；样本哈希计算时不含自身 `sample_fingerprint` 字段。文件哈希针对原始文件字节。运行结束生成文件清单；运行中的文件尚未封存。

当前“指纹”仅指可溯源身份/内容哈希，**未实现未经定义的电化学指纹特征提取**。V5.2 既有第三路模拟异常仍待定位，测到数据不等于八路模拟精度验收。完整数据在磁盘，状态接口只保留有界视图，不能用 UI 缓存替代原始记录。
