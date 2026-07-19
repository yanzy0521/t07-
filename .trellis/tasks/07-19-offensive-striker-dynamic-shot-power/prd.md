# 进攻射门动态力度

## Goal

让普通进攻中的 offensive striker 根据球到当前实际射门目标的距离平滑调整射门力度，使近距离射门更轻、远距离射门更有力，同时保持 defensive presser、守门员和开球的踢球配置完全独立。

## Background

- `Player.plan_offensive_shot()` 是普通进攻主 striker、前场搭档临时接管以及其他复用 `Player.attack()` 场景的统一射门规划入口。
- 当前 offensive striker 使用固定的 `OFFENSIVE_STRIKER_KICK_POWER` / `OFFENSIVE_STRIKER_BACKFIELD_KICK_POWER`。
- `Player.plan_defensive_clear()` 已使用独立的 `DEFENSIVE_PRESSER_CLEAR_POWER`，守门员和开球也分别显式传入自己的力度。
- 成人场地长度为 14m；当前默认射门目标位于对方门线后 `GOAL_TARGET_DEPTH_M`，但规划代码必须使用实际目标坐标计算欧氏距离，不能依赖场地半区或球的 X 坐标。

## Requirements

1. 动态力度只在 `Player.plan_offensive_shot()` 代表的 offensive striker 射门路径中生效。
2. offensive striker 射门力度下限为 4.0，上限为 8.0；最终规划结果必须再次限制在该区间。
3. 力度依据球当前位置到当前实际射门目标的二维欧氏距离计算。
4. 小于等于近距离阈值时使用 4.0；大于等于远距离阈值时使用 8.0；中间使用连续、单调递增的线性插值。
5. 最小力度、最大力度、近距离阈值和远距离阈值集中定义在 `src/param.py`，名称带 `OFFENSIVE_STRIKER_` 前缀。
6. 球、目标、距离或计算结果异常时使用位于 `[4.0, 8.0]` 内的安全回退力度。
7. 通过现有 debug draw 展示当前 offensive striker 射门距离和最终力度，不新增外部接口或网络通信。
8. 不改变射门方向、目标选择、触发距离、对齐、绕球、角色选择、攻防模式、M-05、readiness 或路径规划。
9. 不改变 defensive presser、守门员 CLEAR、我方开球及其他重启动作的力度。

## Out of Scope

- 传球力度规划。
- 防守、守门员或开球动态力度。
- 新射门目标算法、新攻防状态、T07.1、T08-T11。
- 与本需求无关的重构或清理。

## Confirmed Parameters

- 最小射门力度：4.0。
- 最大射门力度：8.0。
- 近距离阈值：2.0m。
- 远距离阈值：10.0m。
- 2.0m–10.0m 之间采用线性插值。

## Acceptance Criteria

- [ ] 普通进攻主 striker 和临时接管的 front partner 均使用同一动态进攻射门力度。
- [ ] 射门力度随球到实际射门目标的距离连续、单调增加。
- [ ] 近距离为 4.0，远距离为 8.0，中间距离位于两者之间。
- [ ] offensive striker 规划出的最终力度在所有正常和异常情况下都位于 `[4.0, 8.0]`。
- [ ] defensive presser 解围力度仍为 5.0，守门员 CLEAR 仍为 7.0，我方开球仍为 5.0。
- [ ] 现有可视化能显示 offensive striker 的射门距离和最终力度。
- [ ] 未改变射门触发时机、方向、目标、绕球、对齐或角色状态。
- [ ] 仅提供手动验证建议，不运行任何自动验证命令。
