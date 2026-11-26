# 汽车船靠离泊协作与作业窗口交接

汽车船靠离泊两动作、泊位事实与外部协助组交接。

数据均为合成，`samples/` 保存原始样本和字段约定，《交换资料约定》说明同一对象的编号、时间及来源。`harbor/` 提供港调排期服务，把船代预报、码头窗口、协助机构回话和现场回执串成一次停靠的进出港安排。

## 运行环境

使用 Python 3.11.2。资料检查与服务测试只用标准库，无需下载安装包。由项目根目录运行：

```sh
python3 -m unittest discover -s tests -v
```

测试覆盖两部分：

* `tests/test_feeds.py` 检查合成资料能否解析、字段类型与引用是否对应；
* `tests/test_peer.py` 通过回环 HTTP 检查报文重放；
* `tests/test_service.py` 检查排期、确认、撤回、改期、实际回执与查询。

## 服务用法

```python
from harbor import HarborService, load
from harbor.peer import PeerClient

feeds = load("samples")
service = HarborService(feeds, peer=PeerClient("http://127.0.0.1:8087"))

# 船代登记预报；服务按 ETA、船宽、吃水自动挑选可行泊位与候选窗口。
result = service.register_forecast("Q43", {
    "beam_m": 24, "draft_dm": 74,
    "eta": "2026-11-27T01:30:00Z",
    "work_minutes": 55,
    "latest_exit": "2026-11-27T05:20:00Z",
    "assist_required": ["pilot", "tug"],
})
plan_id = result.plan["id"]

# 港调确认；每个动作的每个协助组都要有明确回话，否则方案保持候选。
service.confirm(plan_id)

# 码头登记实际回执；泊位与协助组各自释放。
service.record_quay_event({
    "event": "E-1", "call": "Q43", "berth": "B5",
    "kind": "all_fast", "at": "2026-11-27T02:05:00Z",
})
```

### 操作

| 角色 | 方法 | 说明 |
| --- | --- | --- |
| 船代 | `register_forecast` / `update_forecast` | 登记或更新到达预报；更新会让已确认方案退回候选 |
| 船代 | `accept_plan` | 接受候选；仍需港调向机构确认 |
| 港调 | `propose` | 按最新资料重新计算候选 |
| 港调 | `propose_manual` | 人工指定靠/离时段，仍受窗口、占用与作业时长约束 |
| 港调 | `confirm` | 取得所有协助组明确回话后才确认 |
| 港调 | `cancel` | 撤回；未出航的等原机构释放，已出航/已登轮的等对方结束 |
| 港调 | `reschedule` | 改期；靠、离两个动作都兑现才切换 |
| 港调 | `record_inquiry` | 登记向机构或码头的咨询 |
| 码头 | `record_quay_event` | 登记 `all_fast` / `lines_clear` 实际回执 |
| 码头 | `record_shift_update` | 登记窗口修订与预计腾出（不替代实际回执） |
| 码头 | `record_clear_confirmation` | 确认前船腾让，解除软件占用 |
| 码头 | `set_window_revision` | 发布压缩或新增的泊位窗口修订 |
| 协助机构 | `apply_peer_reply` / `poll` | 写入或重问明确回话 |
| 查询 | `query(call=, berth=, group=)` | 按航次、泊位或值班组查看 |

### 关键判断

* **候选与确认分开。** 机构没有明确答复的方案只保留 `state="candidate"`，`pending` / `pending_decisions()` 指出仍在决定的机构；未回话的预约不会因为本地计算通过就当作确认。
* **两套事实。** 泊位位置由 `all_fast` / `lines_clear` 推进；协助组由对端 `released` / `completed` 释放。`dispatched` / `on_board` 的预约只有对方结束或同意继续服务新窗口才可复用，不能按改写计划当作空闲。
* **值班组独占。** 同一组同一时段只能承诺一个动作；不同窗口可以复用。`confirm` 会检查本项目已有的重叠承诺。
* **窗口与作业时长。** 靠离动作都要完整落在泊位窗口内（左闭右开），船宽与吃水适配；靠妥后必须留出完整 `work_minutes` 才能离泊，且不晚于 `latest_exit`。
* **改期。** 只有新靠、新离都取得明确协助回话才确认；否则新方案留候选，`kept` 列出仍有效的旧承诺，`released` 列出已释放的旧承诺。
* **已系泊的船不是空泊位。** 码头查询给出 `berthing`（实际靠妥、可开始装车时间、是否已无合适离泊窗口）与 `loading_point`（下一次可交给装车组的时间）。没有 `lines_clear` 或码头确认让泊位前，别的船不能占用。
* **资料变化。** 实际回执或窗口修订会让相关已确认方案 `stale=True`，由港调决定重新确认还是改期。

## 目录

```
harbor/
  timeutil.py   时间与左闭右开区间运算
  feeds.py      交换资料读取与校验
  model.py      项目状态、方案、承诺与实际回执
  facts.py      从实际回执推导泊位占用
  schedule.py   候选窗口计算
  peer.py       对端 HTTP 客户端与离线回话
  service.py    协作服务
```

## 本地对端

运行 `python3 peer_replay.py` 可在 `127.0.0.1:8087` 重放 `samples/peer.json` 中的 HTTP 录制。`--recording` 可读取另一份同格式录制，`--port` 可更改端口。每条录制依次写请求方法、路径、JSON 请求体和响应序列。没有请求体时写 null。每种请求独立推进响应，最后一项可重复读取，状态码 0 代表连接断开且未收到应答。重启重放进程会重新开始读取录制，不代表现实中的远端状态被重置。

服务使用对端引用 `<组>-<航次>` 与 `<组>-<航次>-depart` 查询协助，与录制一致；本项目内部仍按“方案/动作/组”分别保存承诺，便于审计。
