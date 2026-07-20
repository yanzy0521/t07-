# 进攻小力带球减少后撤

## Goal

优化进攻持球/追球时过度后撤问题，探索小力度带球以保持出脚速度；不得修改防守参数和防守逻辑。

## Requirements

- 检查进攻持球、追球、绕球和射门准备逻辑，定位机器人带球时频繁后撤导致失去出脚速度优势的原因。
- 只允许修改进攻 striker / 进攻搭档相关参数或逻辑；不得修改防守 presser、defensive clear、守门员、防守保护站位相关参数和逻辑。
- 当机器人已经具备较好的前进触球方向时，应避免为了完美球后位反复后撤。
- 在合适场景下允许使用小力度向前带球/推球，保持球向对方球门方向移动，并让机器人维持出脚速度。
- 小力带球不得替代明确射门机会；接近对方球门且角度合适时仍应正常射门。
- 改动应局部、可读、易调参，并保持已有防守参数分离结构。

## Acceptance Criteria

- [x] 已定位进攻后撤主要来自哪个追球/绕球/射门准备分支。
- [x] 新增或调整的参数明确属于进攻命名空间，不复用、不改动防守参数。
- [x] 进攻机器人在合适情况下会小力向前带球，减少不必要后撤。
- [x] 防守 presser、defensive clear、守门员相关参数和逻辑未被修改。
- [x] 按项目规则不运行自动验证，由用户手动观察实战效果。

## Notes

- Keep `prd.md` focused on requirements, constraints, and acceptance criteria.
- Lightweight tasks can remain PRD-only.
- For complex tasks, add `design.md` for technical design and `implement.md` for execution planning before `task.py start`.
