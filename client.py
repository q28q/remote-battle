"""
LAN Platform Battle — 游戏客户端 (TCP)
pygame 渲染 + TCP 连接
2D 平台跳跃 + 近战/枪械
"""
import pygame
import socket
import json
import time
import math
import sys
import random

# 单调时钟
_clock = time.monotonic

sys.path.insert(0, ".")
from config import *


# ── TCP 收发工具 ──

def _send_msg(sock: socket.socket, data: dict):
    """发送长度前缀 JSON 消息"""
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    try:
        sock.sendall(len(payload).to_bytes(4, 'big') + payload)
    except OSError:
        pass


def _recv_msg(sock: socket.socket, buf: bytearray) -> dict | None:
    """从缓冲区提取一条 JSON 消息，先用 sock.recv 尝试读更多"""
    try:
        chunk = sock.recv(4096)
        if chunk:
            buf.extend(chunk)
        else:
            return None  # 断开
    except socket.timeout:
        pass  # 无新数据
    except OSError:
        return None

    if len(buf) < 4:
        return None
    n = int.from_bytes(buf[:4], 'big')
    if len(buf) < 4 + n:
        return None
    body = buf[4:4 + n]
    buf[:] = buf[4 + n:]
    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


# ── 中文字体 ──

_FONT_CACHE: dict[int, pygame.font.Font] = {}

def _get_font(size: int) -> pygame.font.Font:
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    paths = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/msyh.ttf",
        "C:/Windows/Fonts/msyhbd.ttf",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "C:/Windows/Fonts/simsun.ttf",
        "C:/Windows/Fonts/yahei.ttf",
        "C:/Windows/Fonts/yaheibd.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    ]
    for p in paths:
        try:
            f = pygame.font.Font(p, size)
            _FONT_CACHE[size] = f
            return f
        except (FileNotFoundError, pygame.error):
            continue
    f = pygame.font.Font(None, size)
    _FONT_CACHE[size] = f
    return f


# ── 客户端 ──

