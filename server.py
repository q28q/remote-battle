"""
LAN Platform Battle — 游戏服务器 (TCP)
2D 平台跳跃 + 近战/枪械 权威服务器
"""
import socket
import select
import json
import time as _time_module
import math
import random
import sys

_clock = _time_module.monotonic

sys.path.insert(0, ".")
from config import *


class Player:
    def __init__(self, pid: int, sock: socket.socket, name: str):
        sp = random.choice(SPAWN_POINTS)
        self.id = pid
        self.sock = sock
        self.name = name[:10]
        self.x, self.y = float(sp[0]), float(sp[1])
        self.vx = 0.0
        self.vy = 0.0
        self.facing = 1
        self.hp = PLAYER_MAX_HP
        self.kills = 0
        self.deaths = 0
        # 颜色在 handle_join 中由服务器分配
        self.color: tuple[int, int, int] = (255, 255, 255)
        self.last_active = _clock()
        self.input_left = False
        self.input_right = False
        self.input_jump = False
        self.input_down = False
        self.prev_jump = False
        self.weapon = 0
        self.attack = False
        self.on_ground = False
        self.alive = True
        self.dead_timer = 0.0
        self.gun_cooldown = 0.0
        self.melee_cooldown = 0.0
        self.melee_active = False
        self.melee_timer = 0.0
        self.melee_hit_set = set()
        self.can_double_jump = True
        self.knockback_timer = 0.0
        self.knockback_drag = 0.0
        self.buffs: dict[str, float] = {}
        self.prev_attack = False


class Bullet:
    def __init__(self, x: float, y: float, vx: float, vy: float,
                 owner_id: int, explosive: bool = False, giant: bool = False):
        self.x = x; self.y = y; self.vx = vx; self.vy = vy
        self.owner_id = owner_id
        self.explosive = explosive
        self.giant = giant
        self.init_vx = vx
        self.spawn_time = _clock()


class Item:
    """从空中掉落的道具方块"""
    TYPES = ("speed", "shield", "heal", "explosive", "giant")
    WEIGHTS = (4, 4, 2, 4, 2)  # 护盾 = 加速 = 爆裂 > 回血 = 巨大化
    def __init__(self, x: float, y: float, vy: float = 60.0):
        self.x = x; self.y = y; self.vy = vy
        self.spawn_time = _clock()
    @property
    def type(self):
        return random.choices(self.TYPES, weights=self.WEIGHTS, k=1)[0]

def _random_player_color(existing: list[tuple[int,int,int]]) -> tuple[int,int,int]:
    for _ in range(50):
        r, g, b = random.randint(60, 255), random.randint(60, 255), random.randint(60, 255)
        if r > 200 and g > 100 and b < 100:
            continue
        if r < 60 and g < 60 and b < 60:
            continue
        ok = True
        for er, eg, eb in existing:
            if (r - er) ** 2 + (g - eg) ** 2 + (b - eb) ** 2 < 2000:
                ok = False
                break
        if ok:
            return (r, g, b)
    return (random.randint(100,255), random.randint(100,255), random.randint(100,255))

# 物理每秒常量（原常量按 30 tick/s 标定，换算到每秒）
_BASE_TICK = 30.0
_SPEED_PS = PLAYER_SPEED * _BASE_TICK
_GRAVITY_PS = GRAVITY * _BASE_TICK ** 2
_JUMP_PS = JUMP_SPEED * _BASE_TICK
_MAX_FALL_PS = MAX_FALL_SPEED * _BASE_TICK


