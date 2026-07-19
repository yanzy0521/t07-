# 进攻射门动态力度实施计划

## Implementation

- [ ] 在 `src/param.py` 中以 `OFFENSIVE_STRIKER_` 前缀定义 4.0/8.0 力度边界和 2.0m/10.0m 距离阈值。
- [ ] 在 `src/player.py` 中增加只供 offensive striker 射门规划使用的距离到力度计算方法。
- [ ] 让 `Player.plan_offensive_shot()` 使用球到当前实际射门目标的二维距离计算力度。
- [ ] 对异常目标、非有限距离、异常阈值和异常计算结果使用安全回退，并对最终力度执行 `[4.0, 8.0]` 夹取。
- [ ] 复用 debug draw 展示射门距离和最终力度。
- [ ] 保持 `plan_defensive_clear()`、守门员 CLEAR、开球及底层 `kick()` 的其他调用不变。
- [ ] 检查固定进攻力度旧参数的所有引用，只移除已经被动态规划替代的 offensive striker 引用。

## Manual Verification Only

按项目规则不运行 build、test、lint、type-check、format、仿真、部署、开发服务器、IDE diagnostics 或运行时验证命令。

用户手动验证：

1. 球靠近对方球门时力度接近 4.0。
2. 中场附近射门力度位于 4.0–8.0。
3. 远距离射门达到但不超过 8.0。
4. 阈值附近小幅移动时力度连续平滑。
5. front partner 临时接管时使用相同动态力度。
6. defensive presser 解围仍为 5.0。
7. 守门员 CLEAR 仍为 7.0，我方开球仍为 5.0。

## Review Gates

- [ ] 射门距离来自球到实际目标，而非球的 X、半场或机器人到球距离。
- [ ] 所有 offensive striker 正常和回退力度均处于 `[4.0, 8.0]`。
- [ ] 未修改射门触发时机和任何非进攻射门职责。
- [ ] 未覆盖或回退工作区已有修改。
