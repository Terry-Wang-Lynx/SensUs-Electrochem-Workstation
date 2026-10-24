# 高通量自动化实验：API-first 与可交互3D

**2026-10-20纠正**：用户要求按既有Fusion设计，三维装配必须是X移动孔板托盘、后方Z背架固定、Z带动悬臂/电极/双板。旧局部网格拼装示意已退役；用保存STEP导出的完整分组网格，详见`../../AUTOMATION-MODEL-SOURCES.md`。用户另确认同时需要溯源和电化学特征指纹；后者尚未实现，现SHA256只承担前者。

本设计取代前一份高通量页的“表单优先”布局。用户明确：页面中央为可旋转/缩放的整机3D，按孔板列配置溶液/浓度/驻留/速度/位移；Scratch式动作原语、默认值与覆盖；驻留阶段选择A–H任意通道采集；逐样本记录身份/配置指纹、主机及板端时间、数值和完整日志；给其他LLM可调用的结构化接口及编程窗口，接口优先于手动操作。

## 已确认边界

- 两块V5.2，各4路，固定映射board A的CH1–4→A–D、board B的CH1–4→E–H。
- X/Z使用一个USB-RS485总线、两个不同非广播地址；参考`~/Downloads/镀膜机上位机/`的Emm私有协议，不是Modbus。
- 中央视图先复用现有CAD/网格和机构几何，明确不是已经完成的3DGS扫描重建。鼠标拖动只改变视角；点击列仅编辑/预览，不触发实物运动。
- 硬件标定、地址、轴方向、参考位置及运动/极化策略未实机确认。默认模拟；本轮代理不打开运动串口、不使能、不回零、不移动实物。真实运行必须明确选择live且满足门禁。
- 原参考只实现一个Qt滑台窗口；默认39mm/2mm不是实际双轴校准，自动使能/碰撞回零/未校验地址的响应不能照搬。
- 指纹当前最低实现为电极/设备/配置/动作及样本内容的溯源哈希；电化学特征若无明确算法，不编造特征值。

## 实验文档（声明式JSON，不执行任意代码）

```json
{
  "schema_version":"1.0",
  "name":"八电极浸泡测量",
  "output_dir":"/absolute/experiment/root",
  "bindings":{
    "boards":{"A":{"address":"","settings":{}},"B":{"address":"","settings":{}}},
    "motion":{"port":"","baud":115200,"axes":{
      "X":{"address":1,"lead_mm":2,"travel_mm":150,"positive_direction":0,"max_speed_mm_s":10},
      "Z":{"address":2,"lead_mm":2,"travel_mm":50,"positive_direction":0,"max_speed_mm_s":10}
    }}
  },
  "defaults":{"descend_speed_mm_s":0.5,"ascend_speed_mm_s":1,"translate_speed_mm_s":2,"dwell_s":180,"settle_s":2,"z_safe_mm":0,"z_immersed_mm":6,"x_first_mm":0,"column_pitch_mm":9,"channels":["A","B","C","D","E","F","G","H"]},
  "electrodes":[{"id":"A","name":"样品A"},{"id":"B","name":"样品B"},{"id":"C","name":"样品C"},{"id":"D","name":"样品D"},{"id":"E","name":"样品E"},{"id":"F","name":"样品F"},{"id":"G","name":"样品G"},{"id":"H","name":"样品H"}],
  "columns":[{"index":1,"solution":"PBS","concentration":{"value":0,"unit":"uM"},"overrides":{}},{"index":8,"solution":"待测液","concentration":{"value":10,"unit":"uM"},"overrides":{"dwell_s":180}}],
  "program":[{"op":"move_to_column","column":1},{"op":"immerse"},{"op":"dwell","acquire":["A","B","C","D","E","F","G","H"]},{"op":"withdraw"},{"op":"move_to_column","column":8},{"op":"immerse"},{"op":"dwell","seconds":180,"acquire":["A","C","E","G"]},{"op":"withdraw"}],
  "safety":{"motion_policy_confirmed":false}
}
```

几何默认值仅是编辑起点，界面标记待实物校准。默认值优先级：动作显式字段 > 当前列overrides > defaults。列必须先定义，浓度有限非负并携带单位。op白名单：move_to_column、immerse、withdraw、wait、dwell、wait_operator、repeat（count/body）。repeat最多3层、展开最多500步、估计总时长不超过8h；不允许任意Python/JS/shell代码。

move_to_column原语始终先抬Z至safe，再横移X；安全序列由编译器生成，不要求LLM手写。immerse/withdraw解析当前列速度及深度；dwell.seconds或列dwell_s确定驻留时间，acquire=[]仅等待，指定A–H时启动相应板卡mask；不偷偷采全通道再筛选。覆膜由慢速withdraw与wait组合表达，无另一个冲突的配方模型。

## Agent API v1

前缀`/api/automation/v1`；默认仅本机，不为远程LLM开放监听地址或绕过安全检查。

- GET capabilities、schema、openapi.json：机器可读功能/JSON Schema/OpenAPI，包含未验收边界。
- GET state、ports、events?after=seq：状态、串口枚举（不打开）、增量事件。
- POST validate / preview，body={experiment}：验证/展开计划，无硬件副作用；返回plan_hash、steps、warnings、duration_s。
- POST experiment，body={experiment}：保存最近文档。
- POST run，body={experiment,mode:"simulate"|"live",plan_hash,idempotency_key}：计划哈希匹配、幂等运行；同key同计划返回原run，不重复执行；同key不同计划拒绝。
- POST stop / resume：停止所有资源/人工等待后继续；故障不自动续跑。
- POST motion/connect {motion}、motion/reference {confirmed:true}、motion/enable {axis}、motion/stop、motion/disconnect：只在空闲且明确请求时执行；连接仅核版本/配置/位置/状态，不使能、不回零。

状态：idle、validating、running、waiting_operator、stopping、completed、stopped、error。返回{state,busy,mode,run_id,step_index,column,position:{X,Z},message,error,stop_confirmed,channels:[{id,name,board_slot,channel,time_s,current_nA,valid,latest_current_nA}],motion,events,manifest_path,experiment}。模拟结果每处标识synthetic，导出目录与实测分开。错误必须有code/message/path/request_id；不要把未确认停机写成安全停止。

## 采样与审计

所有测量保留原有各板raw/all-channels/monitor/state/audit；另写实验manifest、events.jsonl与samples.jsonl。每条样本至少包含run_id、step_id、列/溶液/浓度、A–H身份、样品名、board_slot/address、板端seq/uptime_ms、host_unix_s/UTC、counts/current_fa/flags、device/config/step/sample指纹。不把接收时间当同步ADC采样时间。运行结束后列出各文件SHA256；无电化学准确度/指纹特征已验证的暗示。

两块板并行、非硬件同步。collect_ble扩展可选channel_mask和start_gate：默认15、无gate保持旧行为；两板都配置确认后才放行本次gate，各板记录真实开始时间。任何配置/流/STOP失败均使本阶段失败并尝试停止另一板；不在另一板继续时横移。

## 交付

独立新模块，不扩写旧单板标定算法。测试覆盖schema/默认覆盖/危险动作/计划边界、串口伪帧、双板启动门禁与取消、逐样本溯源、幂等、UI无隐藏硬件副作用。提供LLM可复制的curl/Python例子；UI JSON编辑器只发送此结构化协议。第三路已知模拟异常保留提示；无双板/双轴实机证据时只报告模拟/单元测试。
