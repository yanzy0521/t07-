# 进攻射门动态调整偏球 - Design

## Scope

本任务只处理普通进攻 striker 的射门前目标选择和稍偏球调整。防守逼抢、后场 defensive clear、守门员、保护站位、开球和定位球不在范围内。

## Technical Approach

1. 复查 `Player.attack()`、`plan_offensive_shot()`、小力带球分支和进攻参数。
2. 将进攻球位按横向偏移分成三档：
   - 正常通道：直接瞄准对方球门。
   - 稍偏通道：目标向场内收一点，先把球调整回更安全的射门通道。
   - 极偏通道：靠近对方角球区，不强行调整，保持快速处理。
3. 稍偏调整只改变进攻射门/小力带球的目标方向，不修改底层踢球接口。
4. 所有新增阈值放在 `OFFENSIVE_STRIKER_*` 参数区。

## Non-Goals

- 不修改 `DEFENSIVE_PRESSER_*`。
- 不修改 `plan_defensive_clear()`。
- 不修改 `GOALKEEPER_*`。
- 不改固定战术。

## Rollback Shape

若动态调整效果不好，可回退新增进攻参数和 `Player` 中射门目标选择逻辑；防守逻辑无需回退。
