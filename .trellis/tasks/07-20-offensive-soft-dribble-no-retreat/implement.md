# 进攻小力带球减少后撤 - Implementation Plan

## Checklist

1. 阅读相关规范和任务需求。
2. 定位 `Player.attack()`、进攻参数和普通比赛进攻调用点。
3. 确认防守参数已经与进攻参数分离，并避免触碰防守命名空间。
4. 新增进攻专用小力带球参数。
5. 在进攻逻辑中增加减少后撤的小力带球分支。
6. 人工检查 diff，确认未修改防守 presser、defensive clear、守门员逻辑。
7. 按项目规则不运行 build/test/lint/type-check/format/仿真/IDE diagnostics。

## Manual Verification Suggestions

- 观察普通进攻 striker 近球但角度不完美时，是否减少后撤绕球。
- 观察机器人是否能用小力度把球持续向对方球门方向推，而不是停球后退。
- 观察明确射门机会下是否仍会正常大力射门。
- 观察防守逼抢和守门员解围行为是否没有变化。
