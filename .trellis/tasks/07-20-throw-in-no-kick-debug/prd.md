# 排查我方界外球不发球

## Goal

定位并最小修复“我方界外球策略运行时不发球、机器人站着等待”的原因，范围仅限我方界外球策略。

## Requirements

- 只检查和必要时修复我方界外球逻辑。
- 不实现球门球、角球、中场开球等其他定位球策略。
- 不修改 GameController、runtime、codec、backend、ROS 或路径规划器，除非代码证据表明这是唯一必要修复点。
- GameController 仍是唯一规则权威；我方界外球策略只应在以下条件同时满足时执行：
  - `game.state == GameState.PLAYING`
  - `not game.stopped`
  - `game.set_play == SetPlay.THROW_IN`
  - `game.kicking_team == context.team_id`
- 不伪造或新增规则状态推断，例如 `ball_free`、`possession`、`last touch`、`set play expired`、`event id`。
- 以当前工作区文件为准，同时参考提交 `9f5a491` 中的界外球实现差异。
- 不回退或覆盖用户及其他并行修改；如果同文件变更无法安全区分，先停止并说明。
- 遵守项目手动验证规则：不运行 build、test、lint、type-check、format、仿真、部署、服务器或 IDE diagnostics。
- 优先定位实际卡点，包括是否未进入界外球路由、初始化失败并 consumed、POSITIONING 等 kicker/receiver、KICKING 未调用或底层 kick 未触发。
- 若需要改代码，优先采用小补丁：增加可见 reason/action/debugdraw，避免一上来重构或扩大策略范围。

## Acceptance Criteria

- [ ] 明确说明当前运行代码中界外球卡住的阶段或最可能阶段。
- [ ] 明确说明对应的 reason/action 或会导致“站着等”的控制流。
- [ ] 如果能安全最小修复，则提供补丁；否则只提供可操作的最小修复建议并说明阻塞原因。
- [ ] 未运行任何自动验证命令，并在最终回复中明确说明。

## Notes

- 重点检查文件：`src/main.py`、`src/player.py`、`src/param.py`，并注意当前工作区可能还有未提交改动。
