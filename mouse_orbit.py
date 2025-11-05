"""Sophisticated AFK mouse movement script for Windows 11."""

import ctypes
import ctypes.wintypes as wintypes
import math
import queue
import random
import threading
import time
from dataclasses import dataclass
from typing import Callable

import tkinter as tk
from tkinter import messagebox, ttk




try:
    ULONG_PTR = wintypes.ULONG_PTR  # type: ignore[attr-defined]
except AttributeError:  # pragma: no cover - fallback for older Python builds
    if ctypes.sizeof(ctypes.c_void_p) == ctypes.sizeof(ctypes.c_ulonglong):
        ULONG_PTR = ctypes.c_ulonglong
    else:
        ULONG_PTR = ctypes.c_ulong


INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
KEYEVENTF_KEYUP = 0x0002
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
VK_Q = 0x51
VK_W = 0x57
VK_A = 0x41
VK_S = 0x53
VK_D = 0x44
VK_B = 0x42


@dataclass
class OrbitConfig:
    base_radius: float = 220.0
    interval: float = 0.01
    angular_velocity: float = 1.25
    jitter: float = 6.0
    startup_delay: float = 5.0
    enable_keyboard_steps: bool = True
    enable_saccades: bool = True
    enable_b_hold: bool = False
    step_interval_range: tuple[float, float] = (9.0, 20.0)
    step_hold_range: tuple[float, float] = (0.045, 0.12)
    saccade_interval_range: tuple[float, float] = (4.5, 9.5)
    screen_margin: float = 2.0
    jitter_random_scale: float = 0.2
    wobble_scale: float = 0.4
    wobble_vertical_bias: float = 0.6
    spin_noise_scale: float = 0.16


APP_NAME = "AutoAFK Control Suite"
APP_TAGLINE = "intelligent idle choreography that feels human"
MENU_WIDTH = 72

PRIMARY_BG = "#0f172a"
PANEL_BG = "#111c2e"
CARD_BG = "#1b2436"
ACCENT_COLOR = "#7c3aed"
ACCENT_LIGHT = "#a855f7"
DANGER_COLOR = "#ef4444"
TEXT_BRIGHT = "#e2e8f0"
TEXT_MUTED = "#94a3b8"


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint), ("union", INPUTUNION)]


def _catmull_rom(p0: float, p1: float, p2: float, p3: float, t: float) -> float:
    t2 = t * t
    t3 = t2 * t
    return 0.5 * (
        (2.0 * p1)
        + (-p0 + p2) * t
        + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t2
        + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * t3
    )


class SmoothNoise:
    """Generates smooth pseudo-random curves using cached value noise."""

    def __init__(self, seed: int) -> None:
        self._rand = random.Random(seed)
        self._cache: dict[int, float] = {}

    def _value(self, index: int) -> float:
        if index not in self._cache:
            self._cache[index] = self._rand.uniform(-1.0, 1.0)
        return self._cache[index]

    def sample(self, position: float) -> float:
        base = math.floor(position)
        t = position - base
        p0 = self._value(base - 1)
        p1 = self._value(base)
        p2 = self._value(base + 1)
        p3 = self._value(base + 2)
        return _catmull_rom(p0, p1, p2, p3, t)


class InputController:
    """Dispatches relative mouse movement using SendInput."""

    def __init__(self) -> None:
        self._user32 = ctypes.windll.user32
        try:
            self._user32.SetProcessDPIAware()
        except AttributeError:
            pass

    def move(self, dx: float, dy: float) -> None:
        ix = int(round(dx))
        iy = int(round(dy))
        if ix == 0 and iy == 0:
            return
        mouse_input = MOUSEINPUT(dx=ix, dy=iy, mouseData=0, dwFlags=MOUSEEVENTF_MOVE, time=0, dwExtraInfo=0)
        packet = INPUT(type=INPUT_MOUSE, union=INPUTUNION(mi=mouse_input))
        sent = self._user32.SendInput(1, ctypes.byref(packet), ctypes.sizeof(INPUT))
        if sent != 1:
            raise ctypes.WinError()

    def position(self) -> tuple[float, float]:
        point = POINT()
        if not self._user32.GetCursorPos(ctypes.byref(point)):
            raise ctypes.WinError()
        return float(point.x), float(point.y)


class KeyboardController:
    """Sends quick virtual-key taps using SendInput."""

    def __init__(self) -> None:
        self._user32 = ctypes.windll.user32

    def _send(self, key_code: int, flags: int) -> None:
        keyboard_input = KEYBDINPUT(wVk=key_code, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
        packet = INPUT(type=INPUT_KEYBOARD, union=INPUTUNION(ki=keyboard_input))
        sent = self._user32.SendInput(1, ctypes.byref(packet), ctypes.sizeof(INPUT))
        if sent != 1:
            raise ctypes.WinError()

    def press(self, key_code: int) -> None:
        self._send(key_code, 0)

    def release(self, key_code: int) -> None:
        self._send(key_code, KEYEVENTF_KEYUP)


class HotkeyMonitor(threading.Thread):
    """Listens for Ctrl+Alt+Q using RegisterHotKey."""

    def __init__(self, on_trigger: threading.Event) -> None:
        super().__init__(daemon=True)
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._on_trigger = on_trigger
        self._thread_id = 0
        self._stop_event = threading.Event()

    def run(self) -> None:
        self._thread_id = self._kernel32.GetCurrentThreadId()
        if not self._user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT, VK_Q):
            raise ctypes.WinError()
        msg = wintypes.MSG()
        try:
            while not self._stop_event.is_set():
                result = self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise ctypes.WinError()
                if msg.message == WM_HOTKEY and msg.wParam == 1:
                    self._on_trigger.set()
                    self._stop_event.set()
                    break
        finally:
            self._user32.UnregisterHotKey(None, 1)

    def stop(self) -> None:
        self._stop_event.set()
        if self.is_alive() and not self._thread_id:
            while self.is_alive() and not self._thread_id:
                time.sleep(0.01)
        if self._thread_id:
            self._user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)