class GameServer:
    def __init__(self, port: int = SERVER_PORT):
        self.port = port
        self.players: dict[int, Player] = {}
        self.sock_to_pid: dict[socket.socket, int] = {}
        self.bullets: list[Bullet] = []
        self.kill_feed: list[str] = []
        self.running = False
        self.server_sock: socket.socket | None = None
        self.disc_sock: socket.socket | None = None
        self._next_pid = 1
        self._pending_socks: list[socket.socket] = []
        self._recv_buf: dict[socket.socket, bytes] = {}
        self.items: list[Item] = []
        self._next_item_spawn = _clock() + 3.0

    # ── TCP 收发 ──

    # 标记：连接已断开（与"暂无完整消息"的 None 区分）
    _DISCONNECTED = object()

    @staticmethod
    def _send_all(sock: socket.socket, data: bytes):
        sock.sendall(len(data).to_bytes(4, 'big') + data)

    def _recv_msg(self, sock: socket.socket) -> dict | None | object:
        """从缓冲区提取一条消息。返回 dict=成功, None=等待更多数据, _DISCONNECTED=断开"""
        buf = self._recv_buf.get(sock, b'')
        try:
            chunk = sock.recv(4096)
            if chunk:
                buf += chunk
                self._recv_buf[sock] = buf
            else:
                return self._DISCONNECTED
        except BlockingIOError:
            pass
        except OSError:
            return self._DISCONNECTED
        if len(buf) < 4:
            return None
        n = int.from_bytes(buf[:4], 'big')
        if len(buf) < 4 + n:
            return None
        body = buf[4:4 + n]
        self._recv_buf[sock] = buf[4 + n:]
        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def send_to(self, sock: socket.socket, data: dict):
        try:
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._send_all(sock, payload)
        except OSError:
            pass

    def broadcast(self, data: dict):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        for p in list(self.players.values()):
            try:
                self._send_all(p.sock, payload)
            except OSError as e:
                print(f"  [广播失败] {p.name}(ID={p.id}) {e}")

    # ── 消息处理 ──

    def _handle_discover(self, sock: socket.socket):
        """处理 UDP LAN 发现"""
        try:
            data, addr = sock.recvfrom(4096)
            msg = json.loads(data.decode("utf-8"))
            if msg.get("type") == "discover":
                resp = json.dumps({"type": "here"}).encode("utf-8")
                sock.sendto(resp, addr)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass

    def handle_message(self, sock: socket.socket, msg: dict):
        t = msg.get("type", "")
        if t == "join":
            self.handle_join(sock, msg)
        elif t == "input":
            self.handle_input(sock, msg)
        elif t == "disconnect":
            self._remove_by_sock(sock, is_disconnect=True)

    def handle_join(self, sock: socket.socket, msg: dict):
        if sock in self.sock_to_pid:
            return
        if len(self.players) >= MAX_PLAYERS:
            self.send_to(sock, {"type": "error", "message": "服务器已满"})
            return
        name = msg.get("name", f"玩家{self._next_pid}")
        p = Player(self._next_pid, sock, name)
        self._next_pid += 1
        p.color = _random_player_color([pl.color for pl in self.players.values()])
        self.players[p.id] = p
        self.sock_to_pid[sock] = p.id
        if sock in self._pending_socks:
            self._pending_socks.remove(sock)
        self.send_to(sock, {
            "type": "welcome", "pid": p.id,
            "color": list(p.color), "server_tick": TICK_RATE,
        })
        print(f"[+] {p.name}(ID={p.id}) 加入  在线: {len(self.players)}人")

    def handle_input(self, sock: socket.socket, msg: dict):
        pid = self.sock_to_pid.get(sock)
        if pid is None:
            return
        p = self.players.get(pid)
        if p is None:
            return
        p.last_active = _clock()
        keys = msg.get("keys", {})
        p.input_left = bool(keys.get("left", False))
        p.input_right = bool(keys.get("right", False))
        p.input_jump = bool(keys.get("jump", False))
        p.input_down = bool(keys.get("down", False))
        p.attack = bool(msg.get("attack", False))
        w = msg.get("weapon", p.weapon)
        if isinstance(w, int) and w in (0, 1):
            p.weapon = w
        f = msg.get("facing", 0)
        if f in (-1, 1):
            p.facing = f


    def _remove_by_sock(self, sock: socket.socket, is_disconnect: bool = False):
        pid = self.sock_to_pid.pop(sock, None)
        if pid is not None:
            p = self.players.get(pid)
            name = p.name if p else f"#{pid}"
            self.remove_player(pid, is_disconnect=is_disconnect)
            print(f"[-] {name}(ID={pid}) {'主动断开' if is_disconnect else '断开'}")
        elif sock in self._pending_socks:
            self._pending_socks.remove(sock)
        self._recv_buf.pop(sock, None)
        try:
            sock.close()
        except OSError:
            pass

    # ── 游戏逻辑（与 UDP 版一致）──

    def tick(self):
        now = _clock()
        dt = now - getattr(self, '_last_tick', now)
        self._last_tick = now
        dt = min(dt, 0.1)
        # 道具生成
        self._update_items(now)
        for p in self.players.values():
            self.update_player(p, dt)
        self.update_bullets(now, dt)
        self.check_timeout(now)
        if len(self.kill_feed) > 10:
            self.kill_feed = self.kill_feed[-10:]

    def _update_items(self, now: float):
        for i in list(self.items):
            i.y += i.vy * 0.016
            if i.y > MAP_HEIGHT * 2:
                self.items.remove(i)
        if now >= self._next_item_spawn and len(self.items) < 5:
            sx = random.uniform(50, MAP_WIDTH - 50)
            self.items.append(Item(sx, -30.0, vy=random.uniform(30, 60)))
            self._next_item_spawn = now + random.uniform(5.0, 8.0)
        # 拾取检测
        for i in list(self.items):
            for p in self.players.values():
                if not p.alive:
                    continue
                pr = 35 * (2 if "giant" in p.buffs else 1)
                dx = (p.x + PLAYER_W // 2) - i.x
                dy = (p.y + PLAYER_H // 2) - i.y
                if dx * dx + dy * dy <= pr * pr:
                    self._apply_item(p, i)
                    self.items.remove(i)
                    break

    def _apply_item(self, p: Player, item: Item):
        t = item.type
        print(f"[道具] {p.name} 拾取 {t} (hp={p.hp}/{PLAYER_MAX_HP})")
        useful = [x for x in Item.TYPES
                  if not ((x == "heal" and p.hp >= PLAYER_MAX_HP)
                          or (x == "explosive" and "explosive" in p.buffs)
                          or (x == "speed" and "speed" in p.buffs))]
        if t not in useful:
            ws = [Item.WEIGHTS[Item.TYPES.index(x)] for x in useful]
            t = random.choices(useful, weights=ws, k=1)[0]
            print(f"  -> 重新随机为 {t}")
        if t == "speed":
            p.buffs["speed"] = 6.0
        elif t == "shield":
            p.buffs["shield"] = 20.0
        elif t == "heal":
            p.hp = min(PLAYER_MAX_HP, p.hp + 40)
        elif t == "explosive":
            p.buffs["explosive"] = 1.0
        elif t == "giant":
            p.buffs["giant"] = 15.0


    def update_player(self, p: Player, dt: float):
        """每秒物理：所有常量已转为每秒值，乘以实际 dt"""
        if not p.alive:
            p.dead_timer -= dt
            if p.dead_timer <= 0:
                self.respawn(p)
            return
        if p.gun_cooldown > 0:
            p.gun_cooldown -= dt
        if p.melee_cooldown > 0:
            p.melee_cooldown -= dt
        if p.melee_active:
            p.melee_timer -= dt
            if p.melee_timer <= 0:
                p.melee_active = False
                p.melee_hit_set.clear()
        # 击退 — 线性减速抛物线
        if p.knockback_timer > 0:
            p.knockback_timer -= dt
            # 线性阻力：每秒减少 knockback_drag px/s
            drag = p.knockback_drag * dt
            if p.vx > 0:
                p.vx = max(0, p.vx - drag)
            elif p.vx < 0:
                p.vx = min(0, p.vx + drag)
            # 阻力结束时自动清除计时器
            if abs(p.vx) < 5:
                p.vx = 0.0
                p.knockback_timer = 0.0
        # Buff 计时
        for bkey in list(p.buffs):
            if bkey == "explosive":
                continue  # 次数型
            p.buffs[bkey] -= dt
            if p.buffs[bkey] <= 0:
                del p.buffs[bkey]
        speed_mult = 1.75 if "speed" in p.buffs else 1.0
        speed_mult *= 0.5 if "slow" in p.buffs else 1.0
        speed_mult *= 0.5 if "giant" in p.buffs else 1.0
        jump_mult = 1.25 if "speed" in p.buffs else 1.0
        if p.knockback_timer <= 0:
            if p.input_left:
                p.vx = -_SPEED_PS * speed_mult
                p.facing = -1
            elif p.input_right:
                p.vx = _SPEED_PS * speed_mult
                p.facing = 1
            else:
                p.vx = 0.0
        if p.input_jump and not p.prev_jump:
            if p.on_ground:
                p.vy = _JUMP_PS * jump_mult
                p.on_ground = False
                p.can_double_jump = True
            elif p.can_double_jump:
                p.vy = _JUMP_PS * 0.85 * jump_mult
                p.can_double_jump = False

        p.prev_jump = p.input_jump
        p.vy += _GRAVITY_PS * dt
        if p.vy > _MAX_FALL_PS:
            p.vy = _MAX_FALL_PS
        p.x += p.vx * dt
        p.y += p.vy * dt
        p.on_ground = False
        p.y = self.resolve_platform_collision(p, p.vy, dt)
        if p.x < 0:
            p.x = 0.0
        if p.x + PLAYER_W > MAP_WIDTH:
            p.x = float(MAP_WIDTH - PLAYER_W)
        if p.y > MAP_HEIGHT:
            self.kill_player(p, None)
            return
        if p.weapon == 0:
            if p.attack and not p.prev_attack and p.melee_cooldown <= 0:
                self.melee_attack(p)
        else:
            if p.attack and not p.prev_attack and p.gun_cooldown <= 0:
                self.gun_attack(p)
        p.prev_attack = p.attack

    def resolve_platform_collision(self, p: Player, vy: float, dt: float = 1/60) -> float:
        best_y = p.y
        for plat in PLATFORMS:
            px, py, pw, ph = plat
            if p.x + PLAYER_W <= px or p.x >= px + pw:
                continue
            # 按 S 时穿过平台（地面除外）
            if p.input_down and py < MAP_HEIGHT - 50:
                continue
            player_bottom = p.y + PLAYER_H
            prev_bottom = player_bottom - vy * dt  # vy 是每秒值
            if vy >= -1.0 and prev_bottom <= py + 4 and player_bottom >= py - 4:
                if py - PLAYER_H < best_y:
                    best_y = float(py - PLAYER_H)
                    p.on_ground = True
                    p.vy = 0.0
                    p.can_double_jump = True
        return best_y

    def melee_attack(self, p: Player):
        p.melee_active = True
        p.melee_timer = MELEE_ACTIVE_TIME
        p.melee_cooldown = MELEE_COOLDOWN
        p.melee_hit_set.clear()
        is_giant = "giant" in p.buffs
        giant_s = 1.5 if is_giant else 1.0
        cx = p.x + PLAYER_W if p.facing == 1 else p.x
        cy = p.y + PLAYER_H // 2
        radius = int(80 * giant_s)
        for other in self.players.values():
            if other.id == p.id or not other.alive:
                continue
            ox = other.x + PLAYER_W // 2
            oy = other.y + PLAYER_H // 2
            dx = ox - cx
            dy = oy - cy
            ow2 = int(PLAYER_W * (2 if "giant" in other.buffs else 1))
            if (p.facing == 1 and dx < -ow2) or (p.facing == -1 and dx > ow2):
                continue
            if dx * dx + dy * dy <= radius * radius:
                if "shield" in other.buffs:
                    del other.buffs["shield"]
                    continue
                dmg = MELEE_DAMAGE * (2 if is_giant else 1)
                dmg = dmg // (2 if "giant" in other.buffs else 1)
                other.hp -= dmg
                if other.hp <= 0:
                    self.kill_player(other, p)
                else:
                    kb = 2.0 if is_giant else 1.0
                    other.vy = -290.0 * kb
                    other.vx = 1200.0 * p.facing * kb
                    other.knockback_timer = 0.5
                    other.buffs["slow"] = 3.0
                    other.knockback_drag = 2400.0 / kb
                p.melee_hit_set.add(other.id)

    def gun_attack(self, p: Player):
        p.gun_cooldown = GUN_COOLDOWN
        bx = p.x + PLAYER_W if p.facing == 1 else p.x - BULLET_W
        by = p.y + PLAYER_H // 2 - BULLET_H // 2
        bvx = BULLET_SPEED * _BASE_TICK * p.facing
        bvy = random.uniform(-0.06, 0.06) * BULLET_SPEED * _BASE_TICK
        explosive = "explosive" in p.buffs
        giant = "giant" in p.buffs
        if explosive:
            del p.buffs["explosive"]
            bvy = 0.0
        self.bullets.append(Bullet(bx, by, bvx, bvy, p.id, explosive, giant))

    def update_bullets(self, now: float, dt: float = 1/60):
        new_bullets = []
        for b in self.bullets:
            if b.explosive and abs(b.init_vx) > 10:
                age = (now - b.spawn_time) / 0.6
                speed_mult = 1.0 + min(age, 1.0) * 2.0
                b.vx = b.init_vx * speed_mult
                # 追踪最近的敌人
                target = None
                target_dist2 = float('inf')
                for p in self.players.values():
                    if p.id == b.owner_id or not p.alive:
                        continue
                    dx = (p.x + PLAYER_W // 2) - b.x
                    dy = (p.y + PLAYER_H // 2) - b.y
                    d2 = dx * dx + dy * dy
                    if d2 < target_dist2:
                        target_dist2 = d2
                        target = p
                if target:
                    dx = (target.x + PLAYER_W // 2) - b.x
                    dy = (target.y + PLAYER_H // 2) - b.y
                    dist = math.sqrt(dx * dx + dy * dy)
                    if dist > 1:
                        turn_power = 300 + 300000 / max(dist, 10)
                        b.vx += (dx / dist) * turn_power * dt
                        b.vy += (dy / dist) * turn_power * dt
            b.x += b.vx * dt
            b.y += b.vy * dt
            if b.x + BULLET_W < 0 or b.x > MAP_WIDTH or b.y + BULLET_H < 0 or b.y > MAP_HEIGHT:
                continue
            if now - b.spawn_time > BULLET_LIFETIME:
                continue
            if not b.explosive:
                hit_plat = False
                for plat in PLATFORMS:
                    px, py, pw, ph = plat
                    if b.x + BULLET_W > px and b.x < px + pw and b.y + BULLET_H > py and b.y < py + ph:
                        hit_plat = True
                        break
                if hit_plat:
                    continue
            hit_player = False
            for p in self.players.values():
                if p.id == b.owner_id or not p.alive:
                    continue
                pw2 = int(PLAYER_W * (2 if "giant" in p.buffs else 1))
                ph2 = int(PLAYER_H * (2 if "giant" in p.buffs else 1))
                gx_off = (pw2 - PLAYER_W) // 2
                gy_off = ph2 - PLAYER_H
                if b.x + BULLET_W > p.x - gx_off and b.x < p.x - gx_off + pw2 and b.y + BULLET_H > p.y - gy_off and b.y < p.y - gy_off + ph2:
                    if "shield" in p.buffs:
                        b.vx = -b.vx
                        b.init_vx = b.vx
                        b.vy = -b.vy
                        b.owner_id = p.id
                        b.spawn_time = now  # 重置生存时间
                        new_bullets.append(b)
                        hit_player = True
                        break
                    dmg = GUN_DAMAGE * (2 if b.explosive else 1)
                    dmg = dmg * (2 if b.giant else 1)
                    dmg = dmg // (2 if "giant" in p.buffs else 1)
                    p.hp -= dmg
                    if b.explosive:
                        for other in self.players.values():
                            if other.id == b.owner_id or not other.alive:
                                continue
                            ex = (other.x + PLAYER_W//2) - (p.x + PLAYER_W//2)
                            ey = (other.y + PLAYER_H//2) - (p.y + PLAYER_H//2)
                            if ex*ex + ey*ey <= 100*100:
                                other.hp -= GUN_DAMAGE
                                if other.hp > 0:
                                    other.vy = -300.0
                                    other.vx = 600.0 * (1 if b.vx >= 0 else -1)
                                    other.knockback_timer = 0.4
                                    other.knockback_drag = 1000.0
                                if other.hp <= 0:
                                    killer2 = self.players.get(b.owner_id)
                                    self.kill_player(other, killer2)
                    if p.hp > 0:
                        kb = 2.0 if b.giant else 1.0
                        if b.explosive:
                            p.vy = -500.0 * kb
                            p.vx = 1300.0 * (1 if b.vx >= 0 else -1) * kb
                            p.knockback_timer = 0.6
                            p.knockback_drag = 800.0 / kb
                        else:
                            p.vy = -280.0 * kb
                            p.vx = 750.0 * (1 if b.vx >= 0 else -1) * kb
                            p.knockback_timer = 0.4
                            p.knockback_drag = 1200.0 / kb
                    if p.hp <= 0:
                        killer = self.players.get(b.owner_id)
                        self.kill_player(p, killer)
                    hit_player = True
                    break
            if not hit_player:
                new_bullets.append(b)
        self.bullets = new_bullets

    def kill_player(self, p: Player, killer: Player | None):
        if not p.alive:
            return
        p.alive = False
        p.dead_timer = REBIRTH_DELAY
        p.deaths += 1
        p.melee_active = False
        if killer:
            killer.kills += 1
            msg = f"{killer.name} 淘汰了 {p.name}"
            self.kill_feed.append(msg)
            if len(self.kill_feed) > 10:
                self.kill_feed.pop(0)
            print(f"[击杀] {msg}")
        else:
            print(f"[死亡] {p.name} 掉出地图")

    def respawn(self, p: Player):
        sp = random.choice(SPAWN_POINTS)
        p.x, p.y = float(sp[0]), float(sp[1])
        p.vx = 0.0
        p.vy = 0.0
        p.hp = PLAYER_MAX_HP
        p.alive = True
        p.on_ground = False
        p.dead_timer = 0.0
        p.gun_cooldown = 0.0
        p.melee_cooldown = 0.0
        p.melee_active = False
        p.melee_hit_set.clear()
        p.can_double_jump = True
        p.knockback_timer = 0.0
        p.knockback_drag = 0.0
        p.buffs.clear()

    def check_timeout(self, now: float):
        to_remove = []
        for pid, p in self.players.items():
            if now - p.last_active > TIMEOUT_SEC:
                to_remove.append(pid)
        for pid in to_remove:
            p = self.players.get(pid)
            if p:
                self._remove_by_sock(p.sock, is_disconnect=False)

    def remove_player(self, pid: int, is_disconnect: bool = False):
        p = self.players.pop(pid, None)
        if p:
            self.sock_to_pid.pop(p.sock, None)
            if not is_disconnect:
                msg = f"{p.name} 离开了游戏"
                self.kill_feed.append(msg)
                if len(self.kill_feed) > 10:
                    self.kill_feed.pop(0)
        self.bullets = [b for b in self.bullets if b.owner_id != pid]

    @staticmethod
    def rect_collide(a: tuple, b: tuple) -> bool:
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        return ax + aw > bx and ax < bx + bw and ay + ah > by and ay < by + bh

    def build_state(self) -> dict:
        now = _clock()
        players_data = []
        for p in self.players.values():
            players_data.append({
                "id": p.id, "name": p.name,
                "x": round(p.x, 1), "y": round(p.y, 1),
                "vx": round(p.vx, 2), "vy": round(p.vy, 2),
                "facing": p.facing, "hp": max(0, p.hp),
                "kills": p.kills, "deaths": p.deaths,
                "color": list(p.color), "weapon": p.weapon,
                "alive": p.alive, "melee_active": p.melee_active,
                "buffs": {k: round(v, 1) for k, v in p.buffs.items()},
            })
        bullets_data = [{
            "x": round(b.x, 1), "y": round(b.y, 1),
            "vx": round(b.vx, 2), "vy": round(b.vy, 2),
            "owner_id": b.owner_id,
            "explosive": b.explosive,
            "giant": b.giant,
        } for b in self.bullets]
        return {
            "type": "state", "tick": now,
            "players": players_data, "bullets": bullets_data,
            "items": [{"x": round(i.x, 1), "y": round(i.y, 1)}
                      for i in self.items],
            "kill_feed": self.kill_feed[-3:],
        }

    def broadcast_state(self):
        state = self.build_state()
        self.broadcast(state)

    def start(self):
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind(("0.0.0.0", self.port))
        self.server_sock.listen(MAX_PLAYERS)
        self.server_sock.setblocking(False)

        # UDP 发现（仅广播响应，不影响游戏）
        self.disc_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.disc_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.disc_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            self.disc_sock.bind(("0.0.0.0", self.port))
            self.disc_sock.setblocking(False)
        except OSError:
            # 端口被占时发现不可用，不影响 TCP 游戏
            print("  [警告] UDP 发现端口被占用，LAN 发现不可用")
            self.disc_sock = None

        self.running = True

        print(f"=== LAN Platform Battle 服务器 (TCP) ===")
        print(f"端口: {self.port}  最大: {MAX_PLAYERS} 人")
        print(f"按 Ctrl+C 停止\n")

        while self.running:
            try:
                read_socks = [self.server_sock]
                if self.disc_sock:
                    read_socks.append(self.disc_sock)
                read_socks.extend(self._pending_socks)
                for p in self.players.values():
                    read_socks.append(p.sock)

                try:
                    readable, _, exceptional = select.select(
                        read_socks, [], read_socks, 1.0 / TICK_RATE)
                except OSError:
                    continue

                for sock in readable:
                    if sock is self.server_sock:
                        client_sock, addr = self.server_sock.accept()
                        client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        client_sock.setblocking(False)
                        self._pending_socks.append(client_sock)
                        self._recv_buf[client_sock] = b''
                        continue
                    if sock is self.disc_sock:
                        self._handle_discover(sock)
                        continue
                    while True:
                        msg = self._recv_msg(sock)
                        if msg is self._DISCONNECTED:
                            self._remove_by_sock(sock)
                            break
                        if msg is None:
                            break
                        self.handle_message(sock, msg)

                # 额外轮询：处理有残留缓冲区但没有触发 select 的 socket
                for p in list(self.players.values()):
                    if p.sock not in readable:
                        while True:
                            msg = self._recv_msg(p.sock)
                            if msg is self._DISCONNECTED:
                                self._remove_by_sock(p.sock)
                                break
                            if msg is None:
                                break
                            self.handle_message(p.sock, msg)
                for s in list(self._pending_socks):
                    if s not in readable:
                        while True:
                            msg = self._recv_msg(s)
                            if msg is self._DISCONNECTED:
                                self._remove_by_sock(s)
                                break
                            if msg is None:
                                break
                            self.handle_message(s, msg)

                for sock in exceptional:
                    self._remove_by_sock(sock)

                self.tick()
                if self.players:
                    self.broadcast_state()

            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"[服务器异常] {e}")

    def stop(self):
        self.running = False
        for p in list(self.players.values()):
            try:
                p.sock.close()
            except OSError:
                pass
        for s in self._pending_socks:
            try:
                s.close()
            except OSError:
                pass
        if self.disc_sock:
            self.disc_sock.close()
        if self.server_sock:
            self.server_sock.close()
        print("[服务器已停止]")


if __name__ == "__main__":
    port = SERVER_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"用法: python server.py [端口]")
            sys.exit(1)
    gs = GameServer(port)
    try:
        gs.start()
    except KeyboardInterrupt:
        print("\n正在关闭...")
    finally:
        gs.stop()
