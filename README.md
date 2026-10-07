# 药师连续照护协议

本项目提供药师连续照护协议的服务端基础：患者同意范围、药师执业与门店授权、药物清单版本、服务目标、回访事件、异常升级、服务额度与跨店交接。系统只登记、核对与冻结信息，不做诊断，也不修改医嘱——药物清单只能按处方来源以新版本追加，历史版本与历史动作全部保留。

## 目录

- `src/pharmacy_care/domain.py` 领域常量（协议/同意/回访/异常/额度状态、角色）与业务错误。
- `src/pharmacy_care/store.py` SQLite 表结构与事务写入。
- `src/pharmacy_care/service.py` 连续照护应用服务。
- `src/pharmacy_care/api.py` 进程内 JSON 请求边界。
- `tests/` 覆盖当前已有行为。

## 核心业务规则

- **同意与冻结**：`withdraw_consent` 冻结协议及所有未执行回访；`add_medication_list` 追加新版本时冻结依赖旧版本的未来回访。历史记录不删除。
- **资格校验**：回访只能由当时具备资格（角色为药师、在执业有效期内、授权于协议当前门店）的药师签署；销售人员不能关闭安全异常。
- **幂等与复核**：回访安排、离线补录、支付回调按业务键去重；同键不同内容进入 `reviews` 复核队列，原记录不覆盖。
- **额度事务**：预占（`hold_quota`）、完成（`complete_followup` 同事务结算）、退回（`release_quota`）在同一事务语义下；额度不足时签署整体回滚。
- **跨店交接**：`transfer_store` 在同一事务内释放原门店全部有效预占并变更归属门店，此后原门店人员不能再消费额度。
- **分角色视图**：`patient_view`（计划与同意）、`pharmacist_view`（另含执行依据与未解决风险）、`compliance_view`（另含费用去向、流水与复核）。
- **重启恢复**：`recover` 返回逾期回访与未关闭的升级队列，数据持久化于 SQLite，重启后可用。

## 运行

运行测试：`python3 -m pytest -q`

检查源码：`python3 -m compileall src`

本地冒烟：`printf '%s' '{"action":"health"}' | PYTHONPATH=src python3 -m pharmacy_care.cli`

项目只使用 Python 标准库，运行期间不连接其他服务。