def _screen_bounds() -> tuple[int, int]:
    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDPIAware()
    except AttributeError:
        pass
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


class OrbitMover:
    """Moves the mouse along a smooth orbit until the exit combo is pressed."""

    def __init__(self, config: OrbitConfig) -> None:
        self._config = config
        self.base_radius = config.base_radius
        self.interval = config.interval
        self.angular_velocity = config.angular_velocity
        self.jitter = config.jitter
        self._enable_steps = config.enable_keyboard_steps
        self._enable_saccades = config.enable_saccades
        self._enable_b_hold = config.enable_b_hold
        self._step_interval_range = config.step_interval_range
        self._step_hold_range = config.step_hold_range
        self._saccade_interval_range = config.saccade_interval_range
        self._input = InputController()
        self._keyboard = KeyboardController()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._move_loop, daemon=True)
        self._hotkeys = HotkeyMonitor(self._stop_event)
        self._screen_w, self._screen_h = _screen_bounds()
        seed = random.randrange(1 << 30)
        self._noise_radius = SmoothNoise(seed ^ 0x13579BDF)
        self._noise_tilt = SmoothNoise(seed ^ 0x2468ACE0)
        self._noise_precession = SmoothNoise(seed ^ 0x55AA55AA)
        self._noise_center_x = SmoothNoise(seed ^ 0x92E1103A)
        self._noise_center_y = SmoothNoise(seed ^ 0x7F4A1BC2)
        self._noise_spin = SmoothNoise(seed ^ 0x6C3D2E1F)
        self._noise_wobble = SmoothNoise(seed ^ 0x0A0B0C0D)
        self._step_keys = [VK_W, VK_A, VK_S, VK_D]
        self._active_step_key: int | None = None
        self._step_hold_remaining = 0.0
        self._time_until_step = self._next_step_interval() if self._enable_steps else math.inf
        self._b_interval = 180.0
        self._b_duration = 4.0
        self._b_time_until_press = self._b_interval if self._enable_b_hold else math.inf
        self._b_hold_remaining = 0.0
        self._b_active = False

    def _next_step_interval(self) -> float:
        low, high = self._step_interval_range
        return random.uniform(low, high)

    def _next_step_hold(self) -> float:
        low, high = self._step_hold_range
        return random.uniform(low, high)

    def start(self) -> None:
        self._hotkeys.start()
        self._thread.start()

    def wait(self) -> None:
        self._thread.join()
        self._hotkeys.join()

    def stop(self) -> None:
        if not self._stop_event.is_set():
            self._stop_event.set()
        self._hotkeys.stop()
        if self._active_step_key is not None:
            try:
                self._keyboard.release(self._active_step_key)
            except OSError:
                pass
            self._active_step_key = None
        if self._b_active:
            try:
                self._keyboard.release(VK_B)
            except OSError:
                pass
            self._b_active = False
        self._b_time_until_press = self._b_interval if self._enable_b_hold else math.inf

    def _move_loop(self) -> None:
        theta = random.random() * math.tau
        phase = random.random() * math.tau
        bias = random.uniform(0.05, 0.18)
        prev_x = 0.0
        prev_y = 0.0
        elapsed = 0.0
        cycle_blend = 1.0
        radius_gain = 1.0
        target_radius_gain = 1.0 + random.uniform(-0.05, 0.05)
        tilt_offset = 0.0
        target_tilt_offset = random.uniform(-0.3, 0.3)
        center_offset_x = 0.0
        center_offset_y = 0.0
        target_center_offset_x = 0.0
        target_center_offset_y = 0.0
        saccade_offset_x = 0.0
        saccade_offset_y = 0.0
        saccade_decay = 0.0
        time_until_saccade = (
            random.uniform(*self._saccade_interval_range)
            if self._enable_saccades
            else math.inf
        )
        try:
            while not self._stop_event.is_set():
                elapsed += self.interval
                slow_time = elapsed
                if self._enable_steps:
                    self._time_until_step -= self.interval
                    if self._active_step_key is None and self._time_until_step <= 0.0:
                        candidate = random.choice(self._step_keys)
                        try:
                            self._keyboard.press(candidate)
                        except OSError:
                            self._time_until_step = self._next_step_interval()
                        else:
                            self._active_step_key = candidate
                            self._step_hold_remaining = self._next_step_hold()
                            self._time_until_step = self._next_step_interval()
                    elif self._active_step_key is not None:
                        self._step_hold_remaining -= self.interval
                        if self._step_hold_remaining <= 0.0:
                            try:
                                self._keyboard.release(self._active_step_key)
                            except OSError:
                                pass
                            self._active_step_key = None
                elif self._active_step_key is not None:
                    try:
                        self._keyboard.release(self._active_step_key)
                    except OSError:
                        pass
                    self._active_step_key = None
                if self._enable_b_hold:
                    self._b_time_until_press -= self.interval
                    if not self._b_active and self._b_time_until_press <= 0.0:
                        try:
                            self._keyboard.press(VK_B)
                        except OSError:
                            self._b_time_until_press = self._b_interval
                        else:
                            self._b_active = True
                            self._b_hold_remaining = self._b_duration
                    elif self._b_active:
                        self._b_hold_remaining -= self.interval
                        if self._b_hold_remaining <= 0.0:
                            try:
                                self._keyboard.release(VK_B)
                            except OSError:
                                pass
                            self._b_active = False
                            self._b_time_until_press = self._b_interval
                elif self._b_active:
                    try:
                        self._keyboard.release(VK_B)
                    except OSError:
                        pass
                    self._b_active = False
                    self._b_time_until_press = math.inf
                if cycle_blend < 1.0:
                    cycle_blend = min(1.0, cycle_blend + self.interval * 0.35)
                smoothing = 0.5 - 0.5 * math.cos(math.pi * cycle_blend)
                gain = radius_gain + (target_radius_gain - radius_gain) * smoothing
                blended_tilt = tilt_offset + (target_tilt_offset - tilt_offset) * smoothing
                blended_center_x = center_offset_x + (target_center_offset_x - center_offset_x) * smoothing
                blended_center_y = center_offset_y + (target_center_offset_y - center_offset_y) * smoothing
                dynamic_radius = self.base_radius * (0.78 + 0.22 * math.sin(theta * 2.7 + phase))
                radius_offset = 0.12 * self.base_radius * self._noise_radius.sample(slow_time * 0.12)
                vertical_scale = 0.65 + 0.35 * math.cos(theta * 1.4 + phase * 0.5 + blended_tilt + 0.4 * self._noise_tilt.sample(slow_time * 0.08))
                precession = 0.9 * self._noise_precession.sample(slow_time * 0.05)
                cos_prec = math.cos(precession)
                sin_prec = math.sin(precession)
                base_x = (dynamic_radius + radius_offset) * math.cos(theta)
                base_y = (dynamic_radius * vertical_scale) * math.sin(theta)
                base_x *= gain
                base_y *= gain
                rotated_x = cos_prec * base_x - sin_prec * base_y
                rotated_y = sin_prec * base_x + cos_prec * base_y
                drift_x = 0.14 * self.base_radius * self._noise_center_x.sample(slow_time * 0.03)
                drift_y = 0.14 * self.base_radius * self._noise_center_y.sample(slow_time * 0.028)
                wobble = self._config.wobble_scale * self.jitter * self._noise_wobble.sample(slow_time * 0.25)
                fine_x = rotated_x + drift_x + blended_center_x + wobble
                fine_y = rotated_y + drift_y + blended_center_y + self._config.wobble_vertical_bias * wobble
                fine_x += (random.random() - 0.5) * self.jitter * self._config.jitter_random_scale
                fine_y += (random.random() - 0.5) * self.jitter * self._config.jitter_random_scale
                if self._enable_saccades:
                    time_until_saccade -= self.interval
                    if time_until_saccade <= 0.0:
                        saccade_offset_x = random.uniform(-8.0, 8.0)
                        saccade_offset_y = random.uniform(-5.0, 5.0)
                        saccade_decay = 0.0
                        time_until_saccade = random.uniform(*self._saccade_interval_range)
                else:
                    saccade_offset_x = 0.0
                    saccade_offset_y = 0.0
                if self._enable_saccades and (saccade_offset_x or saccade_offset_y):
                    decay_factor = math.exp(-6.0 * saccade_decay)
                    fine_x += saccade_offset_x * decay_factor
                    fine_y += saccade_offset_y * decay_factor
                    saccade_decay += self.interval
                    if decay_factor < 0.02:
                        saccade_offset_x = 0.0
                        saccade_offset_y = 0.0
                x = fine_x
                y = fine_y
                dx = x - prev_x
                dy = y - prev_y
                prev_x = x
                prev_y = y
                current_x, current_y = self._input.position()
                target_x, target_y = current_x + dx, current_y + dy
                clamped_x, clamped_y = self._clamp_to_screen(target_x, target_y)
                self._input.move(clamped_x - current_x, clamped_y - current_y)
                spin_variation = 1.0 + bias * math.sin(theta * 0.9 + phase)
                spin_variation += self._config.spin_noise_scale * self._noise_spin.sample(slow_time * 0.11)
                theta += self.angular_velocity * self.interval * spin_variation
                if theta > math.tau:
                    theta -= math.tau
                    radius_gain = target_radius_gain
                    tilt_offset = target_tilt_offset
                    center_offset_x = target_center_offset_x
                    center_offset_y = target_center_offset_y
                    target_radius_gain = 1.0 + random.uniform(-0.05, 0.05)
                    target_tilt_offset = random.uniform(-0.3, 0.3)
                    target_center_offset_x = random.uniform(-1.0, 1.0) * 0.1 * self.base_radius
                    target_center_offset_y = random.uniform(-1.0, 1.0) * 0.1 * self.base_radius
                    phase += random.uniform(-0.25, 0.25)
                    bias = random.uniform(0.04, 0.19)
                    cycle_blend = 0.0
                time.sleep(self.interval)
        finally:
            if self._active_step_key is not None:
                try:
                    self._keyboard.release(self._active_step_key)
                except OSError:
                    pass
                self._active_step_key = None
            if self._b_active:
                try:
                    self._keyboard.release(VK_B)
                except OSError:
                    pass
                self._b_active = False

    def _clamp_to_screen(self, x: float, y: float) -> tuple[float, float]:
        margin = self._config.screen_margin
        max_x = max(margin, self._screen_w - margin)
        max_y = max(margin, self._screen_h - margin)
        clamped_x = max(margin, min(max_x, x))
        clamped_y = max(margin, min(max_y, y))
        return clamped_x, clamped_y


