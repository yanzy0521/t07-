# 技术设计

## 修改策略

只在 offensive striker 的 `attack()` 中增加一个“近球拼抢破坏”优先分支：

1. 机器人距离球足够近；
2. 至少一个对手距离球也足够近；
3. 常规 offensive 对齐条件尚未满足、否则继续执行高质量射门；
4. 满足以上条件时，直接按 `plan_offensive_shot()` 的对方球门方向低力度出脚。

该分支避免 offensive striker 在 close contest 中继续追求 `OFFENSIVE_STRIKER_BEHIND_BALL_DISTANCE_M` 或绕球圆弧，从而减少视觉上的后撤。

## 为什么不改防守 presser

`defensive_press_and_clear()` 当前已经：

- 切换到 `defensive_presser` 职责；
- 清除 offensive 绕球状态；
- 距球大于阈值时用 `_chase_ball_aggressively()` 全向高速直压球点；
- 距球进入 `DEFENSIVE_PRESSER_TACKLE_ENTER_DISTANCE_M` 后低力度向前抢断。

因此本任务不改它，避免把已直接拼抢的路径复杂化。

## 质量保护

- 如果 offensive striker 已满足原射门距离和球后对齐条件，仍走原 `plan_offensive_shot()` 高质量射门。
- 破坏球只在对手也贴近球时触发，普通无人进攻仍会后撤到球后保证射门质量。
- 破坏球方向仍使用对方球门方向，不向己方门或随机方向踢。
- 破坏球力度单独设为低于正式 offensive shot 的值。

## 风险

- 后端实际踢球质量和接触模型只能通过仿真确认。
- 如果对手识别延迟，分支可能触发偏晚。
- 如果用户希望所有进攻都不后撤，还需要后续明确降低射门质量要求；本任务不做。
