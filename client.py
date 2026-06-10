"""
LAN Platform Battle — 游戏客户端 (UDP)
pygame 渲染 + UDP 连接
2D 平台跳跃 + 近战/枪械
"""
import pygame
import json
import time
import math
import sys
import random

_clock = time.monotonic

sys.path.insert(0, ".")
from config import *
from network import UdpNode


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
        self.node = UdpNode(broadcast=True)  # 用于 LAN 发现 + 游戏通信
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

        self.prev_hp: dict[int, int] = {}
        self.hit_flash: dict[int, float] = {}
        self._got_first_state = False
        self._last_state_time = 0.0
        self._melee_anim: dict[int, float] = {}
        self._shake = 0.0
        self._particles: list[dict] = []
        self._prev_vy: dict[int, float] = {}
        self._head_x: dict[int, float] = {}
        self._size_scale: dict[int, float] = {}
        self._heal_flash: dict[int, float] = {}
        self._death_anim: dict[int, float] = {}
        self._dead_done: set[int] = set()

    # ── LAN 发现 ──

    def discover_server(self, timeout: float = 2.0) -> str | None:
        print("正在扫描局域网游戏服务器 ...")
        self.node.send(("255.255.255.255", SERVER_PORT),
                       {"type": "discover"}, reliable=False)
        start = time.time()
        while time.time() - start < timeout:
            for addr, msg in self.node.recv_all():
                if msg.get("type") == "here":
                    return addr[0]
            time.sleep(0.01)
        return None

    # ── UDP 连接 ──

    def connect(self, server_ip: str, port: int = SERVER_PORT,
                player_name: str = "") -> bool:
        self.server_addr = (server_ip, port)

        # 重置状态
        self._melee_anim.clear()
        self._shake = 0.0
        self._particles.clear()
        self._prev_vy.clear()
        self._head_x.clear()
        self._size_scale.clear()
        self._heal_flash.clear()
        self._death_anim.clear()
        self._dead_done.clear()
        self.kill_msgs.clear()
        self._got_first_state = False
        self._last_state_time = 0.0

        if not player_name:
            player_name = f"玩家{random.randint(10, 99)}"

        print(f"连接 {server_ip}:{port} ...")

        # 发送 join（高可靠：序列号 + ACK + 自动重试）
        self.node.send(self.server_addr,
                       {"type": "join", "name": player_name}, reliable=True)

        # 等待 welcome（高可靠）
        deadline = time.time() + 5.0
        while time.time() < deadline:
            for addr, msg in self.node.recv_all():
                if addr != self.server_addr:
                    continue
                if msg.get("type") == "welcome":
                    self.pid = msg["pid"]
                    self.color = msg.get("color", [255, 255, 255])
                    self.server_tick = msg.get("server_tick", TICK_RATE)
                    self.connected = True
                    print(f"  收到 welcome, ID={self.pid}")
                    return self._wait_initial_state()
                elif msg.get("type") == "error":
                    print(f"  {msg.get('message', '未知错误')}")
                    return False
            time.sleep(0.01)

        print(f"  加入超时")
        return False

    def _wait_initial_state(self) -> bool:
        """等待第一个 state 消息"""
        print("  等待初始状态 ...")
        self.node.send(self.server_addr, {
            "type": "input", "keys": {}, "weapon": 0,
            "attack": False, "facing": 1,
        }, reliable=False)

        deadline = time.time() + 4.0
        while time.time() < deadline:
            for addr, msg in self.node.recv_all():
                if addr != self.server_addr:
                    continue
                if msg.get("type") == "state":
                    self.handle_state(msg)
                    if self.pid in self.players:
                        print(f"  初始状态就绪, 在线 {len(self.players)} 人")
                        return True
            time.sleep(0.01)

        print("  初始状态未到, 继续游戏循环")
        return True

    def disconnect(self):
        self.connected = False
        if self.node and self.server_addr:
            # 发送断开通知（高可靠）
            self.node.send(self.server_addr,
                           {"type": "disconnect"}, reliable=True)
            time.sleep(0.05)
        if self.node:
            self.node.close()
            self.node = None

    # ── 网络收发 ──

    def send_input(self):
        if not self.node or not self.server_addr:
            return
        self.node.send(self.server_addr, {
            "type": "input",
            "keys": self.keys,
            "weapon": self.weapon,
            "attack": self.attack,
            "facing": self.facing,
        }, reliable=False)

    def receive_state(self):
        if not self.node or not self.server_addr:
            return
        for addr, msg in self.node.recv_all():
            if addr != self.server_addr:
                continue
            t = msg.get("type")
            if t == "state":
                self.handle_state(msg)
            elif t == "kill_event":
                self.handle_kill_event(msg)

    def handle_kill_event(self, msg: dict):
        """高可靠击杀事件（必达 + 保序）"""
        now = _clock()
        text = f"{msg.get('killer_name', '?')} 淘汰了 {msg.get('victim_name', '?')}"
        self.kill_msgs.append((text, now))

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
            if not pd["alive"] and pd["id"] not in self._death_anim and pd["id"] not in self._dead_done:
                self._death_anim[pd["id"]] = now
                self.trigger_shake(8)
                c3 = pd.get("color", [255, 255, 255])
                self.spawn_particles(pd.get("x", 0) + 12, pd.get("y", 0) + 17, c3, 12)
            if pd["alive"]:
                self._death_anim.pop(pd["id"], None)
                self._dead_done.discard(pd["id"])
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
            if pd.get("melee_active") and pid not in self._melee_anim:
                self._melee_anim[pid] = now
            elif not pd.get("melee_active") and pid in self._melee_anim:
                del self._melee_anim[pid]

        cur_eb = {b["owner_id"]: b for b in self.bullets if b.get("explosive")}
        old_eb_ids = set(getattr(self, '_prev_eb_ids', set()))
        self.players = {p["id"]: p for p in msg.get("players", [])}
        for pl2 in self.players.values():
            target = 2.0 if "giant" in pl2.get("buffs", {}) else 1.0
            cur = self._size_scale.get(pl2["id"], 1.0)
            self._size_scale[pl2["id"]] = cur + (target - cur) * 0.15
        self.bullets = msg.get("bullets", [])
        self._state_items = msg.get("items", [])
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
        new_ids = set(cur_eb.keys())
        gone_ids = old_eb_ids - new_ids
        for gid in gone_ids:
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
        self._prev_eb_ids = new_ids
        self._last_eb_pos = {oid: (b["x"], b["y"]) for oid, b in cur_eb.items()}
        self._got_first_state = True

        if self.pid is not None and self.pid in self.players:
            self._last_state_time = now
        elif self._got_first_state and self.pid is not None:
            if self._last_state_time > 0 and now - self._last_state_time > 2.0:
                print("  状态中丢失自己超过 2 秒, 标记断线")
                self.connected = False

    # ── 渲染 ──

    @staticmethod
    def draw_background(surf: pygame.Surface):
        for y in range(MAP_HEIGHT):
            t = y / MAP_HEIGHT
            r = int(28 + t * 20)
            g = int(28 + t * 30)
            b = int(48 + t * 40)
            pygame.draw.line(surf, (r, g, b), (0, y), (MAP_WIDTH, y))
        for x in range(0, MAP_WIDTH, 50):
            pygame.draw.line(surf, (60, 60, 90, 60), (x, 0), (x, MAP_HEIGHT))
        for y in range(0, MAP_HEIGHT, 50):
            pygame.draw.line(surf, (60, 60, 90, 60), (0, y), (MAP_WIDTH, y))

    @staticmethod
    def draw_platforms(surf: pygame.Surface):
        for plat in PLATFORMS:
            px, py, pw, ph = plat
            for i in range(8, 0, -2):
                glow = pygame.Surface((pw + i * 2, ph + i * 2), pygame.SRCALPHA)
                glow.fill((72, 72, 112, 15))
                surf.blit(glow, (px - i, py - i))
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
                pygame.draw.circle(surf, (255, 255, 200, 180),
                                   (int(px + 12), int(py + 17)),
                                   int(15 * (1 - d_prog)) + 5)
                continue
            if not alive:
                continue

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
            if flash_on:
                body_color = (255, 255, 255)
            elif has_explosive:
                body_color = (255, 160, 50)
            elif heal_on:
                t_h = (now - heal_f) / 0.4
                body_color = tuple(int(255 * (1 - t_h) + c * t_h) for c in color)
                gr = max(PLAYER_W, PLAYER_H) + int(20 * (1 - t_h))
                gs = pygame.Surface((gr*2, gr*2), pygame.SRCALPHA)
                pygame.draw.circle(gs, (80, 255, 80, int(120 * (1 - t_h))),
                                   (gr, gr), gr, 3)
                surf.blit(gs, (round(px + PLAYER_W//2 - gr), round(py + PLAYER_H//2 - gr)))
            else:
                body_color = color

            scale = self._size_scale.get(p["id"], 1.0)
            sw = int(PLAYER_W * scale)
            sh = int(PLAYER_H * scale)
            sx_off = (sw - PLAYER_W) // 2
            sy_off = sh - PLAYER_H

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

    def draw_kill_feed(self, surf: pygame.Surface, font_small):
        """绘制击杀信息（来自高可靠 kill_event 消息）"""
        now = _clock()
        # 清理过期消息
        self.kill_msgs = [(t, ts) for t, ts in self.kill_msgs if now - ts < 5.0]
        y = 30
        for text, ts in reversed(self.kill_msgs[-5:]):
            age = now - ts
            alpha = max(0, min(255, int(255 * (1 - age / 5.0))))
            label = font_small.render(text, True, (255, 240, 100))
            label.set_alpha(alpha)
            surf.blit(label, (12, y))
            y += 22

    def draw_items(self, surf: pygame.Surface):
        items = getattr(self, '_state_items', [])
        for it in items:
            x, y = it["x"], it["y"]
            col = (180, 140, 255)
            for r in range(12, 0, -3):
                s = pygame.Surface((r*4, r*4), pygame.SRCALPHA)
                s.fill((*col, 30))
                surf.blit(s, (x - r*2, y - r*2))
            sz = 18
            pygame.draw.rect(surf, col, (x - sz//2, y - sz//2, sz, sz))
            pygame.draw.rect(surf, (255, 255, 255), (x - sz//2, y - sz//2, sz, sz), 2)

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
        for _ in range(count):
            a = random.uniform(0, math.pi * 2)
            sp = random.uniform(30, 100)
            self._particles.append({
                "x": x, "y": y, "vx": math.cos(a) * sp, "vy": math.sin(a) * sp,
                "life": random.uniform(0.3, 0.6), "color": color,
            })

    def draw_particles(self, surf: pygame.Surface):
        now = _clock()
        dead = []
        for p in self._particles:
            p["x"] += p["vx"] * 0.016
            p["y"] += p["vy"] * 0.016
            p["vy"] += 200 * 0.016
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
            st = "" if alive else " \u2020"
            pf = "> " if is_me else "  "
            text = f"{pf}{p.get('name','?')[:7]}{st}  {p.get('kills',0)}\u6740 {p.get('deaths',0)}\u6b7b"
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

        if self.pid is not None and self.pid in self.players:
            raw = self.players[self.pid].get("buffs", {})
            bnames = {"speed": "加速", "shield": "护盾",
                      "explosive": "爆裂", "giant": "巨大化"}
            items = []
            for bk, bv in sorted(raw.items(),
                                 key=lambda x: -x[1] if isinstance(x[1], (int, float)) else 0):
                label = bnames.get(bk, bk)
                if bk in ("speed", "giant", "shield") and isinstance(bv, (int, float)):
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

            if self.connected and self.pid is not None and self.node:
                disconnect_logged = False
                self.send_input()
                self.receive_state()

                if self._got_first_state and _clock() - self._last_state_time > 3.0:
                    self.connected = False
                    continue

                if self._shake > 0:
                    self._shake *= 0.85
                    if self._shake < 0.5:
                        self._shake = 0.0
                shake_off = (random.randint(-int(self._shake), int(self._shake)),
                             random.randint(-int(self._shake), int(self._shake)))

                canvas = pygame.Surface((WIDTH, HEIGHT))
                self.draw_background(canvas)
                self.draw_platforms(canvas)
                self.draw_items(canvas)
                self.draw_bullets(canvas)
                self.draw_players(canvas, font_small)
                self.draw_kill_feed(canvas, font_small)
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
