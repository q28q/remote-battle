"""
UDP 网络层 — 低可靠/高可靠传输 + 后台线程

设计：
  - 单 UDP socket，后台 daemon 线程处理所有 I/O
  - 主线程通过 send()/recv_all() 与网络线程通过 queue.Queue 交互（线程安全）
  - 低可靠通道（reliable=False）：即发即走，短窗口内最多额外重传 2 次
  - 高可靠通道（reliable=True）：序列号 + ACK + 持续重试，保证有序投递

包格式 (5 字节头部 + JSON 载荷)：
  [0] flags: bit0=RELIABLE  bit1=IS_ACK
  [1-2] seq: 序列号 (uint16 LE)
  [3-4] ack: 预留（暂未使用）
  [5..]  payload: UTF-8 JSON
"""

import socket
import json
import struct
import threading
import queue
import time
import random

_clock = time.monotonic

# ── 协议常量 ──
HEADER_SIZE = 5
FLAG_RELIABLE = 1 << 0
FLAG_ACK = 1 << 1

# 低可靠重传参数
LOW_RETRY_INTERVAL = 0.010    # 10ms
LOW_MAX_RETRIES = 2           # 最多额外重传 2 次（共 3 次发送机会）

# 高可靠重传参数
HIGH_RETRY_INTERVAL = 0.050   # 50ms


def _make_header(flags: int, seq: int, _ack: int = 0) -> bytes:
    return struct.pack('<BHH', flags, seq & 0xFFFF, _ack & 0xFFFF)


def _seq_gt(a: int, b: int) -> bool:
    """序列号 a 是否在 b 之后（环形 16-bit 空间）"""
    return ((a - b) & 0xFFFF) < 32768


class _LowSlot:
    """低可靠待重传条目"""
    __slots__ = ('data', 'addr', 'last_send', 'retries')

    def __init__(self, data: bytes, addr: tuple, now: float):
        self.data = data
        self.addr = addr
        self.last_send = now
        self.retries = 0


