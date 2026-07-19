# 防守抢断与禁区安全设计

## Behavior Boundary

本设计只修改 `Player.defensive_press_and_clear()` 和 `Player.plan_defensive_clear()` 所代表的 defensive presser 路径。普通防守和争议球都通过该入口执行，因此自动共享新行为。

offensive striker、守门员 CLEAR、开球、重启和其他走位入口保持不变。

## State Model

复用现有单一 `_kicking` 底层状态，不创建第二个踢球控制器。defensive presser 增加独立距离迟滞：

- 非踢球状态在 `0.65m` 内进入抢断；
- 已处于 defensive presser 踢球状态时，超过 `0.80m` 才退出；
- 职责切换仍由现有 `_ball_handling_role` 清除旧踢球状态。

## Field Zones

己方禁区从 `context.field` 动态计算：

- `own_goal_line_x = -context.field.length / 2`
- 禁区前沿为 `own_goal_line_x + context.field.penalty_area_length`
- 横向边界为 `context.field.penalty_area_width / 2`

小禁区不建立另一套逻辑；它位于大禁区内，因此自然使用同一安全条件。

## Outside Own Penalty Area

1. presser 直接朝球移动，不绕球、不站进攻球后点。
2. 距离满足迟滞门槛后立即以 `4.0` 力度向场地正前方 `+X` 出脚。
3. 不检查球后侧、不检查横向对齐、不等待方向校准。

## Inside Own Penalty Area

1. 先判断 presser 是否满足 `pose.x <= ball.x - 0.10m`。
2. 满足球后侧条件且距离满足迟滞门槛时，以 `4.0` 力度向 `+X` 出脚。
3. 不满足球后侧条件时立即释放旧踢球命令，向 `(ball.x - 0.75m, ball.y)` 移动。`0.75m` 使目标位于约 `0.5m` 的球障碍之外，避免路径规划目标不可达。
4. 球后目标限制在场内安全范围内，但不启用 offensive striker 绕球、角度对齐或外侧目标选择。
5. presser 不必走完整个 0.75m；一旦纵向位置满足后侧安全条件，下一帧立即转为直追和低力度抢断。
6. 该安全动作使用可辨识 action 标签，使 M-05 将正在获取安全侧的 presser 视为受保护职责。

## Parameters

集中放在 `src/param.py`：

- `DEFENSIVE_PRESSER_TACKLE_POWER = 4.0`
- `DEFENSIVE_PRESSER_TACKLE_ENTER_DISTANCE_M = 0.65`
- `DEFENSIVE_PRESSER_TACKLE_EXIT_DISTANCE_M = 0.80`
- `DEFENSIVE_PRESSER_PENALTY_BEHIND_MARGIN_M = 0.10`
- `DEFENSIVE_PRESSER_PENALTY_BEHIND_TARGET_DISTANCE_M = 0.75`
- `DEFENSIVE_PRESSER_PENALTY_TARGET_FIELD_MARGIN_M`：用于限制安全目标在场内，建议复用当前防守场地余量数值 `0.30m`，但保留独立前缀。

## Kick Planning

`plan_defensive_clear()` 不再瞄准对方球门中心，直接返回场地绝对方向 `0.0` 弧度和 `DEFENSIVE_PRESSER_TACKLE_POWER`。这不会影响 offensive striker 的射门目标或守门员 CLEAR 目标。

## Compatibility

- 不改变 defensive presser 选择迟滞或攻防模式判断。
- 不改变 defensive protector 的站位。
- 不改变守门员六状态和 CLEAR 力度。
- 不改变 M-05 机制，只同步保护新的 presser 安全侧 action 标签。
- 不改变 readiness、罚下、重启和路径规划。

## Rollback

如需回退，仅恢复 defensive presser 的固定 0.5m 门槛、5.0 力度和原解围目标，并删除禁区球后侧分支；其他职责不需要变化。
