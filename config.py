"""
LAN Platform Battle — 配置常量
2D 平台跳跃 + 近战/枪械 多人混战
"""
import math

# === 网络 ===
SERVER_PORT = 12345
MAX_PLAYERS = 4
TIMEOUT_SEC = 5.0       # 客户端超时断开秒数
TICK_RATE = 60          # 服务器更新频率 (次/秒)

# === 窗口 ===
WIDTH = 1024
HEIGHT = 600
FPS = 60
TITLE = "LAN Platform Battle"

# === 物理 ===
GRAVITY = 0.65
MAX_FALL_SPEED = 15.0
PLAYER_SPEED = 10.0
JUMP_SPEED = -13.0     # 负值 = 向上 (pygame y 轴向下)
# 二段跳：在空中再按一次跳跃触发

# === 玩家 ===
PLAYER_W = 24
PLAYER_H = 34
PLAYER_MAX_HP = 100
REBIRTH_DELAY = 3.0

# === 近战 ===
MELEE_DAMAGE = 25
MELEE_COOLDOWN = 0.5   # 秒
MELEE_RANGE_W = 114   # 1.5x 横向
MELEE_RANGE_H = 30    # 0.5x 纵向
MELEE_ACTIVE_TIME = 0.15  # 攻击判定持续秒数

# === 枪械 ===
GUN_DAMAGE = 15
GUN_COOLDOWN = 0.5
BULLET_W = 8
BULLET_H = 4
BULLET_SPEED = 14.0
BULLET_LIFETIME = 1.5

# === 地图 ===
MAP_WIDTH = 1024
MAP_HEIGHT = 600

# 平台定义: (x, y, width, height)
# y 为平台顶边坐标
PLATFORMS = [
    (0,   568, 1024, 16),  # 地面
    (60,  460, 160, 14),
    (804, 460, 160, 14),
    (380, 380, 264, 14),   # 中高台
    (120, 310, 160, 14),
    (744, 310, 160, 14),
    (340, 230, 344, 14),   # 顶台
    (120, 150, 160, 14),
    (744, 150, 160, 14),
]

# 出生点 (x, y) — 玩家脚底位置
SPAWN_POINTS = [
    (120, -34),
    (380, -34),
    (560, -34),
    (820, -34),
]

# === 颜色 ===
COLOR_BG = (28, 28, 48)
COLOR_GRID = (40, 40, 60)
COLOR_PLATFORM = (72, 72, 112)
COLOR_PLATFORM_TOP = (110, 110, 170)
COLOR_PLATFORM_BORDER = (55, 55, 85)
COLOR_HP_BAR_BG = (50, 50, 50)
COLOR_HP_BAR = (50, 220, 50)
COLOR_HP_BAR_LOW = (220, 50, 50)
COLOR_BULLET = (255, 200, 50)
COLOR_NAME = (240, 240, 240)
COLOR_HUD = (200, 200, 200)
COLOR_KILL_MSG = (255, 240, 100)
COLOR_CONNECTED = (100, 240, 100)
COLOR_DISCONNECTED = (240, 100, 100)

PLAYER_COLORS = [
    (255, 80, 80),
    (80, 130, 255),
    (80, 230, 80),
    (255, 200, 50),
    (255, 80, 200),
    (80, 230, 230),
    (255, 150, 50),
    (180, 100, 255),
]

def get_color(idx: int):
    return PLAYER_COLORS[idx % len(PLAYER_COLORS)]
