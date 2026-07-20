# 进攻射门动态调整偏球

## Goal

进攻射门按球位偏正动态处理：正球直接踢，稍偏先调整，极偏靠角球区允许直接踢出界；不修改防守逻辑。

## Requirements

- 检查当前进攻射门方向、球后位对齐、小力带球和出界风险相关逻辑。
- 只允许修改进攻 striker 相关参数或逻辑；不得修改防守 presser、defensive clear、守门员和防守保护逻辑。
- 当球位相对球门比较正时，保持现有直接射门/向前处理，避免过度调整拖慢出脚。
- 当球位稍微偏向边线且直接射门容易向前出界时，应先加入适度调整，让球向场内或更接近球门方向移动。
- 当球位非常偏、已经接近对方角球区时，不强行调整，允许直接向前处理或踢出界，因为进球概率很低。
- 新增阈值必须属于进攻命名空间，便于现场调参，并保持防守参数分离。

## Acceptance Criteria

- [x] 已定位当前偏球直接向前踢出界的进攻分支。
- [x] 新增/调整参数使用 `OFFENSIVE_STRIKER_*` 命名，不复用也不修改防守参数。
- [x] 稍微偏的球会优先获得场内调整目标，而不是直接向边线外踢。
- [x] 很正的球仍能快速直接射门。
- [x] 极偏靠对方角球区的球不会被强行长时间调整。
- [x] 防守 presser、defensive clear、守门员相关逻辑未被修改。
- [x] 按项目规则不运行自动验证，由用户手动观察实战效果。

## Notes

- Keep `prd.md` focused on requirements, constraints, and acceptance criteria.
- Lightweight tasks can remain PRD-only.
- For complex tasks, add `design.md` for technical design and `implement.md` for execution planning before `task.py start`.
