# TESTING.md — 最少剔除试算

本地无 Docker/MySQL，验证用 SQLite 文件库 + fakeredis 跑 FastAPI 测试客户端；
算法另有独立暴力枚举 oracle。生产用 MySQL 8 与 Redis 7，行为一致（行锁在
SQLite 下退化为无操作，MySQL 下为 `SELECT ... FOR UPDATE`）。

## 环境

```bash
python3 -m pip install --user --break-system-packages \
  numpy scipy pytest httpx fastapi==0.115.6 sqlalchemy==2.0.36 \
  pyjwt==2.10.1 redis==5.2.1 fakeredis==2.26.2
cd backend && PYTHONPATH=. python3 -m pytest tests -q
```

结果：`83 passed`。

## 覆盖点

| 需求 | 验证 |
|---|---|
| 保留样本内孔隙率更高者吸水率不得更低 | `test_higher_porosity_never_lower_absorption_rule`、`test_ties_are_not_inversions`、`test_all_tied_rows_are_retained` |
| 最少剔除且 5000 件不逐对比较 | `test_matches_bruteforce`（60 组随机小样本对暴力枚举：保留数、必剔除集合、合法性全等）、`test_large_input_is_fast`（5000 件 <5s，实测 0.03s；Fenwick O(n log n)） |
| 必留项矛盾时指出哪两件 | `test_forced_conflict_names_both_specimens`、`test_preview_conflict_names_the_pair`、`test_conflict_cannot_be_confirmed` |
| 所有最优解都得剔除的试件 | `brute_must` oracle 比对；`test_preview_reports_optimum_and_must_exclude` |
| 改测值后旧方案不生效 | `test_confirmation_with_stale_basis_is_rejected`、`test_delete_and_recreate_invalidates_old_basis`（删除后重加也失效，因依据含观测 id） |
| 改必留项后旧方案不生效 | `test_confirmation_with_changed_forced_keys_is_rejected` |
| 确认后冻结排队、结果用冻结样本 | `test_retry_reuses_same_trial_and_freezes_basis`、`test_result_uses_frozen_basis_after_later_edits`（排队期间改来源，结果仍按冻结快照） |
| 中断可恢复、重试不重复建 | `test_recovery_requeues_without_duplicating`（清空 Redis 队列后 `recover()` 重投同一 trial；重复投递处理为 no-op；表内仅 1 行） |
| 失败态保留错误 | `test_failed_trial_keeps_error_and_is_not_retried` |
| 权限 | `test_viewer_cannot_confirm_but_can_preview` |

## 已知边界

- 单一 worker；`recover()` 在启动时及每 30s 扫描 `queued/running`，Redis 丢失
  任务可由数据库补偿，但极端并发下的重复投递依赖处理端的幂等跳过（已测）。
- 前端只做 `node --check` 与离线渲染冒烟（列表页/结果页/矛盾页），未做浏览器端
  自动化；真实点击链路需 `docker compose up` 后人工走一遍。
- 业务口径解释：剔除只服务于后续复测/预算假设，不构成基于 p 值挑样本的依据；
  结果页与算法说明沿用现有免责声明。
