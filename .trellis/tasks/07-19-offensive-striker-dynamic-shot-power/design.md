# 进攻射门动态力度设计

## Boundary

动态力度只由 `Player.plan_offensive_shot()` 负责。所有通过 `Player.attack()` 执行射门的普通进攻持球者，包括固定 offensive striker 和临时接管的 front partner，自动共享该规划。

`Player.plan_defensive_clear()`、`Player.goalkeeper_clear()` 和我方开球的显式 `Player.kick()` 调用保持原有独立力度，不进入动态进攻计算。

## Parameter Contract

在 `src/param.py` 中集中定义：

- `OFFENSIVE_STRIKER_SHOT_POWER_MIN = 4.0`
- `OFFENSIVE_STRIKER_SHOT_POWER_MAX = 8.0`
- `OFFENSIVE_STRIKER_SHOT_POWER_NEAR_DISTANCE_M = 2.0`
- `OFFENSIVE_STRIKER_SHOT_POWER_FAR_DISTANCE_M = 10.0`

不再使用固定的普通进攻前后场射门力度作为正常计算分支。异常回退使用夹在上述边界内的安全值。

## Data Flow

1. `plan_offensive_shot()` 按现有逻辑取得本次实际射门方向和可视化射门目标。
2. 使用球坐标与该实际目标坐标计算二维欧氏距离。
3. 距离小于等于 2.0m 时返回 4.0；大于等于 10.0m 时返回 8.0。
4. 中间距离计算归一化比例 `(distance - near) / (far - near)`，再在线性区间 `[4.0, 8.0]` 插值。
5. 对最终结果再次夹取到 `[4.0, 8.0]`。
6. 复用 debug draw，在场外显示当前射门距离和最终力度；原射门目标 X 标记保持不变。

## Safety Fallback

球或上下文不可用时维持现有 `None` 规划结果，由上层停止射门。若目标坐标、距离、阈值或插值结果不是有限数值，则使用安全回退力度 4.0，并再次执行 `[4.0, 8.0]` 夹取。

## Compatibility

- 不改变射门方向和实际目标的选择逻辑。
- 不改变 `Player.attack()` 的距离、对齐、绕球或触发条件。
- 不改变 offensive striker/front partner 的角色选择。
- 不改变 defensive presser、守门员、开球、重启、M-05、readiness 或路径规划。

## Rollback Shape

如需回退，只需恢复 `src/param.py` 的固定进攻力度参数，并在 `plan_offensive_shot()` 中恢复固定力度选择；其他角色和动作入口不需要变化。
