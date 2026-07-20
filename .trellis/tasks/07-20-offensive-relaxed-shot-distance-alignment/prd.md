# 放宽进攻射门条件与远距离调整

## Goal

进攻更容易触发大脚射门；远距离球路不正时先调整；不修改防守逻辑。

## Requirements

- 检查当前进攻射门触发条件，重点是距离、球后对齐角度、小力带球和动态射门目标之间的关系。
- 只允许修改进攻 striker 相关参数或逻辑；不得修改防守 presser、defensive clear、守门员和防守保护逻辑。
- 适当放宽近/中距离射门触发条件，让机器人在具备出脚机会时更常大脚射门。
- 远距离射门仍需保持方向质量：如果球路不正，应先按动态目标或球后位做一定调整，避免远距离盲目踢偏出界。
- 新增阈值必须属于 `OFFENSIVE_STRIKER_*` 命名空间，保持进攻/防守参数分离。

## Acceptance Criteria

- [x] 已定位当前射门触发过少的关键条件。
- [x] 近/中距离射门对齐角度适当放宽，更容易触发大脚。
- [x] 远距离射门如果不正仍会先调整，不因放宽条件而远距离盲踢。
- [x] 新增或调整参数均为 `OFFENSIVE_STRIKER_*`，未修改防守参数。
- [x] 防守 presser、defensive clear、守门员相关逻辑未被修改。
- [x] 按项目规则不运行自动验证，由用户手动观察实战效果。

## Notes

- Keep `prd.md` focused on requirements, constraints, and acceptance criteria.
- Lightweight tasks can remain PRD-only.
- For complex tasks, add `design.md` for technical design and `implement.md` for execution planning before `task.py start`.