class GameClient:

    def __init__(self):
        self.server_addr: tuple[str, int] | None = None
        self.pid: int | None = None
        self.color: list[int] = [255, 255, 255]
        self.server_tick: int = TICK_RATE
        self.players: dict[int, dict] = {}
        self.bullets: list[dict] = []
        self.kill_msgs: list[tuple[str, float]] = []
        self.running = False
        self.connected = False

        self.keys = {"left": False, "right": False, "jump": False}
        self.gun_pressed = False
        self.melee_pressed = False
        self.weapon = 0
        self.facing = 1

        self.sock: socket.socket | None = None
        self.prev_hp: dict[int, int] = {}
        self.hit_flash: dict[int, float] = {}
        self._got_first_state = False
        self._last_state_time = 0.0
        self._recv_buf = bytearray()
        self._melee_anim: dict[int, float] = {}  # pid -> start_time
        self._shake = 0.0  # 屏幕震动强度
        self._particles: list[dict] = []  # 粒子效果
        self._prev_vy: dict[int, float] = {}  # 落地检测
        self._head_x: dict[int, float] = {}  # 头部惯性追踪
        self._size_scale: dict[int, float] = {}  # 巨大化缩放
        self._heal_flash: dict[int, float] = {}  # 回血闪绿
        self._death_anim: dict[int, float] = {}  # 死亡动画
        self._dead_done: set[int] = set()  # 已完成死亡爆炸

    # ── LAN 发现（保留 UDP 单包交换）──

    def discover_server(self, timeout: float = 2.0) -> str | None:
        print("正在扫描局域网游戏服务器 ...")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(timeout)
        try:
            msg = json.dumps({"type": "discover"}).encode("utf-8")
            s.sendto(msg, ("255.255.255.255", SERVER_PORT))
            start = time.time()
            while time.time() - start < timeout:
                try:
                    data, addr = s.recvfrom(4096)
                    if json.loads(data).get("type") == "here":
                        return addr[0]
                except (socket.timeout, json.JSONDecodeError, OSError):
                    continue
        except OSError:
            pass
        finally:
            s.close()
        return None

    # ── TCP 连接 ──

    def connect(self, server_ip: str, port: int = SERVER_PORT,
                player_name: str = "") -> bool:
        self.server_addr = (server_ip, port)
        self._recv_buf = bytearray()
        self._melee_anim: dict[int, float] = {}  # pid -> start_time
        self._shake = 0.0  # 屏幕震动强度
        self._particles: list[dict] = []  # 粒子效果
        self._prev_vy: dict[int, float] = {}  # 落地检测
        self._head_x: dict[int, float] = {}  # 头部惯性追踪
        self._size_scale: dict[int, float] = {}  # 巨大化缩放
        self._heal_flash: dict[int, float] = {}  # 回血闪绿
        self._death_anim: dict[int, float] = {}  # 死亡动画
        self._dead_done: set[int] = set()  # 已完成死亡爆炸

        if not player_name:
            player_name = f"玩家{random.randint(10, 99)}"

        print(f"连接 {server_ip}:{port} ...")

        # 建立 TCP 连接
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(5.0)
        try:
            self.sock.connect(self.server_addr)
        except (ConnectionError, OSError, socket.timeout) as e:
            print(f"  连接失败: {e}")
            self.sock.close()
            self.sock = None
            return False

        # 发送 join
        self.sock.settimeout(5.0)
        _send_msg(self.sock, {"type": "join", "name": player_name})

        # 等待 welcome
        try:
            self.sock.settimeout(5.0)
            msg = _recv_msg(self.sock, self._recv_buf)
            if msg and msg.get("type") == "welcome":
                self.pid = msg["pid"]
                self.color = msg.get("color", [255, 255, 255])
                self.server_tick = msg.get("server_tick", TICK_RATE)
                self.connected = True
                print(f"  收到 welcome, ID={self.pid}")
            else:
                print(f"  未收到 welcome: {msg}")
                return False
        except (ConnectionError, OSError) as e:
            print(f"  接收 welcome 失败: {e}")
            return False

        # 等待第一个状态帧
        print("  等待初始状态 ...")
        _send_msg(self.sock, {
            "type": "input", "keys": {}, "weapon": 0,
            "attack": False, "facing": 1,
        })

        self.sock.settimeout(0.1)
        deadline = time.time() + 4.0
        while time.time() < deadline:
            msg = _recv_msg(self.sock, self._recv_buf)
            if msg and msg.get("type") == "state":
                self.handle_state(msg)
                if self.pid in self.players:
                    print(f"  初始状态就绪, 在线 {len(self.players)} 人")
                    self.sock.settimeout(None)
                    return True

        print("  初始状态未到, 继续游戏循环")
        self.sock.settimeout(None)
        return True

    def disconnect(self):
        self.connected = False
        if self.sock:
            # 通知服务器
            try:
                _send_msg(self.sock, {"type": "disconnect"})
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # ── 网络收发 ──

    def send_input(self):
        if not self.sock:
            return
        _send_msg(self.sock, {
            "type": "input",
            "keys": self.keys,
            "weapon": self.weapon,
            "attack": self.attack,
            "facing": self.facing,
        })

    def receive_state(self):
        if not self.sock:
            return
        self.sock.settimeout(0.001)
        while True:
            msg = _recv_msg(self.sock, self._recv_buf)
            if msg is None:
                break
            if msg.get("type") == "state":
                self.handle_state(msg)

    def handle_state(self, msg: dict):
        now = _clock()
        if not self._got_first_state:
            plist = msg.get("players", [])
            print(f"  [收到状态帧] 玩家={len(plist)} 子弹={len(msg.get('bullets',[]))}")
        for pd in msg.get("players", []):
            pid = pd["id"]
            old_hp = self.prev_hp.get(pid, pd["hp"])
            if pd["alive"] and old_hp > pd["hp"]:
                self.hit_flash[pid] = now
                color2 = pd.get("color", [255, 255, 255])
                self.spawn_particles(pd.get("x", 0) + 12, pd.get("y", 0) + 17, color2, 6)
                if pd["id"] == self.pid:
                    self.trigger_shake(6)
            # 死亡动画触发（仅一次）
            if not pd["alive"] and pd["id"] not in self._death_anim and pd["id"] not in self._dead_done:
                self._death_anim[pd["id"]] = now
                self.trigger_shake(8)
                c3 = pd.get("color", [255, 255, 255])
                self.spawn_particles(pd.get("x", 0) + 12, pd.get("y", 0) + 17, c3, 12)
            if pd["alive"]:
                self._death_anim.pop(pd["id"], None)
                self._dead_done.discard(pd["id"])
            # 回血闪绿 + 粒子
            if pd["alive"] and old_hp < pd["hp"]:
                self._heal_flash[pid] = now
                for _ in range(12):
                    a = random.uniform(0, 6.283)
                    sp = random.uniform(60, 180)
                    self._particles.append({
                        "x": pd.get("x", 0) + 12, "y": pd.get("y", 0) + 17,
                        "vx": math.cos(a) * sp, "vy": math.sin(a) * sp,
                        "life": random.uniform(0.3, 0.6),
                        "color": (80, 255, 80),
                    })
            # 二段跳检测
            prev_vy2 = self._prev_vy.get(pid, 0)
            cur_vy2 = pd.get("vy", 0)
            if pd.get("alive") and cur_vy2 < -200 and prev_vy2 > -50:
                for _ in range(6):
                    a = random.uniform(-3.14, 0)
                    sp = random.uniform(40, 100)
                    self._particles.append({
                        "x": pd.get("x", 0) + 12, "y": pd.get("y", 0) + PLAYER_H,
                        "vx": math.cos(a) * sp, "vy": math.sin(a) * sp,
                        "life": random.uniform(0.2, 0.4),
                        "color": (200, 200, 255),
                    })
            # 落地检测 + 行走粒子
            prev_vy = self._prev_vy.get(pid, 0)
            cur_vy = pd.get("vy", 0)
            cur_vx = pd.get("vx", 0)
            if abs(cur_vy) < 15 and abs(cur_vx) > 80 and random.random() < 0.3:
                dx = -12 if cur_vx > 0 else 12
                self.spawn_particles(pd.get("x", 0) + 12 + dx,
                                     pd.get("y", 0) + PLAYER_H,
                                     (160, 160, 180), 1)
            if prev_vy > 80 and cur_vy < 10:
                lx = pd.get("x", 0) + 12
                ly = pd.get("y", 0) + PLAYER_H
                self.spawn_particles(lx, ly, (180, 180, 200), 5)
            self._prev_vy[pid] = cur_vy
            self.prev_hp[pid] = pd["hp"]
            # 近战动画追踪
            if pd.get("melee_active") and pid not in self._melee_anim:
                self._melee_anim[pid] = now
            elif not pd.get("melee_active") and pid in self._melee_anim:
                del self._melee_anim[pid]

        # 爆裂弹追踪（按 owner_id 稳定匹配）
        cur_eb = {b["owner_id"]: b for b in self.bullets if b.get("explosive")}
        old_eb_ids = set(getattr(self, '_prev_eb_ids', set()))
        self.players = {p["id"]: p for p in msg.get("players", [])}
        # 巨大化缩放追踪
        for pl2 in self.players.values():
            target = 2.0 if "giant" in pl2.get("buffs", {}) else 1.0
            cur = self._size_scale.get(pl2["id"], 1.0)
            self._size_scale[pl2["id"]] = cur + (target - cur) * 0.15
        self.bullets = msg.get("bullets", [])
        self._state_items = msg.get("items", [])
        # 爆裂弹飞行爆炸（每帧爆炸 + 持续震动）
        for ob in cur_eb.values():
            for _ in range(25):
                a2 = random.uniform(0, 6.283)
                sp2 = random.uniform(60, 200)
                self._particles.append({
                    "x": ob["x"], "y": ob["y"],
                    "vx": math.cos(a2) * sp2, "vy": math.sin(a2) * sp2,
                    "life": random.uniform(0.2, 0.5),
                    "color": (255, random.randint(80, 200), 20),
                })
            self.trigger_shake(4)
        # 检测爆裂弹消失 → 大爆炸
        new_ids = set(cur_eb.keys())
        gone_ids = old_eb_ids - new_ids
        for gid in gone_ids:
            # 从 old_eb 取最后位置
            bx, by = 0, 0
            if hasattr(self, '_last_eb_pos') and gid in self._last_eb_pos:
                bx, by = self._last_eb_pos[gid]
            for _ in range(60):
                a2 = random.uniform(0, 6.283)
                sp2 = random.uniform(100, 400)
                self._particles.append({
                    "x": bx, "y": by,
                    "vx": math.cos(a2) * sp2, "vy": math.sin(a2) * sp2,
                    "life": random.uniform(0.3, 0.8),
                    "color": (255, random.randint(80, 200), 20),
                })
            for _ in range(20):
                a2 = random.uniform(0, 6.283)
                self._particles.append({
                    "x": bx, "y": by,
                    "vx": math.cos(a2) * 15, "vy": math.sin(a2) * 15,
                    "life": 0.2,
                    "color": (255, 255, 200),
                })
            self.trigger_shake(14)
        # 记录下一帧用
        self._prev_eb_ids = new_ids
        self._last_eb_pos = {oid: (b["x"], b["y"]) for oid, b in cur_eb.items()}
        self._got_first_state = True

        # 断线检测
        if self.pid is not None and self.pid in self.players:
            self._last_state_time = now
        elif self._got_first_state and self.pid is not None:
            if self._last_state_time > 0 and now - self._last_state_time > 2.0:
                print("  状态中丢失自己超过 2 秒, 标记断线")
                self.connected = False

    # ── 渲染 ──

    @staticmethod
    def draw_background(surf: pygame.Surface):
        # 垂直渐变背景
        for y in range(MAP_HEIGHT):
            t = y / MAP_HEIGHT
            r = int(28 + t * 20)
            g = int(28 + t * 30)
            b = int(48 + t * 40)
            pygame.draw.line(surf, (r, g, b), (0, y), (MAP_WIDTH, y))
        # 网格（半透明）
        for x in range(0, MAP_WIDTH, 50):
            pygame.draw.line(surf, (60, 60, 90, 60), (x, 0), (x, MAP_HEIGHT))
        for y in range(0, MAP_HEIGHT, 50):
            pygame.draw.line(surf, (60, 60, 90, 60), (0, y), (MAP_WIDTH, y))

    @staticmethod
    def draw_platforms(surf: pygame.Surface):
        for plat in PLATFORMS:
            px, py, pw, ph = plat
            # 底部微光
            for i in range(8, 0, -2):
                glow = pygame.Surface((pw + i * 2, ph + i * 2), pygame.SRCALPHA)
                glow.fill((72, 72, 112, 15))
                surf.blit(glow, (px - i, py - i))
            # 主体
            pygame.draw.rect(surf, COLOR_PLATFORM, (px, py, pw, ph))
            pygame.draw.line(surf, COLOR_PLATFORM_TOP,
                             (px, py), (px + pw, py), 2)
            pygame.draw.rect(surf, COLOR_PLATFORM_BORDER,
                             (px, py, pw, ph), 1)

    def draw_players(self, surf: pygame.Surface, font_small):
        now = _clock()
        my_pid = self.pid
        for p in self.players.values():
            px, py = p["x"], p["y"]
            color = p.get("color", [255, 255, 255])
            is_me = (p["id"] == my_pid)
            alive = p.get("alive", True)

            # 死亡爆炸
            d_start = self._death_anim.get(p["id"])
            if d_start and not alive:
                d_prog = min(1.0, (now - d_start) / 0.25)
                if d_prog >= 1.0:
                    self._death_anim.pop(p["id"], None)
                    self._dead_done.add(p["id"])
                    for _ in range(30):
                        a = random.uniform(0, 6.283)
                        sp = random.uniform(100, 350)
                        self._particles.append({
                            "x": px + 12, "y": py + 17,
                            "vx": math.cos(a) * sp, "vy": math.sin(a) * sp,
                            "life": random.uniform(0.4, 0.9),
                            "color": (255, random.randint(80, 200), 20),
                        })
                    self.trigger_shake(12)
                # 爆心闪白
                pygame.draw.circle(surf, (255, 255, 200, 180),
                                   (int(px + 12), int(py + 17)),
                                   int(15 * (1 - d_prog)) + 5)
                continue
            # 死亡后不绘制
            if not alive:
                continue

            # 阴影（仅存活时绘制）
            shadow = pygame.Surface((PLAYER_W, 6), pygame.SRCALPHA)
            shadow.fill((0, 0, 0, 60))
            surf.blit(shadow, (round(px), round(py + PLAYER_H - 2)))

            flash = self.hit_flash.get(p["id"])
            flash_on = flash and (now - flash < 0.12)
            heal_f = self._heal_flash.get(p["id"], 0)
            heal_on = heal_f and (now - heal_f < 0.4)
            buffs = p.get("buffs", {})
            has_explosive = "explosive" in buffs
            has_shield = "shield" in buffs
            # 颜色优先级：闪白 > 爆裂橙 > 回血绿 > 回血渐变 > 原色
            if flash_on:
                body_color = (255, 255, 255)
            elif has_explosive:
                body_color = (255, 160, 50)
            elif heal_on:
                t_h = (now - heal_f) / 0.4
                body_color = tuple(int(255 * (1 - t_h) + c * t_h) for c in color)
                # 绿色光环
                gr = max(PLAYER_W, PLAYER_H) + int(20 * (1 - t_h))
                gs = pygame.Surface((gr*2, gr*2), pygame.SRCALPHA)
                pygame.draw.circle(gs, (80, 255, 80, int(120 * (1 - t_h))),
                                   (gr, gr), gr, 3)
                surf.blit(gs, (round(px + PLAYER_W//2 - gr), round(py + PLAYER_H//2 - gr)))
            else:
                body_color = color

            # 巨大化缩放
            scale = self._size_scale.get(p["id"], 1.0)
            sw = int(PLAYER_W * scale)
            sh = int(PLAYER_H * scale)
            sx_off = (sw - PLAYER_W) // 2  # 保持居中
            sy_off = sh - PLAYER_H       # 底部固定

            # 玩家底部光晕
            glow_s = pygame.Surface((sw + 16, sh + 16), pygame.SRCALPHA)
            for r in range(12, 0, -3):
                pygame.draw.ellipse(glow_s, (*color[:3], 12),
                                    (8 - r, 8 - r, sw + r * 2, sh + r * 2), 0)
            surf.blit(glow_s, (round(px - sx_off - 8), round(py - sy_off - 8)))

            rect = (round(px - sx_off), round(py - sy_off), sw, sh)
            pygame.draw.rect(surf, body_color, rect, border_radius=3)
            bw = 3 if is_me else 1
            bc = (255, 255, 255) if is_me else (180, 180, 180)
            pygame.draw.rect(surf, bc, rect, bw, border_radius=3)

            # 护盾光环（正圆）
            if has_shield:
                s_r = max(sw, sh) // 2 + int(14 * scale)
                cx_p = round(px - sx_off + sw // 2)
                cy_p = round(py - sy_off + sh // 2)
                pygame.draw.circle(surf, (50, 140, 255, 120), (cx_p, cy_p), s_r, 3)
                pygame.draw.circle(surf, (100, 200, 255, 60), (cx_p, cy_p), s_r + 4, 2)

            target_hx = px + PLAYER_W // 2
            cur_hx = self._head_x.get(p["id"], target_hx)
            new_hx = cur_hx + (target_hx - cur_hx) * 0.10
            new_hx = max(target_hx - 6, min(target_hx + 6, new_hx))
            self._head_x[p["id"]] = new_hx
            hx = new_hx
            head_r = int(7 * scale)
            hy = py - sy_off - head_r
            pygame.draw.circle(surf, body_color, (round(hx), round(hy)), head_r)
            pygame.draw.circle(surf, bc, (round(hx), round(hy)), head_r, max(1, bw))
            eox = int(3 * scale) * p.get("facing", 1)
            eo = max(1, int(2 * scale))
            pygame.draw.circle(surf, (255, 255, 255),
                               (round(hx + eox - eo), round(hy - 1)), max(1, int(2 * scale)))
            pygame.draw.circle(surf, (255, 255, 255),
                               (round(hx + eox + eo), round(hy - 1)), max(1, int(2 * scale)))
            pygame.draw.circle(surf, (0, 0, 0),
                               (round(hx + eox + 1), round(hy - 1)), max(1, int(scale)))

            weapon = p.get("weapon", 0)
            if p["facing"] == 1:
                muzzle = (px + PLAYER_W, py + PLAYER_H // 2 - 2, 8, 4)
            else:
                muzzle = (px - 8, py + PLAYER_H // 2 - 2, 8, 4)
            if weapon == 1:
                pygame.draw.rect(surf, (60, 60, 60), muzzle, border_radius=1)
            else:
                bc2 = (200, 200, 220)
                if p["facing"] == 1:
                    pygame.draw.rect(surf, bc2,
                                     (px + PLAYER_W - 2, py + 8, 10, 4),
                                     border_radius=1)
                else:
                    pygame.draw.rect(surf, bc2,
                                     (px - 8, py + 8, 10, 4),
                                     border_radius=1)

            if p.get("melee_active") and p["id"] in self._melee_anim:
                t_raw = min(1.0, (now - self._melee_anim[p["id"]]) / MELEE_ACTIVE_TIME)
                progress = t_raw * t_raw * (3 - 2 * t_raw)
                sign = 1 if p["facing"] == 1 else -1
                ms = self._size_scale.get(p["id"], 1.0)
                cx2 = px + PLAYER_W if p["facing"] == 1 else px
                cy2 = py + PLAYER_H // 2
                inner_r = int(20 * ms)
                outer_r = int(50 * ms)
                angle = -80 + 160 * progress
                rad = math.radians(angle)
                nx = cx2 + math.cos(rad) * inner_r * sign
                ny = cy2 + math.sin(rad) * inner_r
                fx = cx2 + math.cos(rad) * outer_r * sign
                fy = cy2 + math.sin(rad) * outer_r
                for off in range(4, 0, -1):
                    pygame.draw.line(surf, (255, 255, 255, 20),
                                     (nx + off, ny), (fx + off, fy), 6 + off)
                pygame.draw.line(surf, (255, 255, 255), (nx, ny), (fx, fy), 3)
                pygame.draw.circle(surf, (255, 255, 255), (int(fx), int(fy)), 2)

            name_text = p.get("name", f"#{p['id']}")
            if is_me:
                name_text += " (你)"
            nl = font_small.render(name_text, True, COLOR_NAME)
            nr = nl.get_rect(center=(px + PLAYER_W // 2, py - 18))
            surf.blit(nl, nr)

            hp = max(0, p.get("hp", 0))
            hp_pct = hp / PLAYER_MAX_HP
            bw2 = PLAYER_W + 6
            bx = px - 3
            by = py + PLAYER_H + 4
            pygame.draw.rect(surf, COLOR_HP_BAR_BG, (bx, by, bw2, 4))
            if hp_pct > 0:
                fw = int(bw2 * hp_pct)
                hpc = (220, 50, 50) if hp_pct < 0.4 else (
                    50 + int((1 - hp_pct) * 200),
                    220 - int((1 - hp_pct) * 170), 50)
                pygame.draw.rect(surf, hpc, (bx, by, fw, 4))
            pygame.draw.rect(surf, (80, 80, 80), (bx, by, bw2, 4), 1)

    def draw_items(self, surf: pygame.Surface):
        items = getattr(self, '_state_items', [])
        for it in items:
            x, y = it["x"], it["y"]
            # 发光方块（放大尺寸）
            col = (180, 140, 255)
            for r in range(12, 0, -3):
                s = pygame.Surface((r*4, r*4), pygame.SRCALPHA)
                s.fill((*col, 30))
                surf.blit(s, (x - r*2, y - r*2))
            sz = 18
            pygame.draw.rect(surf, col, (x - sz//2, y - sz//2, sz, sz))
            pygame.draw.rect(surf, (255,255,255), (x - sz//2, y - sz//2, sz, sz), 2)

    def draw_bullets(self, surf: pygame.Surface):
        for b in self.bullets:
            bs = 2.0 if b.get("giant") else 1.0
            bw2 = int(BULLET_W * bs)
            bh2 = int(BULLET_H * bs)
            rect = (round(b["x"]), round(b["y"]), bw2, bh2)
            pygame.draw.rect(surf, COLOR_BULLET, rect, border_radius=1)
            if b.get("vx", 0) != 0:
                tx = b["x"] - (b["vx"] / abs(b["vx"])) * 6
                pygame.draw.line(surf, (255, 220, 100),
                                 (round(tx), round(b["y"] + BULLET_H // 2)),
                                 (round(b["x"]), round(b["y"] + BULLET_H // 2)),
                                 2)

    def trigger_shake(self, intensity: float):
        self._shake = max(self._shake, intensity)

    def spawn_particles(self, x, y, color, count=8):
        import math as m
        for _ in range(count):
            a = random.uniform(0, m.pi * 2)
            sp = random.uniform(30, 100)
            self._particles.append({
                "x": x, "y": y, "vx": m.cos(a) * sp, "vy": m.sin(a) * sp,
                "life": random.uniform(0.3, 0.6), "color": color,
            })

    def draw_particles(self, surf: pygame.Surface):
        now = _clock()
        dead = []
        for p in self._particles:
            p["x"] += p["vx"] * 0.016
            p["y"] += p["vy"] * 0.016
            p["vy"] += 200 * 0.016  # 粒子重力
            p["life"] -= 0.016
            if p["life"] <= 0:
                dead.append(p)
                continue
            alpha = min(255, int(255 * max(0, p["life"] / 0.5)))
            sz = max(1, int(3 * p["life"] / 0.5))
            pygame.draw.circle(surf, (*p["color"][:3], alpha),
                               (int(p["x"]), int(p["y"])), sz)
        for p in dead:
            self._particles.remove(p)

    def draw_vignette(self, surf: pygame.Surface):
        # 低血量红色晕影
        my_hp = 0
        if self.pid is not None and self.pid in self.players:
            my_hp = self.players[self.pid].get("hp", 100)
        intensity = max(0, 1 - my_hp / 40)
        if intensity > 0:
            vg = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
            for r in range(WIDTH // 2, 0, -1):
                a = int((WIDTH // 2 - r) / (WIDTH // 2) * 120 * intensity)
                if a <= 0:
                    continue
                pygame.draw.circle(vg, (180, 20, 20, a),
                                   (WIDTH // 2, HEIGHT // 2), r, 2)
            surf.blit(vg, (0, 0))

    def draw_hud(self, surf: pygame.Surface, font, font_large, font_small):
        now = _clock()

        sorted_pl = sorted(
            self.players.values(),
            key=lambda p: (-p.get("kills", 0), p.get("deaths", 0))
        )
        title = font.render("-- 排行 --", True, (255, 215, 0))
        surf.blit(title, (WIDTH - 170, 8))
        for i, p in enumerate(sorted_pl):
            y = 34 + i * 22
            if y > HEIGHT - 20:
                break
            color = p.get("color", [255, 255, 255])
            is_me = (p["id"] == self.pid)
            alive = p.get("alive", True)
            pygame.draw.circle(surf, color, (WIDTH - 160, y + 5), 5)
            st = "" if alive else " †"
            pf = "> " if is_me else "  "
            text = f"{pf}{p.get('name','?')[:7]}{st}  {p.get('kills',0)}杀 {p.get('deaths',0)}死"
            clr = (255, 255, 255) if is_me else (200, 200, 200)
            label = font_small.render(text, True, clr)
            surf.blit(label, (WIDTH - 148, y))

        w_names = ["近战", "枪械"]
        w_text = f"武器: [1]{w_names[0]}  [2]{w_names[1]}  -> {w_names[self.weapon]}"
        wl = font_small.render(w_text, True, COLOR_HUD)
        surf.blit(wl, (12, HEIGHT - 40))
        ctrl_text = "W/Space=跳  A=左  S=下落  D=右  J=射击  K=近战  左键=射击  ESC=退出"
        cl = font_small.render(ctrl_text, True, (140, 140, 160))
        surf.blit(cl, (12, HEIGHT - 22))

        st_color = COLOR_CONNECTED if self.connected else COLOR_DISCONNECTED
        st_text = "已连接" if self.connected else "已断开"
        alive_n = sum(1 for p in self.players.values() if p.get("alive"))
        total = len(self.players)
        st = font_small.render(
            f"{st_text}  |  在线 {total} 人  |  存活 {alive_n}",
            True, st_color)
        sr = st.get_rect(center=(WIDTH // 2, HEIGHT - 4))
        sr.bottom = HEIGHT - 4
        surf.blit(st, sr)

        # Buff 文字（含剩余时间）
        if self.pid is not None and self.pid in self.players:
            raw = self.players[self.pid].get("buffs", {})
            bnames = {"speed":"加速","shield":"护盾","explosive":"爆裂","giant":"巨大化"}
            items = []
            for bk, bv in sorted(raw.items(), key=lambda x: -x[1] if isinstance(x[1], (int,float)) else 0):
                label = bnames.get(bk, bk)
                if bk in ("speed", "giant", "shield") and isinstance(bv, (int,float)):
                    label += f" {bv:.0f}s"
                elif bk == "explosive":
                    label += " \u25cf"
                items.append(label)
            if items:
                txt = " | ".join(items[:3])
                lbl = font_small.render(txt, True, (255, 230, 100))
                surf.blit(lbl, (12, HEIGHT - 62))


    # ── 主循环 ──

    def run(self):
        pygame.init()
        screen = pygame.display.set_mode((WIDTH, HEIGHT))
        pygame.display.set_caption(TITLE)
        clock = pygame.time.Clock()
        font = _get_font(28)
        font_large = _get_font(34)
        font_small = _get_font(20)

        self.running = True
        disconnect_logged = False
        self._last_state_time = _clock()

        while self.running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        self.running = False
                    self._on_key(event.key, True)
                elif event.type == pygame.KEYUP:
                    self._on_key(event.key, False)

            # J=射击  K=近战  鼠标左键=射击
            if self.melee_pressed:
                self.weapon = 0
                self.attack = True
            elif self.gun_pressed or pygame.mouse.get_pressed()[0]:
                self.weapon = 1
                self.attack = True
            else:
                self.attack = False

            if self.keys["left"]:
                self.facing = -1
            elif self.keys["right"]:
                self.facing = 1

            if self.connected and self.pid is not None:
                disconnect_logged = False
                self.send_input()
                self.receive_state()

                # 断线检测
                if self._got_first_state and _clock() - self._last_state_time > 3.0:
                    self.connected = False
                    continue

                # 震动衰减
                if self._shake > 0:
                    self._shake *= 0.85
                    if self._shake < 0.5:
                        self._shake = 0.0
                shake_off = (random.randint(-int(self._shake), int(self._shake)),
                             random.randint(-int(self._shake), int(self._shake)))
                # 绘制到临时画布实现震动
                canvas = pygame.Surface((WIDTH, HEIGHT))
                self.draw_background(canvas)
                self.draw_platforms(canvas)
                self.draw_items(canvas)
                self.draw_bullets(canvas)
                self.draw_players(canvas, font_small)
                self.draw_particles(canvas)
                self.draw_vignette(canvas)
                self.draw_hud(canvas, font, font_large, font_small)
                screen.blit(canvas, shake_off)
            else:
                if not disconnect_logged:
                    disconnect_logged = True
                screen.fill(COLOR_BG)
                msg = "与服务器断开 (ESC退出)" if self.server_addr else "未连接"
                label = font_large.render(msg, True, (200, 200, 200))
                lr = label.get_rect(center=(WIDTH // 2, HEIGHT // 2))
                screen.blit(label, lr)
                self.receive_state()

            pygame.display.flip()
            clock.tick(FPS)

        pygame.quit()
        self.disconnect()

    def _on_key(self, key: int, down: bool):
        mapping = {
            pygame.K_a: "left", pygame.K_LEFT: "left",
            pygame.K_d: "right", pygame.K_RIGHT: "right",
            pygame.K_w: "jump", pygame.K_UP: "jump",
            pygame.K_SPACE: "jump",
            pygame.K_s: "down", pygame.K_DOWN: "down",
        }
        k = mapping.get(key)
        if k:
            self.keys[k] = down
            return
        if key == pygame.K_j:
            self.gun_pressed = down
            return
        if key == pygame.K_k:
            self.melee_pressed = down
            return
        if down:
            if key == pygame.K_1:
                self.weapon = 0
            elif key == pygame.K_2:
                self.weapon = 1



# ── 启动 ──

def main():
    import argparse
    parser = argparse.ArgumentParser(description="LAN Platform Battle")
    parser.add_argument("server_ip", nargs="?", default=None)
    parser.add_argument("-p", "--port", type=int, default=SERVER_PORT)
    parser.add_argument("-n", "--name", default="")
    args = parser.parse_args()

    client = GameClient()

    ip = args.server_ip or client.discover_server(2.0)
    while not ip:
        ip = input("输入服务器 IP: ").strip()

    name = args.name.strip()
    while not name:
        name = input("输入名字 (回车随机): ").strip()
        if not name:
            name = f"玩家{random.randint(10, 99)}"

    if not client.connect(ip, args.port, name):
        print("连接失败")
        input("按回车退出...")
        sys.exit(1)

    try:
        client.run()
    except KeyboardInterrupt:
        pass
    finally:
        client.disconnect()
        print("客户端已退出")


if __name__ == "__main__":
    main()