class UdpNode:
    """
    UDP 网络节点

    后台线程处理所有 socket I/O，主线程通过 send() / recv_all() 与它交互。

    使用例：
        node = UdpNode(port=12345, broadcast=True)   # 服务端
        node = UdpNode()                               # 客户端（随机端口）
        node.send(addr, {"type": "ping"}, reliable=False)
        node.send(addr, {"type": "join"}, reliable=True)
        for a, m in node.recv_all():
            print(a, m)
        node.close()
    """

    def __init__(self, port: int = 0, broadcast: bool = False):
        self.port = port

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if broadcast:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind(("0.0.0.0", port))
        self.sock.setblocking(False)

        self._running = True

        # ── 线程安全队列（主线程 ↔ 网络线程） ──
        self._send_queue: queue.Queue = queue.Queue()
        self._recv_queue: queue.Queue = queue.Queue()
        self._drop_queue: queue.Queue = queue.Queue()  # (addr, key) 丢弃指令

        # ── 发送端状态（仅在网络线程中访问） ──
        self._next_seq = random.randint(0, 65535)
        self._pending_low: dict[str, _LowSlot] = {}
        self._pending_high: dict[int, bytes] = {}
        self._pending_high_addr: dict[int, tuple] = {}
        self._pending_high_time: dict[int, float] = {}

        # ── 接收端状态（仅在网络线程中访问） ──
        self._recv_expected: dict[str, int] = {}
        self._recv_buffer: dict[str, dict[int, tuple]] = {}

        # ── 启动后台线程 ──
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ═══════════════════════════════════════════════════════════
    #  公开 API（主线程安全）
    # ═══════════════════════════════════════════════════════════

    def send(self, addr: tuple, data: dict, reliable: bool = False):
        """向 addr 发送一条 JSON 消息"""
        try:
            payload = json.dumps(data, ensure_ascii=False,
                                 default=str).encode("utf-8")
        except Exception:
            return

        if reliable:
            seq = self._next_seq
            self._next_seq = (seq + 1) & 0xFFFF
            header = _make_header(FLAG_RELIABLE, seq)
            self._send_queue.put((addr, header + payload, True))
        else:
            header = _make_header(0, 0)
            self._send_queue.put((addr, header + payload, False))

    def send_raw(self, addr: tuple, payload: bytes):
        """发送裸字节（低可靠，用于无需序列化的场景）"""
        self._send_queue.put((addr, payload, False))

    def drop_peer(self, addr: tuple):
        """丢弃该地址的所有待重传消息（主线程安全）"""
        key = f"{addr[0]}:{addr[1]}"
        self._drop_queue.put((addr, key))

    def recv_all(self) -> list[tuple[tuple, dict]]:
        """非阻塞获取所有已收到的消息"""
        msgs = []
        while not self._recv_queue.empty():
            try:
                msgs.append(self._recv_queue.get_nowait())
            except queue.Empty:
                break
        return msgs

    def close(self):
        """关闭节点"""
        self._running = False
        try:
            self.sock.close()
        except OSError:
            pass

    # ═══════════════════════════════════════════════════════════
    #  后台线程
    # ═══════════════════════════════════════════════════════════

    def _run(self):
        while self._running:
            now = _clock()

            # 0. 处理丢弃指令
            self._drain_drop_queue()
            # 1. 发送待发队列
            self._drain_send_queue(now)
            # 2. 接收
            self._recv_all()
            # 3. 低可靠重传
            self._retransmit_low(now)
            # 4. 高可靠重传
            self._retransmit_high(now)

            time.sleep(0.0005)

    def _drain_drop_queue(self):
        while True:
            try:
                addr, key = self._drop_queue.get_nowait()
            except queue.Empty:
                break
            # 清除低可靠待重传
            self._pending_low.pop(key, None)
            # 清除高可靠待重传
            for seq, a in list(self._pending_high_addr.items()):
                if a == addr:
                    self._pending_high.pop(seq, None)
                    self._pending_high_addr.pop(seq, None)
                    self._pending_high_time.pop(seq, None)
            # 清除接收端状态
            self._recv_expected.pop(key, None)
            self._recv_buffer.pop(key, None)

    def _drain_send_queue(self, now: float):
        while True:
            try:
                addr, raw, is_reliable = self._send_queue.get_nowait()
            except queue.Empty:
                break

            if is_reliable:
                seq = struct.unpack('<H', raw[1:3])[0]
                self._pending_high[seq] = raw
                self._pending_high_addr[seq] = addr
                self._pending_high_time[seq] = now
            else:
                key = f"{addr[0]}:{addr[1]}"
                self._pending_low[key] = _LowSlot(raw, addr, now)

            try:
                self.sock.sendto(raw, addr)
            except OSError:
                pass

    def _recv_all(self):
        while True:
            try:
                raw, addr = self.sock.recvfrom(4096)
            except BlockingIOError:
                break
            except OSError:
                break
            self._on_recv(raw, addr)

    def _on_recv(self, raw: bytes, addr: tuple):
        if len(raw) < HEADER_SIZE:
            return

        flags, seq, _ = struct.unpack('<BHH', raw[:HEADER_SIZE])
        payload = raw[HEADER_SIZE:]
        key = f"{addr[0]}:{addr[1]}"

        # ── ACK 包 ──
        if flags & FLAG_ACK:
            if seq in self._pending_high:
                del self._pending_high[seq]
                self._pending_high_addr.pop(seq, None)
                self._pending_high_time.pop(seq, None)
            return

        # ── 高可靠数据包 ──
        if flags & FLAG_RELIABLE:
            # 立即回复 ACK
            try:
                ack_raw = _make_header(FLAG_ACK | FLAG_RELIABLE, seq)
                self.sock.sendto(ack_raw, addr)
            except OSError:
                pass

            if not payload:
                return
            try:
                msg = json.loads(payload.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return

            # 有序投递
            expected = self._recv_expected.get(key, -1)
            if expected == -1:
                # 第一个收到的可靠包
                self._recv_expected[key] = seq
                self._recv_queue.put((addr, msg))
                self._deliver_buffered(key)
            elif seq == expected:
                self._recv_queue.put((addr, msg))
                self._recv_expected[key] = (expected + 1) & 0xFFFF
                self._deliver_buffered(key)
            elif _seq_gt(seq, expected):
                # 乱序到达，入缓冲
                buf = self._recv_buffer.setdefault(key, {})
                if len(buf) < 64:
                    buf[seq] = (addr, msg)
            # seq < expected → 已处理过的重复包，丢弃

        # ── 低可靠数据包 ──
        else:
            if not payload:
                return
            try:
                msg = json.loads(payload.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            self._recv_queue.put((addr, msg))

    def _deliver_buffered(self, key: str):
        """递送缓冲区中后续按序到达的包"""
        buf = self._recv_buffer.get(key, {})
        expected = self._recv_expected.get(key, -1)
        if expected == -1:
            return
        while True:
            nxt = (expected + 1) & 0xFFFF
            if nxt in buf:
                a, m = buf.pop(nxt)
                self._recv_queue.put((a, m))
                expected = nxt
                self._recv_expected[key] = expected
            else:
                break

    def _retransmit_low(self, now: float):
        expired = []
        for key, slot in self._pending_low.items():
            if slot.retries >= LOW_MAX_RETRIES:
                expired.append(key)
                continue
            if now - slot.last_send >= LOW_RETRY_INTERVAL:
                try:
                    self.sock.sendto(slot.data, slot.addr)
                except OSError:
                    expired.append(key)
                    continue
                slot.retries += 1
                slot.last_send = now
        for key in expired:
            self._pending_low.pop(key, None)

    def _retransmit_high(self, now: float):
        for seq in list(self._pending_high.keys()):
            last_send = self._pending_high_time.get(seq, 0)
            if now - last_send >= HIGH_RETRY_INTERVAL:
                addr = self._pending_high_addr.get(seq)
                data = self._pending_high.get(seq)
                if addr is not None and data is not None:
                    try:
                        self.sock.sendto(data, addr)
                    except OSError:
                        # 对方不可达，停止重试
                        del self._pending_high[seq]
                        self._pending_high_addr.pop(seq, None)
                        self._pending_high_time.pop(seq, None)
                        continue
                    self._pending_high_time[seq] = now
