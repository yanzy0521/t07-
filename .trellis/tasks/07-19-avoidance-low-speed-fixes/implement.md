# 实施计划

## 1. Player 路径跟随

- 将 `_path_waypoint()` 改为累计 segment 长度 lookahead。
- 在 A* heading clearance 低于 `PLAN_CLEARANCE` 时计算局部 VFH 候选。
- 仅在局部 clearance 更大时切换 heading，并清除实际控制 waypoint 标记。

## 2. A* 邻接安全

- 增加点到线段最短距离的内部 helper。
- 增加普通栅格边与膨胀障碍圆的连续碰撞检查。
- 增加对角 corner 检查。
- 增加 blocked start 第一跳向外脱困判定。

## 3. 静态复核

- 静态确认函数签名、导入和类型标注一致。
- 静态确认无障碍路径不受额外阻挡。
- 静态确认目标和速度参数没有改变。
- 静态确认已有用户修改未被回退。

## 验证约束

根据项目规则，本窗口不运行 build、test、lint、type-check、format、仿真、部署、开发服务器或 IDE diagnostics。用户后续手动验证应重点观察 A* 成功率、waypoint、全局与局部 clearance、最终 heading、`clearance_factor` 和 `vx/vy`。

## 风险与回退点

- 连续边检查可能让窄通道路径更频繁回退 VFH；若手动仿真确认过于保守，应优先复核统一半径语义，而不是删除边检查。
- blocked start 例外只允许增加与重叠障碍中心距离的第一步；多障碍夹持场景仍可能没有可行邻居。
- 如局部 heading 覆盖造成路径摆动，可单独回退 Player 中的局部覆盖逻辑，不必回退 A* 几何修复。
