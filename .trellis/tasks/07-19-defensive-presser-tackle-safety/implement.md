# 防守抢断与禁区安全实施计划

## Implementation

- [x] 在 `src/param.py` 中建立独立 defensive presser 抢断力度、进入/退出距离和禁区球后侧参数。
- [x] 将 defensive presser 力度调整为 4.0，进入距离调整为 0.65m，退出距离设为 0.80m。
- [x] 在 `src/player.py` 中根据 `context.field` 判断球是否位于己方禁区。
- [x] 禁区外保持直接追球，满足距离后固定向 `+X` 低力度出脚。
- [x] 禁区内增加 `pose.x <= ball.x - 0.10m` 的球后侧判断。
- [x] 禁区内不安全时释放踢球，移动到球后 0.35m 且受场地边界限制的目标。
- [x] 保持禁区内外都不执行 offensive striker 绕球、球后角度对齐或外侧目标校准。
- [x] 更新 action 标签，使 chase、safe-side approach 和 tackle 均保持 defensive presser 语义，并兼容 M-05 前缀保护。
- [x] 检查 `DEFENSIVE_PRESSER_CLEAR_DISTANCE_M` / `DEFENSIVE_PRESSER_CLEAR_POWER` 的全部引用，只替换 defensive presser 路径。
- [x] 保持 offensive striker、守门员 CLEAR、开球和重启代码不变。

## Manual Verification Only

按项目规则，不运行 build、test、lint、type-check、format、仿真、部署、开发服务器、IDE diagnostics 或任何运行时验证命令。

建议用户手动验证：

1. 禁区外约 0.65m 内快速以 4.0 力度出脚。
2. 禁区外位于球前方或侧方时仍不等待球后侧校准。
3. 0.65m/0.80m 附近距离小幅变化时不频繁启动和释放。
4. 己方大禁区内位于球前方时不出脚，而是移动到球后安全点。
5. 己方大禁区内位于球后侧时直接向 `+X` 低力度出脚。
6. 小禁区使用相同安全规则，不要求守门员优先禁踢。
7. DEFENDING 和 CONTESTED 都使用相同行为。
8. offensive striker 射门、守门员 CLEAR、我方开球力度不变。
9. M-05 不覆盖正在追球、获取安全侧或出脚的 presser。

## Review Gates

- [ ] 禁区边界来自 `context.field`，没有写死成人场地坐标。
- [ ] 禁区外无额外方向或球后侧校准。
- [ ] 禁区内错误侧不会沿用旧踢球命令。
- [ ] 没有使用 offensive striker 的参数或动作状态。
- [ ] 未覆盖或回退当前工作区已有修改。
