"""对端协助机构接口的最小客户端。

接口约定见《交换资料约定.md》：

* ``GET /assists/{reference}`` 返回 ref、call、group、action 与 state；
* ``POST /assists/{reference}/cancel`` 请求取消，回话仍由原动作占用时表示不能复用。

客户端只负责传输与响应解析，不推断“取消成功”。是否释放由调用方按 state 判断。
"""

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass

from .feeds import ACTIONS, ASSIST_STATES


class PeerUnavailable(Exception):
    """对端没有给出可用答复：超时、断开、非 JSON 或不认识的回话。

    ``reason`` 用于向船代说明“仍在等哪个机构决定”。
    """

    def __init__(self, reference, reason):
        self.reference = reference
        self.reason = reason
        super().__init__(f"{reference}: {reason}")


@dataclass(frozen=True)
class AssistReply:
    ref: str
    call: str
    group: str
    action: str
    state: str

    @property
    def holds_group(self):
        """该回话是否代表值班组仍被这个动作占用。"""
        return self.state in ("dispatched", "on_board")

    @property
    def finished(self):
        return self.state in ("completed", "released")

    @classmethod
    def from_payload(cls, payload, reference):
        if not isinstance(payload, dict):
            raise PeerUnavailable(reference, "回话不是对象")
        try:
            reply = cls(
                ref=payload["ref"],
                call=payload["call"],
                group=payload["group"],
                action=payload["action"],
                state=payload["state"],
            )
        except KeyError as error:
            raise PeerUnavailable(reference, f"回话缺少 {error.args[0]}") from error
        if reply.action not in ACTIONS:
            raise PeerUnavailable(reference, f"回话的 action 不认识：{reply.action!r}")
        if reply.state not in ASSIST_STATES:
            raise PeerUnavailable(reference, f"回话的 state 不认识：{reply.state!r}")
        return reply


class PeerClient:
    def __init__(self, base_url, timeout=3.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method, path, body=None, reference=""):
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as error:
            raise PeerUnavailable(reference, f"未收到应答：{error}") from error
        try:
            payload = json.loads(raw)
        except ValueError as error:
            raise PeerUnavailable(reference, "应答不是有效 JSON") from error
        return AssistReply.from_payload(payload, reference)

    def fetch(self, reference):
        return self._request("GET", f"/assists/{reference}", reference=reference)

    def cancel(self, reference):
        # 录制里取消请求体为空对象，与接口约定一致。
        return self._request(
            "POST", f"/assists/{reference}/cancel", body={}, reference=reference
        )


class ScriptedPeer:
    """离线测试用的对端：按引用返回预置的回话序列。"""

    def __init__(self, script):
        # script: {reference: [AssistReply 或 PeerUnavailable, ...]}
        self._script = {key: list(value) for key, value in script.items()}

    def _next(self, reference):
        replies = self._script.get(reference)
        if not replies:
            raise PeerUnavailable(reference, "没有可用回话")
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, PeerUnavailable):
            raise reply
        return reply

    def fetch(self, reference):
        return self._next(reference)

    def cancel(self, reference):
        return self._next(reference)
