# 修复抢球机器人后撤

## Goal

减少抢球机器人在有对手贴近球时为了追求完美球后位而先后撤的行为，让其在近球拼抢场景优先直接破坏球，同时保留无人干扰时的进攻射门质量。

## Background

当前最新代码中，防守 presser 已经使用 `defensive_press_and_clear()` 直压球点并在 0.90m 内低力度抢断；进攻 striker 仍使用 `attack()` 的球后点和绕球逻辑，未对“对手已接近、我方也已贴球”的拼抢场景做快速破坏分支。因此用户看到的“抢球前先后撤”最可能来自进攻 striker 在 close contest 中仍进入 offensive behind-ball/circle 质量优先路径。

## Requirements

- 保留无人或低压力进攻时的既有球后点 / 绕球 / 高质量射门逻辑。
- 当 offensive striker 已接近球且最近对手也接近球时，优先执行低力度、朝对方球门方向的快速破坏球，不再先后撤到完美球后位。
- 不改变 defensive presser 当前直压球点和低力度抢断逻辑。
- 不改变战术角色分配、避障参数、球门参数或路径规划参数。
- 修改应局限于 `src/player.py` 与必要调参常量。

## Constraints

- 不运行 build、test、lint、type-check、format、仿真、部署、开发服务器、IDE diagnostics 或任何运行时验证命令。
- 不清理、回退或覆盖工作区已有修改。
- 只做高把握的最小修改，不扩大到多机器人整体避障或角色重构。

## Acceptance Criteria

- [x] Offensive striker 在近球且对手近球时存在直接破坏球分支。
- [x] 直接破坏球使用对方球门方向和较低力度，而不是任意方向乱踢。
- [x] 非拼抢压力下仍保留原 offensive shooting 质量逻辑。
- [x] Defensive presser 逻辑未被改坏。
- [x] 未运行项目禁止的任何验证命令。
