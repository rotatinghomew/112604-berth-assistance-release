"""用合成资料演示一次停靠的排期、确认与窗口交接。

不连接对端时自动生成“全部答复”的离线回话，便于在本地观察服务输出：

    python3 demo.py

加 ``--peer`` 会按项目里的录制启动本地重放，但只对录制中出现的引用有效，
其余引用会显示为等待机构回话。
"""

import argparse
import json

from harbor import HarborService, load
from harbor.peer import AssistReply, PeerClient, PeerUnavailable


class ConfirmingPeer:
    """离线演示用：对任何引用都回“可以”，撤销都回“已释放”。"""

    def fetch(self, ref):
        call, action, group = _split(ref)
        return AssistReply(ref=ref, call=call, group=group, action=action, state="confirmed")

    def cancel(self, ref):
        call, action, group = _split(ref)
        return AssistReply(ref=ref, call=call, group=group, action=action, state="released")


def _split(ref):
    head = ref.split("-")[0]
    call = ref.split("-")[1]
    action = "departure" if ref.endswith("-depart") else "approach"
    return call, action, head


def show(title, payload):
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", default="samples", help="交换资料目录")
    parser.add_argument("--peer", metavar="URL", help="对端地址；省略则使用离线回话")
    options = parser.parse_args()

    feeds = load(options.samples)
    peer = PeerClient(options.peer) if options.peer else ConfirmingPeer()
    service = HarborService(feeds, peer=peer)

    result = service.register_forecast(
        "Q43",
        {
            "beam_m": 24,
            "draft_dm": 74,
            "eta": "2026-11-27T01:30:00Z",
            "work_minutes": 55,
            "latest_exit": "2026-11-27T05:20:00Z",
            "assist_required": ["pilot", "tug"],
        },
    )
    plan_id = result.plan["id"]
    show("登记预报后的候选", result.to_json())

    confirmed = service.confirm(plan_id)
    show("向协助机构确认", confirmed.to_json())
    if isinstance(peer, PeerClient) and not confirmed.ok:
        return

    show(
        "码头按航次查询",
        service.call_report("Q43"),
    )

    after_arrival = service.record_quay_event(
        {
            "event": "DEMO-1",
            "call": "Q43",
            "berth": result.plan["berth"],
            "kind": "all_fast",
            "at": "2026-11-27T02:05:00Z",
        }
    )
    show("实际系缆后：可开始装车与离泊窗口", after_arrival["berthing"])


if __name__ == "__main__":
    main()
