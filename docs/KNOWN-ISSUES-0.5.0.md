# 0.5.0 已遇到故障、回归与未验收项

不能保证“避免所有 bug”。本表区分软件回归、受限实物检查与尚待验收的边界，供另一台电脑按同一流程复现，而不是绕过检查继续运动。

| 已遇到问题 | 当前处理及主要回归 |
| --- | --- |
| 双BLE连接互斥、前后板/通道混淆 | 独立连接管理、显式A/B绑定、OS身份+芯片通道映射；`test_ble_connections.py`、`test_dual_ble_gui.py`、`test_route_runtime.py` |
| 四点按钮无反馈、150mm旧范围、第三点后W异常 | 采点/完成状态、坐标容器及固定目标表示修正；`test_calibration_feedback_gui.py`、`test_calibration_actual_extent.py`、`test_keyboard_w_after_capture.py` |
| 短按/换键/松键取消被误判、过期按键复活、边界后全锁死 | 取消溯源、租约/代次、方向性边界和快速STOP；`test_keyboard_target_release.py`、`test_keyboard_directional_limits.py`、`test_keyboard_post_stop_settling.py` |
| 移动中跨轴/配置查询及压缩间隔引起EE/多余回包 | 活动轴独占、原20ms总线间隔、真实静止后复核保持轴；`test_live_motion_bus.py`、`test_keyboard_query_cadence.py` |
| USB端口变号/刷新后旧状态延续 | 显式重连重置、已枚举新端口确认、停止所有权保留、前端修订提示；`test_motion_port_recovery.py`、`test_frontend_revision_gui.py` |
| 断线恢复中编码器未知导致边界状态计算异常 | 无新鲜有效计数/配置时不生成边界距离，保留未知STOP和故障状态；`test_rs485_replug_stop.py` |
| Z完成姿态/初始安全高度位于通常采集窗外 | 保存完成姿态与唯一首次Z到安全高度走廊，不授权横移/直接浸入；`test_calibration_finish_pose.py`、`test_guided_run_entry.py` |
| 正常到位多发FE改写目标、结束时/STOP竞态误报 | 自然有限完成及双轴停泊证明，显式STOP仍新发双轴FE；`test_automatic_arrival_settling.py` |
| 20mm/s长段接近首列时瞬时4.5166µm越点 | 原容差不变，内侧预备+慢速精靠近，整段锁/截止时间/保持轴基线；`test_endpoint_approach.py`、[实物边界](ENDPOINT-APPROACH.md) |
| A–H的5秒被误认为全部列、应用高级参数无反馈 | 每列真实时长和作用域标注、完整保存确认及勾选；`test_column_duration_visibility.py`、`test_advanced_settings_feedback.py` |
| 多轮/折返顺序或记录复用、暂停/START前假进度 | 有限有序路径、完整访问状态机、独立轮次/访问身份和运行快照进度；`test_guided_routes.py`、`test_route_runtime.py`、`test_experiment_progress.py` |
| 仅全量测试中的两项键盘测试失败 | 已退役5ms假设改为现行20ms安全断言；日志单例测试在独立进程验证，未改变生产日志目录；`test_emm_command_spacing.py`、`test_keyboard_trace.py` |
| 冻结包与开发机PSD后端不同 | 便携包显式包含同版SciPy、锁定运行/验证依赖；`test_release_reproducibility.py`及冻结包测试 |
| 云端/Windows运行网页回归时命令行过长 | JavaScript测试通过UTF-8标准输入执行，不将整份网页脚本塞入命令行参数；保留全部DOM行为断言；`test_node_test_utils.py` |

## 尚未解决或不能据软件保证的事项

- V5.2 CH3 悬空模拟异常未定位；四路模拟精度要以逐路已知负载 / 电压实测验收，BLE通路通过不代表精度通过。
- 端点新策略做过约2mm单向受限指令/低速停稳检查，没有修正后约199mm原高速横移的完整实板复验，也未签核双轴双板完整自动实验。
- 两板不是硬件同步；主机到包时间不是同一板端时钟。掉线数据留空，不补零或伪造续采。
- 3D含名义几何与配准假设，不能代替碰撞、液位、机械全行程或外部直线精度验证。
- 电机计数不保证断电绝对保持；重连必须新标定。软件STOP/窗口保护不能替代物理限位、急停、保持力和现场安全余量。
- macOS安装包未经Developer ID公证；Windows新版本软件冒烟需以本次CI结果为准，不宣称另一台实体Windows机器或Intel Mac已经实测。
- 固件主机逻辑测试在macOS26/Apple clang17可能遇到系统ASan启动自旋；LLVM22可运行相同测试并已验证，见[复现指南](REPRODUCING-0.5.0.md)。这不影响不包含sanitizer的工作站App。
- 板端电化学特征指纹算法未实现；当前SHA256仅是溯源。

提交新问题时保留诊断编号、运行目录、events、plan、标定快照与原始样本；不要先删状态或反复启动。未知STOP先按提示确认安全，不用清零、回零或强行导入旧标定试探。
