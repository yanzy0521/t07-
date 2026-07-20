# 实施计划

1. 在 `src/param.py` 添加 offensive close-contest disrupt 的距离和低力度常量。
2. 在 `src/player.py` 添加最近对手到球距离 helper。
3. 在 `Player.attack()` 原 `_kicking` 高质量射门判断之后、后撤/绕球走位之前，加入 close-contest disrupt 分支。
4. 静态复核：确认 defensive presser 未变、常规 offensive 已对齐时仍优先高质量射门、未修改禁止范围参数。

## 验证约束

本窗口不运行 build、test、lint、type-check、format、仿真、部署、开发服务器或 IDE diagnostics。用户后续手动仿真应观察 offensive striker 在 opponent 距球较近时是否从 `direct/circle` 改为快速出脚。
