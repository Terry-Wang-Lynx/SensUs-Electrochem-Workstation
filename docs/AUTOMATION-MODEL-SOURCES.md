# 高通量页的 Fusion / CAD 模型来源

2026-10-20，依据会话「完善运动机构采购规格」（`01a101dd-14bc-7841-b232-2ca5c28459b3`）及同级机械项目 `electrode-test-automation` 的已保存导出。没有修改 Fusion 原文档或机械源。

## 根因与修正

旧页面只复用局部板卡/夹持器网格，其余整机由基础方块拼装，误将 Z 支架挂在 X 移动组下，孔板反而固定。公共底座、带筋背架及托盘也与保存设计不符。旧测试只检查“96 孔和两块板”，未检查运动归属。

现为三个并列组：

- `fixedGroup`：280 × 230 mm 底座、X 固定滑台/电机、Z 带筋背架及 Z 固定滑台/电机。
- `xGroup`：X 滑块、孔板托盘、96 孔板、对应五金、孔位拾取覆盖层和列标签。只有托盘侧横移。
- `zGroup`：Z 滑块、橙色双托槽臂、八工位卡座、适配器/电极、两块板及对应五金。板在臂背面，接口朝上、电池面朝臂。

## 保存源

以下为机械项目内的相对路径。全部 SHA256、实体索引、平移和包围盒随 `software/host/pa_host/gui/ht-apparatus.json` 保存。

| 来源 | 用途与边界 |
| --- | --- |
| `outputs/20261006-130753-Fusion-v3-X150-Z50/bioautomation-v3.step` | Fusion 原生整机坐标、底座、背架、滑台和托盘；采购件本身仍是参考外形 |
| `outputs/20261011-full-DIN-fastener-audit/bioautomation-full-DIN-audit.step` | 沿用原整机的机械体、适配器/电极、54 个 DIN 五金；此审查 STEP 未确认导入 Fusion，24 个检查辅助体不显示 |
| `outputs/20261012-lower-pin-relief-v3/02-eight-gap1p80-relief6/02-eight-gap1p80-relief6.step` | 替换为下方 6 mm 避让槽的八工位卡座；独立 CAD 修订 |
| `outputs/20261017-orange-arm-dual-cradle/01-orange-arm-dual-cradle/01-orange-arm-dual-cradle.step` | 替换为真正导出的双托槽臂，不再用旧 Three.js 方块代替；配合仍待试打 |
| `prototypes/dual-board-placement/geometry.json` | 保留测量板参考网格；旧导出简化过部分小元件，电池/插头为名义占位 |

输出含 108 个机械/参考/五金网格、96 个从 BRep 圆柱面提取的孔位，测量板另保留两份 104 网格参考。五金的 STEP 遍历顺序与清单不同，按体积及世界包围盒唯一匹配，不按清单序号盲配。尺寸采用不使用三角化缓存的精确 BRep 包围盒，避免网格挠度扩大边界。

## 坐标边界

保存位姿为孔板中心 X=`49.5 mm`、Z 提升=`35 mm`；首列孔在世界 X=`0`，其余列沿 +X 每隔 9 mm。后续列来到固定电极下时，托盘世界 X 位移取编译目标的负值；Z 的相对示意位移为 `-(target_mm - z_safe_mm)`。

这只修正显示，**不改变 RS485 方向、控制坐标和实验默认值**。控制器零点、正向及液位仍需实物校准，CAD 的 Z 提升 35 mm 不是控制器零点。动画压缩时长，不据画面认定浸液深度或碰撞安全。线束只示意连接，不宣称按 200 mm 放样。模型也不是当前未保存 Fusion 文档的实时镜像。

2026-10-23新增[编码器反馈映射](LIVE-FEEDBACK-TWIN.md)：明确连接实物后不再以计划动画驱动模型；有效配准提供真实反馈姿态，否则标为CAD参考/实物未知。名义X150/Z50行程、X±49.5孔列显示范围、Z35展示抬升及0…45展示范围都不是已测机械端点，不能自动替代安全边界的CAD坐标。机械源`fusion/BioAutomationNative/model_v3.py`中的简化滑台未定义精确止挡，`prototypes/full-fastener-audit-v1/build.py`的X±75/Z95.5与145.5是安装孔而非滑块终点。原始计数按导程一比一换算，仅求平移对应，不把安全窗口缩放到这些名义范围。

## 再生成与验证

开发工具 `tools/export_automation_geometry.py` 需要 CadQuery 2.8.0，可用机械项目已有的 `prototypes/side-socket-v2/.venv/bin/python`：

```text
python tools/export_automation_geometry.py /absolute/path/to/electrode-test-automation
```

应用运行不需要 CadQuery。导出器只读源，前后复核输入哈希；不执行原机械生成器，不覆盖 STEP/F3D。场景加载网格，不重新造底座、背架或托盘。

回归覆盖几何尺寸/来源、组归属、X/Z 独立运动、12 列与八电极的对位。浏览器另做目视检查；这不是制造整改、实物运动或电化学精度验收。
