# 放宽进攻射门条件与远距离调整 - Design

## Scope

本任务只处理普通进攻 striker 的射门触发阈值。防守逼抢、后场 defensive clear、守门员、保护站位、开球和定位球不在范围内。

## Technical Approach

1. 保留现有动态射门目标：正球瞄门、稍偏向场内调整、极偏快速处理。
2. 将射门对齐阈值改为随球场位置动态变化：
   - 近/中距离：放宽对齐角度，提高大脚射门触发频率。
   - 远距离/后场：保持更严格对齐，避免远距离不正时直接踢出界。
3. 仅增加进攻命名空间参数和进攻 helper，不修改防守函数。

## Non-Goals

- 不修改 `DEFENSIVE_PRESSER_*`。
- 不修改 `plan_defensive_clear()`。
- 不修改 `GOALKEEPER_*`。
- 不改固定战术。

## Rollback Shape

若射门过于随意，可回退新增的动态对齐阈值 helper 和对应 `OFFENSIVE_STRIKER_*` 参数。