class AutoAFKApp:
    """High polish Tkinter control deck for the AutoAFK choreography."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.config = OrbitConfig()
        self.vars: dict[str, tk.Variable] = {}
        self._interactive_widgets: list[ttk.Widget] = []
        self._value_labels: dict[str, tk.StringVar] = {}
        self._value_formatters: dict[str, Callable[[float], str]] = {}
        self._queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._session_thread: threading.Thread | None = None
        self._session_stop_event = threading.Event()
        self._mover: OrbitMover | None = None
        self._summary_after_id: str | None = None
        self.status_var = tk.StringVar(value="Session idle.")

        self.root.title(APP_NAME)
        self.root.minsize(1180, 660)
        self.root.geometry("1280x720")
        self.root.state("zoomed")

        self._build_style()
        self._init_vars()
        self._build_ui()
        self._refresh_summary()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_queue()

    def _build_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        self.root.configure(bg=PRIMARY_BG)
        style.configure("TFrame", background=PRIMARY_BG)
        style.configure("TLabel", background=PRIMARY_BG, foreground=TEXT_BRIGHT, font=("Segoe UI", 10))
        style.configure("TCheckbutton", background=PRIMARY_BG, foreground=TEXT_BRIGHT)
        style.configure("TButton", font=("Segoe UI", 10), padding=8)

        style.configure("AutoAFK.TFrame", background=PRIMARY_BG)
        style.configure("Card.TFrame", background=CARD_BG)
        style.configure("Card.TLabelframe", background=CARD_BG, foreground=TEXT_BRIGHT, padding=18)
        style.configure(
            "Card.TLabelframe.Label",
            background=CARD_BG,
            foreground=TEXT_BRIGHT,
            font=("Segoe UI", 11, "bold"),
        )
        style.configure("Header.TLabel", font=("Segoe UI", 22, "bold"), foreground=TEXT_BRIGHT, background=PRIMARY_BG)
        style.configure("Subtitle.TLabel", font=("Segoe UI", 12), foreground=TEXT_MUTED, background=PRIMARY_BG)
        style.configure("Section.TLabel", font=("Segoe UI Semibold", 10), foreground=TEXT_BRIGHT, background=CARD_BG)
        style.configure("Muted.TLabel", font=("Segoe UI", 9), foreground=TEXT_MUTED, background=CARD_BG)
        style.configure("Status.TLabel", font=("Segoe UI", 11), foreground=TEXT_BRIGHT, background=CARD_BG)
        style.configure(
            "Accent.TButton",
            font=("Segoe UI Semibold", 11),
            padding=10,
            background=ACCENT_COLOR,
            foreground=TEXT_BRIGHT,
        )
        style.map("Accent.TButton", background=[("active", ACCENT_LIGHT), ("pressed", ACCENT_LIGHT)])
        style.configure(
            "Danger.TButton",
            font=("Segoe UI Semibold", 11),
            padding=10,
            background=DANGER_COLOR,
            foreground=TEXT_BRIGHT,
        )
        style.map("Danger.TButton", background=[("active", "#f87171"), ("pressed", "#dc2626")])
        style.configure("AutoAFK.TCheckbutton", background=CARD_BG, foreground=TEXT_BRIGHT, font=("Segoe UI", 10))
        style.map("AutoAFK.TCheckbutton", foreground=[("disabled", "#475569")])
        style.configure(
            "Accent.Horizontal.TProgressbar",
            troughcolor=PANEL_BG,
            background=ACCENT_COLOR,
            bordercolor=PANEL_BG,
            lightcolor=ACCENT_LIGHT,
            darkcolor=ACCENT_COLOR,
        )

    def _init_vars(self) -> None:
        cfg = self.config
        self._bind_var("enable_steps", tk.BooleanVar(value=cfg.enable_keyboard_steps))
        self._bind_var("enable_saccades", tk.BooleanVar(value=cfg.enable_saccades))
        self._bind_var("enable_b_hold", tk.BooleanVar(value=cfg.enable_b_hold))
        self._bind_var("base_radius", tk.DoubleVar(value=cfg.base_radius))
        self._bind_var("angular_velocity", tk.DoubleVar(value=cfg.angular_velocity))
        self._bind_var("jitter", tk.DoubleVar(value=cfg.jitter))
        self._bind_var("startup_delay", tk.DoubleVar(value=cfg.startup_delay))
        self._bind_var("screen_margin", tk.DoubleVar(value=cfg.screen_margin))
        self._bind_var("step_interval_min", tk.DoubleVar(value=cfg.step_interval_range[0]))
        self._bind_var("step_interval_max", tk.DoubleVar(value=cfg.step_interval_range[1]))
        self._bind_var("step_hold_min", tk.DoubleVar(value=cfg.step_hold_range[0]))
        self._bind_var("step_hold_max", tk.DoubleVar(value=cfg.step_hold_range[1]))
        self._bind_var("saccade_interval_min", tk.DoubleVar(value=cfg.saccade_interval_range[0]))
        self._bind_var("saccade_interval_max", tk.DoubleVar(value=cfg.saccade_interval_range[1]))

    def _bind_var(self, key: str, var: tk.Variable) -> None:
        self.vars[key] = var
        var.trace_add("write", lambda *_args, name=key: self._on_var_change(name))

    def _on_var_change(self, key: str) -> None:
        self._update_value_label(key)
        self._refresh_summary_debounced()

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, style="AutoAFK.TFrame", padding=(28, 24))
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer, style="AutoAFK.TFrame")
        header.pack(fill="x", pady=(0, 20))
        ttk.Label(header, text=APP_NAME, style="Header.TLabel").pack(anchor="w")
        ttk.Label(header, text=APP_TAGLINE, style="Subtitle.TLabel").pack(anchor="w", pady=(4, 0))
        tk.Frame(header, height=3, bg=ACCENT_COLOR, bd=0, highlightthickness=0).pack(fill="x", pady=(18, 0))

        content = ttk.Frame(outer, style="AutoAFK.TFrame")
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1, uniform="col")
        content.columnconfigure(1, weight=1, uniform="col")
        content.columnconfigure(2, weight=1, uniform="col")

        left_panel = ttk.Frame(content, style="Card.TFrame", padding=(24, 22))
        left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        middle_panel = ttk.Frame(content, style="Card.TFrame", padding=(24, 22))
        middle_panel.grid(row=0, column=1, sticky="nsew", padx=12)

        right_panel = ttk.Frame(content, style="Card.TFrame", padding=(24, 22))
        right_panel.grid(row=0, column=2, sticky="nsew", padx=(12, 0))

        left_panel.columnconfigure(0, weight=1)
        middle_panel.columnconfigure(0, weight=1)
        right_panel.columnconfigure(0, weight=1)

        self._create_personality_section(left_panel)
        self._create_timing_section(left_panel)

        self._create_orbit_section(middle_panel)
        self._create_cadence_section(middle_panel)

        self._build_summary_panel(right_panel)
        self._build_controls_panel(right_panel)

    def _create_personality_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Session Personality", style="Card.TLabelframe")
        frame.pack(fill="x", pady=(0, 18))

        footwork = ttk.Checkbutton(
            frame,
            text="Walkabout footwork (subtle WASD taps)",
            variable=self.vars["enable_steps"],
            style="AutoAFK.TCheckbutton",
        )
        footwork.pack(anchor="w")
        self._interactive_widgets.append(footwork)
        ttk.Label(
            frame,
            text="Adds occasional micro-steps to keep avatars breathing.",
            style="Muted.TLabel",
            wraplength=320,
        ).pack(anchor="w", padx=(24, 0), pady=(2, 8))

        saccades = ttk.Checkbutton(
            frame,
            text="Micro-saccade camera jitter",
            variable=self.vars["enable_saccades"],
            style="AutoAFK.TCheckbutton",
        )
        saccades.pack(anchor="w", pady=(4, 0))
        self._interactive_widgets.append(saccades)
        ttk.Label(
            frame,
            text="Subtle lens corrections for lifelike curiosity.",
            style="Muted.TLabel",
            wraplength=320,
        ).pack(anchor="w", padx=(24, 0), pady=(2, 2))

        b_hold = ttk.Checkbutton(
            frame,
            text="Utility stance (hold 'B' every 3 minutes)",
            variable=self.vars["enable_b_hold"],
            style="AutoAFK.TCheckbutton",
        )
        b_hold.pack(anchor="w", pady=(6, 0))
        self._interactive_widgets.append(b_hold)
        ttk.Label(
            frame,
            text="Keeps the B key pressed for 4 seconds once per 3-minute cycle.",
            style="Muted.TLabel",
            wraplength=320,
        ).pack(anchor="w", padx=(24, 0), pady=(2, 0))

    def _create_orbit_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Orbit Dynamics", style="Card.TLabelframe")
        frame.pack(fill="x", pady=(0, 18))

        self._build_slider(frame, "Orbit radius", "base_radius", 60.0, 900.0, 1.0, lambda v: f"{v:.0f} px")
        self._build_slider(frame, "Camera pace", "angular_velocity", 0.25, 2.5, 0.01, lambda v: f"{v:.2f} rad/s")
        self._build_slider(frame, "Motion texture", "jitter", 0.0, 14.0, 0.1, lambda v: f"{v:.1f} px")

    def _create_timing_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Session Envelope", style="Card.TLabelframe")
        frame.pack(fill="x", pady=(0, 18))

        self._build_slider(frame, "Startup countdown", "startup_delay", 0.0, 30.0, 0.1, lambda v: f"{v:.1f} s")
        self._build_slider(frame, "Screen margin", "screen_margin", 1.0, 80.0, 1.0, lambda v: f"{v:.0f} px")

    def _create_cadence_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Cadence Windows", style="Card.TLabelframe")
        frame.pack(fill="x")

        self._build_range_inputs(
            frame,
            "Walkabout cadence window (seconds)",
            "step_interval_min",
            "step_interval_max",
            2.0,
            40.0,
            0.1,
        )
        self._build_range_inputs(
            frame,
            "Walkabout tap length window (seconds)",
            "step_hold_min",
            "step_hold_max",
            0.02,
            0.5,
            0.01,
        )
        self._build_range_inputs(
            frame,
            "Micro-saccade interval window (seconds)",
            "saccade_interval_min",
            "saccade_interval_max",
            1.5,
            20.0,
            0.1,
        )

    def _build_slider(
        self,
        parent: ttk.Frame,
        title: str,
        key: str,
        minimum: float,
        maximum: float,
        resolution: float,
        formatter: Callable[[float], str],
    ) -> None:
        container = ttk.Frame(parent, style="Card.TFrame")
        container.pack(fill="x", pady=(6, 12))

        header = ttk.Frame(container, style="Card.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text=title, style="Section.TLabel").pack(side="left")
        value_var = tk.StringVar()
        ttk.Label(header, textvariable=value_var, style="Section.TLabel").pack(side="right")

        scale = ttk.Scale(
            container,
            from_=minimum,
            to=maximum,
            orient="horizontal",
            variable=self.vars[key],
            command=lambda _value, name=key: self._on_slider(name),
        )
        scale.pack(fill="x", pady=(8, 4))
        self._interactive_widgets.append(scale)

        spin = ttk.Spinbox(
            container,
            from_=minimum,
            to=maximum,
            increment=resolution,
            textvariable=self.vars[key],
            width=8,
            justify="center",
        )
        spin.pack(anchor="e")
        spin.configure(command=self._refresh_summary_debounced)
        spin.bind("<FocusOut>", lambda _event: self._refresh_summary_debounced())
        spin.bind("<Return>", lambda event: (event.widget.selection_clear(), self._refresh_summary_debounced()))
        self._interactive_widgets.append(spin)

        self._value_labels[key] = value_var
        self._value_formatters[key] = formatter
        self._update_value_label(key)

    def _build_range_inputs(
        self,
        parent: ttk.Frame,
        title: str,
        min_key: str,
        max_key: str,
        minimum: float,
        maximum: float,
        increment: float,
    ) -> None:
        container = ttk.Frame(parent, style="Card.TFrame")
        container.pack(fill="x", pady=(6, 10))
        ttk.Label(container, text=title, style="Section.TLabel", wraplength=320).grid(row=0, column=0, columnspan=2, sticky="w")

        min_spin = ttk.Spinbox(
            container,
            from_=minimum,
            to=maximum,
            increment=increment,
            textvariable=self.vars[min_key],
            width=8,
            justify="center",
        )
        min_spin.grid(row=1, column=0, sticky="we", pady=(6, 0))

        max_spin = ttk.Spinbox(
            container,
            from_=minimum,
            to=maximum,
            increment=increment,
            textvariable=self.vars[max_key],
            width=8,
            justify="center",
        )
        max_spin.grid(row=1, column=1, sticky="we", padx=(12, 0), pady=(6, 0))

        container.columnconfigure(0, weight=1)
        container.columnconfigure(1, weight=1)

        for spin in (min_spin, max_spin):
            spin.configure(command=self._refresh_summary_debounced)
            spin.bind("<FocusOut>", lambda _event: self._refresh_summary_debounced())
            spin.bind("<Return>", lambda event: (event.widget.selection_clear(), self._refresh_summary_debounced()))
            self._interactive_widgets.append(spin)

        ttk.Label(container, text="Values auto-sort on launch.", style="Muted.TLabel", wraplength=320).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def _build_summary_panel(self, parent: ttk.Frame) -> None:
        badge = tk.Label(
            parent,
            text="SESSION BLUEPRINT",
            font=("Segoe UI Semibold", 9),
            bg=ACCENT_COLOR,
            fg=TEXT_BRIGHT,
            padx=12,
            pady=2,
            borderwidth=0,
            highlightthickness=0,
        )
        badge.pack(anchor="w")
        ttk.Label(parent, text="Session Blueprint", style="Section.TLabel").pack(anchor="w", pady=(10, 0))

        self.summary_text = tk.Text(
            parent,
            height=15,
            wrap="word",
            state="disabled",
            background=CARD_BG,
            foreground=TEXT_BRIGHT,
            bd=0,
            highlightthickness=0,
            font=("Segoe UI", 11),
            padx=10,
            pady=8,
            insertbackground=TEXT_BRIGHT,
        )
        self.summary_text.tag_configure("detail", foreground=TEXT_MUTED)
        self.summary_text.pack(fill="both", expand=True, pady=(8, 16))

        telemetry = ttk.LabelFrame(parent, text="Live Telemetry", style="Card.TLabelframe", padding=(18, 14))
        telemetry.pack(fill="x", pady=(0, 4))

        ttk.Label(telemetry, textvariable=self.status_var, style="Status.TLabel", wraplength=320).pack(fill="x")
        self.progress = ttk.Progressbar(telemetry, style="Accent.Horizontal.TProgressbar", mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(12, 0))
        ttk.Label(telemetry, text="Hotkey: Ctrl + Alt + Q", style="Muted.TLabel").pack(anchor="w", pady=(12, 2))
        ttk.Label(telemetry, text="Console fallback: Ctrl + C", style="Muted.TLabel").pack(anchor="w")

    def _build_controls_panel(self, parent: ttk.Frame) -> None:
        info = ttk.Label(
            parent,
            text="Sessions launch with a cinematic countdown before motion begins.",
            style="Muted.TLabel",
            wraplength=320,
        )
        info.pack(fill="x", pady=(0, 12))

        control_bar = ttk.Frame(parent, style="Card.TFrame")
        control_bar.pack(fill="x", pady=(4, 0))
        control_bar.columnconfigure(0, weight=1)
        control_bar.columnconfigure(1, weight=1)

        self.start_button = ttk.Button(control_bar, text="Engage Orbit", style="Accent.TButton", command=self._start_session)
        self.start_button.grid(row=0, column=0, sticky="ew")

        self.stop_button = ttk.Button(control_bar, text="Abort Session", style="Danger.TButton", command=self._stop_session)
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(12, 0))
        self.stop_button.state(["disabled"])

    def _on_slider(self, key: str) -> None:
        self._update_value_label(key)
        self._refresh_summary_debounced()

    def _update_value_label(self, key: str) -> None:
        if key in self._value_labels:
            try:
                value = float(self.vars[key].get())
            except (TypeError, tk.TclError):
                return
            formatter = self._value_formatters.get(key)
            if formatter:
                self._value_labels[key].set(formatter(value))

    def _refresh_summary_debounced(self) -> None:
        if self._summary_after_id is not None:
            self.root.after_cancel(self._summary_after_id)
        self._summary_after_id = self.root.after(120, self._refresh_summary)

    def _refresh_summary(self) -> None:
        self._summary_after_id = None
        snapshot = self._build_config_from_vars()

        lines = [
            f"- Orbit radius: {snapshot.base_radius:.0f} px",
            f"- Camera pace: {snapshot.angular_velocity:.2f} rad/s",
            f"- Motion texture: {snapshot.jitter:.1f} px",
            f"- Startup countdown: {snapshot.startup_delay:.1f} s",
            f"- Screen margin: {snapshot.screen_margin:.0f} px",
        ]

        if snapshot.enable_keyboard_steps:
            lines.extend(
                [
                    "- Walkabout footwork: Enabled",
                    f"    cadence window: {snapshot.step_interval_range[0]:.1f}s – {snapshot.step_interval_range[1]:.1f}s",
                    f"    tap length window: {snapshot.step_hold_range[0]:.2f}s – {snapshot.step_hold_range[1]:.2f}s",
                ]
            )
        else:
            lines.append("- Walkabout footwork: Disabled")

        if snapshot.enable_saccades:
            lines.extend(
                [
                    "- Micro-saccades: Enabled",
                    f"    interval window: {snapshot.saccade_interval_range[0]:.1f}s – {snapshot.saccade_interval_range[1]:.1f}s",
                ]
            )
        else:
            lines.append("- Micro-saccades: Disabled")

        if snapshot.enable_b_hold:
            lines.extend(
                [
                    "- Utility stance: Enabled",
                    "    B hold cadence: 4.0s every 180s",
                ]
            )
        else:
            lines.append("- Utility stance: Disabled")

        self.summary_text.configure(state="normal")
        self.summary_text.delete("1.0", "end")
        for line in lines:
            if line.startswith("    "):
                self.summary_text.insert("end", line + "\n", "detail")
            else:
                self.summary_text.insert("end", line + "\n")
        self.summary_text.configure(state="disabled")

    def _build_config_from_vars(self) -> OrbitConfig:
        cfg = OrbitConfig()
        cfg.enable_keyboard_steps = bool(self.vars["enable_steps"].get())
        cfg.enable_saccades = bool(self.vars["enable_saccades"].get())
        cfg.enable_b_hold = bool(self.vars["enable_b_hold"].get())
        cfg.base_radius = float(self.vars["base_radius"].get())
        cfg.angular_velocity = float(self.vars["angular_velocity"].get())
        cfg.jitter = float(self.vars["jitter"].get())
        cfg.startup_delay = float(self.vars["startup_delay"].get())
        cfg.screen_margin = float(self.vars["screen_margin"].get())
        cfg.step_interval_range = tuple(
            sorted(
                (
                    float(self.vars["step_interval_min"].get()),
                    float(self.vars["step_interval_max"].get()),
                )
            )
        )
        cfg.step_hold_range = tuple(
            sorted(
                (
                    float(self.vars["step_hold_min"].get()),
                    float(self.vars["step_hold_max"].get()),
                )
            )
        )
        cfg.saccade_interval_range = tuple(
            sorted(
                (
                    float(self.vars["saccade_interval_min"].get()),
                    float(self.vars["saccade_interval_max"].get()),
                )
            )
        )
        return cfg

    def _collect_config(self) -> OrbitConfig:
        cfg = self._build_config_from_vars()
        self.vars["step_interval_min"].set(cfg.step_interval_range[0])
        self.vars["step_interval_max"].set(cfg.step_interval_range[1])
        self.vars["step_hold_min"].set(cfg.step_hold_range[0])
        self.vars["step_hold_max"].set(cfg.step_hold_range[1])
        self.vars["saccade_interval_min"].set(cfg.saccade_interval_range[0])
        self.vars["saccade_interval_max"].set(cfg.saccade_interval_range[1])
        self.vars["enable_b_hold"].set(cfg.enable_b_hold)
        return cfg

    def _drain_queue(self) -> None:
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in self._interactive_widgets:
            try:
                if enabled:
                    widget.state(["!disabled"])
                else:
                    widget.state(["disabled"])
            except tk.TclError:
                widget.configure(state="normal" if enabled else "disabled")

        if enabled:
            self.start_button.state(["!disabled"])
            self.stop_button.state(["disabled"])
        else:
            self.start_button.state(["disabled"])
            self.stop_button.state(["!disabled"])

    def _start_session(self) -> None:
        if self._session_thread and self._session_thread.is_alive():
            messagebox.showinfo("AutoAFK", "A session is already running.")
            return

        config = self._collect_config()
        self.config = config

        self._drain_queue()
        self.status_var.set("Calibrating session envelope...")
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100, value=0)
        self._session_stop_event.clear()
        self._set_controls_enabled(False)

        self._session_thread = threading.Thread(target=self._session_worker, args=(config,), daemon=True)
        self._session_thread.start()

    def _stop_session(self) -> None:
        if not self._session_thread or not self._session_thread.is_alive():
            return
        self._session_stop_event.set()
        self.status_var.set("Stopping session...")
        try:
            self.progress.configure(mode="indeterminate")
            self.progress.start(14)
        except tk.TclError:
            pass
        if self._mover is not None:
            self._mover.stop()

    def _session_worker(self, config: OrbitConfig) -> None:
        status_message = "Session idle."
        launched = False
        mover: OrbitMover | None = None
        try:
            self._post(("status", "Calibrating session envelope..."))
            total_delay = max(0.0, config.startup_delay)
            if total_delay > 0:
                start = time.perf_counter()
                while not self._session_stop_event.is_set():
                    elapsed = time.perf_counter() - start
                    remaining = max(0.0, total_delay - elapsed)
                    self._post(("countdown", remaining, total_delay))
                    if remaining <= 0.0:
                        break
                    time.sleep(0.1)
            if self._session_stop_event.is_set():
                status_message = "Session canceled before launch."
                return

            mover = OrbitMover(config)
            self._mover = mover
            launched = True
            self._post(("session_state", "running"))
            self._post(("status", "Choreography live. Use Ctrl+Alt+Q to disengage."))
            mover.start()
            mover.wait()
            status_message = "Session interrupted." if self._session_stop_event.is_set() else "Session complete."
        except Exception as exc:  # pragma: no cover - defensive guard
            status_message = "Session aborted due to an error."
            self._post(("error", str(exc)))
        finally:
            if launched and mover is not None:
                try:
                    mover.stop()
                    mover.wait()
                except Exception:
                    pass
            self._mover = None
            self._post(("session_state", "stopped"))
            self._post(("done", status_message))

    def _post(self, payload: tuple[str, object]) -> None:
        self._queue.put(payload)

    def _poll_queue(self) -> None:
        try:
            while True:
                payload = self._queue.get_nowait()
                kind = payload[0]
                if kind == "status":
                    self.status_var.set(payload[1])
                elif kind == "countdown":
                    remaining, total = payload[1], payload[2]
                    if total <= 0:
                        self.progress.configure(mode="determinate", maximum=100, value=100)
                        self.status_var.set("Engagement imminent...")
                    else:
                        completion = min(max((total - remaining) / total, 0.0), 1.0)
                        self.progress.configure(mode="determinate", maximum=100)
                        self.progress["value"] = completion * 100
                        if remaining > 0:
                            self.status_var.set(f"Engaging choreography in {remaining:0.1f}s")
                        else:
                            self.status_var.set("Engagement imminent...")
                elif kind == "session_state":
                    state = payload[1]
                    if state == "running":
                        self.progress.configure(mode="indeterminate")
                        self.progress.start(14)
                    elif state == "stopped":
                        self.progress.stop()
                        self.progress.configure(mode="determinate", maximum=100, value=0)
                elif kind == "error":
                    message = payload[1]
                    self.progress.stop()
                    messagebox.showerror("AutoAFK", message)
                    self.status_var.set("Error encountered. Session halted.")
                elif kind == "done":
                    message = payload[1] if len(payload) > 1 else "Session idle."
                    self.progress.stop()
                    self.progress.configure(mode="determinate", maximum=100, value=0)
                    self._set_controls_enabled(True)
                    self.status_var.set(message)  # type: ignore[arg-type]
                    self._session_thread = None
                    self._session_stop_event.clear()
        except queue.Empty:
            pass
        finally:
            self.root.after(80, self._poll_queue)

    def _on_close(self) -> None:
        if self._session_thread and self._session_thread.is_alive():
            if not messagebox.askyesno("AutoAFK", "A session is running. Stop and exit?"):
                return
            self._stop_session()
            if self._session_thread:
                self._session_thread.join(timeout=2.0)
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    AutoAFKApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
