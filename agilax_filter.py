#!/usr/bin/env python3
"""
Agilax Log Filter — lazy loading
Soubor se přečte až po kliknutí na Filtrovat, pouze pro zadaný rozsah dat.
Formát: 2026.05.14 00:00:00.151│    service    │[level] zpráva
"""

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
from datetime import datetime
import os, re, calendar, threading, sys

try:
    import ttkbootstrap as ttk
    from ttkbootstrap.constants import *
    TTKBS = True
except ImportError:
    from tkinter import ttk
    TTKBS = False

# ─────────────────────────────────────────────────────────────────────────────
#  Témata
# ─────────────────────────────────────────────────────────────────────────────

THEMES = {
    "light": {
        "bg": "#f5f5f5", "bg2": "#ffffff", "bg_header": "#e8e8e8",
        "fg": "#222222", "fg_dim": "#888888", "fg_header": "#1a5276",
        "sep": "#dddddd", "entry_bg": "#ffffff", "entry_fg": "#222222",
        "result_bg": "#fafafa", "result_fg": "#222222",
        "btn_on": "#27ae60", "btn_on_hov": "#2ecc71",
        "btn_off": "#e74c3c", "btn_off_hov": "#c0392b",
        "cal_in_range": "#d6eaf8",   # světle modrá pro dny v rozsahu logu
        "ttkbs_theme": "litera",
        "_dark": False,
    },
    "dark": {
        "bg": "#1e1e2e", "bg2": "#2a2a3e", "bg_header": "#16213e",
        "fg": "#cdd6f4", "fg_dim": "#6c7086", "fg_header": "#89b4fa",
        "sep": "#313244", "entry_bg": "#313244", "entry_fg": "#cdd6f4",
        "result_bg": "#181825", "result_fg": "#cdd6f4",
        "btn_on": "#a6e3a1", "btn_on_hov": "#94e2d5",
        "btn_off": "#f38ba8", "btn_off_hov": "#eba0ac",
        "cal_in_range": "#1a3a5c",   # tmavě modrá pro dny v rozsahu logu
        "ttkbs_theme": "darkly",
        "_dark": True,
    },
}

# ─────────────────────────────────────────────────────────────────────────────
#  Parser — regex pro Agilox formát
# ─────────────────────────────────────────────────────────────────────────────

_AGILOX_RE = re.compile(
    r'^(\d{4}\.\d{2}\.\d{2})\s+(\d{2}:\d{2}:\d{2}\.\d+)'
    r'\u2502([^\u2502]*)\u2502(.*)$'
)
_LEVEL_RE = re.compile(
    r'^\[(error|warning|warn|info|debug|trace|notice|fatal|critical)\]\s*',
    re.IGNORECASE
)
_LEVEL_MAP = {'WARN': 'WARNING', 'NOTICE': 'WARNING'}


def _parse_line(line: str):
    """Parsovat jeden řádek. Vrátí tuple nebo None."""
    m = _AGILOX_RE.match(line)
    if not m:
        return None
    date_raw, time_raw, module, message = m.groups()
    date_str = f"{date_raw[0:4]}-{date_raw[5:7]}-{date_raw[8:10]}"
    time_str = time_raw[:8]
    module   = module.strip()
    message  = message.strip()
    lm = _LEVEL_RE.match(message)
    if lm:
        level     = _LEVEL_MAP.get(lm.group(1).upper(), lm.group(1).upper())
        msg_clean = message[lm.end():]
    else:
        level     = 'INFO'
        msg_clean = message
    # tuple: date, time, time_ms, module, level, message, raw
    return (date_str, time_str, time_raw, module, level, msg_clean, line.rstrip('\n'))


def e_date(e):    return e[0]
def e_time(e):    return e[1]
def e_time_ms(e): return e[2]
def e_module(e):  return e[3]
def e_level(e):   return e[4]
def e_msg(e):     return e[5]
def e_raw(e):     return e[6]


def scan_metadata(filepath: str) -> dict:
    """
    Rychlé skenování souboru — přečte jen začátek a konec.
    Vrátí: {first_date, last_date, modules (set), line_count_approx}
    Trvá < 1 sekundu i pro 400MB soubor.
    """
    SCAN_BYTES = 512 * 1024   # prvních 512 KB
    TAIL_BYTES = 256 * 1024   # posledních 256 KB
    file_size  = os.path.getsize(filepath)

    modules    = set()
    first_date = None
    last_date  = None

    def scan_chunk(data: bytes):
        nonlocal first_date, last_date
        for raw_line in data.split(b'\n'):
            try:
                line = raw_line.decode('utf-8', errors='replace')
            except Exception:
                continue
            e = _parse_line(line)
            if e:
                modules.add(e_module(e))
                if first_date is None:
                    first_date = e_date(e)
                last_date = e_date(e)

    with open(filepath, 'rb') as f:
        # Začátek souboru
        scan_chunk(f.read(SCAN_BYTES))
        # Konec souboru (pro last_date)
        if file_size > SCAN_BYTES + TAIL_BYTES:
            f.seek(-TAIL_BYTES, 2)
            scan_chunk(f.read(TAIL_BYTES))

    # Přibližný počet řádků (odhad z velikosti)
    approx_lines = file_size // 170  # průměrná délka řádku ~170 B

    return {
        'first_date':   first_date,
        'last_date':    last_date,
        'modules':      sorted(m for m in modules if m),
        'approx_lines': approx_lines,
        'file_size':    file_size,
    }


def stream_filter(filepath: str, date_from=None, date_to=None,
                  time_from=None, time_to=None,
                  active_modules=None, level_filter=None,
                  search_text=None, search_case=False, search_regex=False,
                  progress_cb=None) -> list:
    """
    Přečte soubor řádek po řádku a vrátí jen záznamy splňující filtry.
    Nikdy nenačítá celý soubor do paměti najednou.
    """
    results    = []
    file_size  = os.path.getsize(filepath)
    bytes_read = 0
    last_pct   = -1
    REPORT_EVERY = max(1, file_size // 100)
    next_report  = REPORT_EVERY

    # Předkompilovat regex pro fulltext
    search_pat = None
    if search_text:
        if search_regex:
            try:
                search_pat = re.compile(search_text,
                                        0 if search_case else re.IGNORECASE)
            except re.error:
                search_pat = None
        else:
            search_needle = search_text if search_case else search_text.lower()

    with open(filepath, 'r', encoding='utf-8', errors='replace',
              buffering=1 << 20) as f:
        for line in f:
            bytes_read += len(line)

            e = _parse_line(line)
            if not e:
                # RAW řádek — přeskočit pokud jsou aktivní filtry
                if not (date_from or date_to or time_from or time_to or
                        active_modules or level_filter or search_text):
                    s = line.rstrip('\n')
                    if s:
                        results.append((None, None, None, '', 'RAW', s, s))
                continue

            # Filtr datum od
            if date_from and e_date(e) < date_from:
                # Optimalizace: pokud je datum menší, přeskočit
                # (log je chronologický)
                if bytes_read > file_size * 0.01:  # po prvním 1% přestat čekat
                    pass  # nemůžeme přeskočit — log nemusí být 100% seřazený
                continue

            # Filtr datum do — jakmile jsme za rozsahem, skončit
            if date_to and e_date(e) > date_to:
                # Log je chronologický — zbytek přeskočit
                break

            # Filtr čas od
            if time_from and e_time(e) and e_time(e) < time_from:
                continue

            # Filtr čas do
            if time_to and e_time(e) and e_time(e) > time_to:
                continue

            # Filtr modulů
            if active_modules is not None and e_module(e) not in active_modules:
                continue

            # Filtr úrovně
            if level_filter and e_level(e) != level_filter:
                continue

            # Fulltext
            if search_text:
                raw = e_raw(e)
                if search_pat:
                    if not search_pat.search(raw):
                        continue
                else:
                    hay = raw if search_case else raw.lower()
                    if search_needle not in hay:
                        continue

            results.append(e)

            # Progress
            if progress_cb and bytes_read >= next_report:
                pct = min(99, int(bytes_read * 100 / file_size))
                if pct != last_pct:
                    last_pct = pct
                    progress_cb(pct)
                next_report = bytes_read + REPORT_EVERY

    if progress_cb:
        progress_cb(100)
    return results

# ─────────────────────────────────────────────────────────────────────────────
#  Výchozí moduly
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_MODULES = [
    "agiloxio", "service", "connection", "net", "webservice",
    "localisation", "action", "router", "worker", "observe",
    "barcode", "request", "condition", "ci", "order",
    "blackbox", "master", "battery", "modem", "notify",
    "audio", "agilox", "memory", "monitoring", "scanpattern",
    "routemap", "update", "setup", "download", "analytics",
    "plc_out", "plc_in", "remoteConnector", "collision",
    "azureIot", "matching", "fleetgate", "modal", "s300", "r2000",
]

# ─────────────────────────────────────────────────────────────────────────────
#  Kalendář popup
# ─────────────────────────────────────────────────────────────────────────────

class DatePickerPopup:
    def __init__(self, parent, callback, initial_date=None, theme=None,
                 valid_from=None, valid_to=None):
        """
        valid_from / valid_to: str 'RRRR-MM-DD' — rozsah povolených dat.
        Dny mimo rozsah jsou zobrazeny šedě a nelze je vybrat.
        """
        self.callback   = callback
        self.T          = theme or THEMES["light"]
        self.valid_from = valid_from  # 'YYYY-MM-DD' nebo None
        self.valid_to   = valid_to    # 'YYYY-MM-DD' nebo None
        today = datetime.now()
        if initial_date:
            try:
                dt = datetime.strptime(initial_date, '%Y-%m-%d')
                self.year, self.month, self.selected_day = dt.year, dt.month, dt.day
            except ValueError:
                self.year, self.month, self.selected_day = today.year, today.month, today.day
        else:
            self.year, self.month, self.selected_day = today.year, today.month, today.day

        self.win = tk.Toplevel(parent)
        self.win.title("Vyberte datum")
        self.win.resizable(False, False)
        self.win.grab_set()
        self.win.transient(parent)
        parent.update_idletasks()
        self.win.geometry(f"+{parent.winfo_rootx()}+{parent.winfo_rooty()+parent.winfo_height()}")
        self._build()

    def _build(self):
        T = self.T
        self.win.configure(bg=T["bg2"])
        h = tk.Frame(self.win, bg=T["bg_header"], pady=6); h.pack(fill=tk.X)
        for txt, cmd, side in [("<", self._prev_month, tk.LEFT), (">", self._next_month, tk.RIGHT)]:
            tk.Button(h, text=txt, bg=T["bg_header"], fg=T["fg_header"], relief=tk.FLAT,
                      font=("TkDefaultFont",12,"bold"), activebackground=T["sep"],
                      cursor="hand2", command=cmd).pack(side=side, padx=8)
        self.lbl = tk.Label(h, bg=T["bg_header"], fg=T["fg_header"],
                            font=("TkDefaultFont",11,"bold"))
        self.lbl.pack(side=tk.LEFT, expand=True)

        yn = tk.Frame(self.win, bg=T["bg2"], pady=2); yn.pack(fill=tk.X)
        tk.Button(yn, text="<< rok", bg=T["bg2"], fg=T["fg_dim"], relief=tk.FLAT,
                  font=("TkDefaultFont",9), cursor="hand2",
                  command=self._prev_year).pack(side=tk.LEFT, padx=8)
        tk.Button(yn, text="rok >>", bg=T["bg2"], fg=T["fg_dim"], relief=tk.FLAT,
                  font=("TkDefaultFont",9), cursor="hand2",
                  command=self._next_year).pack(side=tk.RIGHT, padx=8)

        dh = tk.Frame(self.win, bg=T["bg2"]); dh.pack(fill=tk.X, padx=4)
        for i, d in enumerate(["Po","Út","St","Čt","Pá","So","Ne"]):
            tk.Label(dh, text=d, bg=T["bg2"],
                     fg="#e74c3c" if i>=5 else T["fg_dim"],
                     font=("TkDefaultFont",9,"bold"), width=4,
                     anchor=tk.CENTER).grid(row=0, column=i, padx=1, pady=2)

        self.gf = tk.Frame(self.win, bg=T["bg2"]); self.gf.pack(fill=tk.BOTH, padx=4, pady=(0,4))
        tf = tk.Frame(self.win, bg=T["bg2"], pady=4); tf.pack(fill=tk.X)
        tk.Button(tf, text="Dnes", bg=T["sep"], fg=T["fg"], relief=tk.FLAT,
                  font=("TkDefaultFont",9), cursor="hand2",
                  command=self._today, padx=10, pady=3).pack()
        self._render()

    def _render(self):
        T = self.T
        for w in self.gf.winfo_children(): w.destroy()
        MONTHS = ["Leden","Únor","Březen","Duben","Květen","Červen",
                  "Červenec","Srpen","Září","Říjen","Listopad","Prosinec"]
        self.lbl.config(text=f"{MONTHS[self.month-1]} {self.year}")
        today = datetime.now()

        # Předpočítat rozsah jako date objekty pro rychlé porovnání
        vf = None
        vt = None
        try:
            if self.valid_from:
                vf = datetime.strptime(self.valid_from, '%Y-%m-%d').date()
        except Exception: pass
        try:
            if self.valid_to:
                vt = datetime.strptime(self.valid_to, '%Y-%m-%d').date()
        except Exception: pass

        for ri, week in enumerate(calendar.monthcalendar(self.year, self.month)):
            for ci, day in enumerate(week):
                if day == 0:
                    tk.Label(self.gf, text="", bg=T["bg2"], width=4, height=1).grid(
                        row=ri, column=ci, padx=1, pady=1)
                    continue

                this_date = datetime(self.year, self.month, day).date()
                is_sel    = day == self.selected_day
                is_today  = (day == today.day and self.month == today.month
                             and self.year == today.year)
                is_we     = ci >= 5

                # Zkontrolovat jestli je den v platném rozsahu logu
                in_range = True
                if vf and this_date < vf: in_range = False
                if vt and this_date > vt: in_range = False

                if not in_range:
                    # Den mimo rozsah logu — šedý label, nelze kliknout
                    tk.Label(self.gf, text=str(day), bg=T["bg2"],
                             fg=T["sep"],   # velmi světlá šedá — skoro neviditelná
                             font=("TkDefaultFont", 10), width=3, height=1,
                             anchor=tk.CENTER).grid(row=ri, column=ci, padx=1, pady=1)
                    continue

                # Den v rozsahu — určit barvu
                if is_sel:
                    bg, fg, fw = T["fg_header"], T["bg2"], "bold"
                elif is_today:
                    bg, fg, fw = T["bg2"], "#27ae60", "bold"
                elif is_we:
                    bg, fg, fw = T["bg2"], "#e74c3c", "normal"
                else:
                    bg, fg, fw = T["bg2"], T["fg"], "normal"

                # Zvýraznit dny v rozsahu logu světle modrým pozadím
                # (jen pokud nejsou vybrané nebo dnešní)
                if not is_sel and not is_today and vf and vt:
                    bg = T.get("cal_in_range", "#d6eaf8")

                tk.Button(self.gf, text=str(day), bg=bg, fg=fg, relief=tk.FLAT,
                          font=("TkDefaultFont", 10, fw), width=3, height=1,
                          cursor="hand2", activebackground=T["sep"],
                          command=lambda d=day: self._pick(d)).grid(
                    row=ri, column=ci, padx=1, pady=1)

    def _pick(self, day):
        self.selected_day = day
        self.win.destroy()
        self.callback(f"{self.year:04d}-{self.month:02d}-{day:02d}")

    def _today(self):
        self.win.destroy(); self.callback(datetime.now().strftime('%Y-%m-%d'))

    def _prev_month(self):
        if self.month == 1: self.month, self.year = 12, self.year-1
        else: self.month -= 1
        self.selected_day = min(self.selected_day, calendar.monthrange(self.year,self.month)[1])
        self._render()

    def _next_month(self):
        if self.month == 12: self.month, self.year = 1, self.year+1
        else: self.month += 1
        self.selected_day = min(self.selected_day, calendar.monthrange(self.year,self.month)[1])
        self._render()

    def _prev_year(self):
        self.year -= 1
        self.selected_day = min(self.selected_day, calendar.monthrange(self.year,self.month)[1])
        self._render()

    def _next_year(self):
        self.year += 1
        self.selected_day = min(self.selected_day, calendar.monthrange(self.year,self.month)[1])
        self._render()


# ─────────────────────────────────────────────────────────────────────────────
#  DatePickerEntry
# ─────────────────────────────────────────────────────────────────────────────

class DatePickerEntry(tk.Frame):
    def __init__(self, parent, font=None, width=12, theme=None, **kwargs):
        self._T         = theme or THEMES["light"]
        self._valid_from = None   # nastaví se po načtení metadat logu
        self._valid_to   = None
        super().__init__(parent, bg=self._T["bg"])
        self._e = tk.Entry(self, font=font, width=width,
                           bg=self._T["entry_bg"], fg=self._T["entry_fg"],
                           insertbackground=self._T["entry_fg"],
                           relief=tk.FLAT, bd=1, highlightthickness=1,
                           highlightbackground=self._T["sep"])
        self._e.pack(side=tk.LEFT)
        self._btn = tk.Button(self, text="📅", relief=tk.GROOVE, cursor="hand2",
                              font=("TkDefaultFont",9),
                              bg=self._T["bg_header"], fg=self._T["fg_header"],
                              activebackground=self._T["sep"],
                              padx=4, pady=1, command=self._open)
        self._btn.pack(side=tk.LEFT, padx=(2,0))

    def set_valid_range(self, valid_from: str | None, valid_to: str | None):
        """Nastavit rozsah platných dat z metadat logu."""
        self._valid_from = valid_from
        self._valid_to   = valid_to

    def _open(self):
        DatePickerPopup(self._e, self._set,
                        initial_date=self._e.get().strip() or None,
                        theme=self._T,
                        valid_from=self._valid_from,
                        valid_to=self._valid_to)

    def _set(self, s):
        self._e.delete(0, tk.END); self._e.insert(0, s)

    def apply_theme(self, T):
        self._T = T
        self.config(bg=T["bg"])
        self._e.config(bg=T["entry_bg"], fg=T["entry_fg"],
                       insertbackground=T["entry_fg"], highlightbackground=T["sep"])
        self._btn.config(bg=T["bg_header"], fg=T["fg_header"],
                         activebackground=T["sep"])

    def get(self): return self._e.get()
    def insert(self, i, s): self._e.insert(i, s)
    def delete(self, f, l=None): self._e.delete(f, l)
    def grid(self, **kw): super().grid(**kw)
    def pack(self, **kw): super().pack(**kw)


# ─────────────────────────────────────────────────────────────────────────────
#  ModuleToggle
# ─────────────────────────────────────────────────────────────────────────────

class ModuleToggle(tk.Frame):
    def __init__(self, parent, module_name: str, on_change=None, theme=None, **kwargs):
        T = theme or THEMES["light"]
        super().__init__(parent, bg=T["bg"])
        self.module_name = module_name
        self.on_change   = on_change
        self._state      = True
        self._T          = T
        self.btn = tk.Button(self, font=("TkDefaultFont",10,"bold"),
                             fg=T["bg2"], relief=tk.FLAT, cursor="hand2",
                             padx=8, pady=4, bd=0, command=self._toggle)
        self.btn.pack(fill=tk.BOTH, expand=True)
        self._refresh()

    def _toggle(self):
        self._state = not self._state
        self._refresh()
        if self.on_change: self.on_change(self.module_name, self._state)

    def _refresh(self):
        T = self._T
        if self._state:
            self.btn.config(bg=T["btn_on"], activebackground=T["btn_on_hov"],
                            fg=T["bg2"], text=f"● {self.module_name}")
        else:
            self.btn.config(bg=T["btn_off"], activebackground=T["btn_off_hov"],
                            fg=T["bg2"], text=f"✕ {self.module_name}")

    def apply_theme(self, T):
        self._T = T; self.config(bg=T["bg"]); self._refresh()

    def set_state(self, s: bool): self._state = s; self._refresh()
    def get_state(self) -> bool:  return self._state


# ─────────────────────────────────────────────────────────────────────────────
#  Registrace přibalených fontů
# ─────────────────────────────────────────────────────────────────────────────

def _get_asset_dir() -> str:
    """Vrátí složku s assety — funguje jak při spuštění .py, tak z PyInstaller EXE."""
    if getattr(sys, 'frozen', False):
        # PyInstaller EXE — assety jsou rozbaleny do sys._MEIPASS
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _register_bundled_fonts():
    """
    Zaregistruje přibalené .ttf soubory do systému, aby je Tkinter viděl.
    - Windows: AddFontResourceEx přes ctypes (dočasně, jen pro tuto session)
    - Linux/macOS: zkopíruje do ~/.fonts a zavolá fc-cache (pokud není jiná cesta)
    Tiše selže pokud registrace není možná.
    """
    asset_dir = _get_asset_dir()
    fonts = [
        os.path.join(asset_dir, "DejaVuSans.ttf"),
        os.path.join(asset_dir, "DejaVuSans-Bold.ttf"),
    ]

    if sys.platform == "win32":
        try:
            import ctypes
            gdi32 = ctypes.WinDLL("gdi32")
            for path in fonts:
                if os.path.exists(path):
                    # FR_PRIVATE = 0x10 — font viditelný jen pro tento proces
                    gdi32.AddFontResourceExW(path, 0x10, 0)
        except Exception:
            pass

    elif sys.platform in ("linux", "linux2", "darwin"):
        try:
            import shutil
            home_fonts = os.path.join(os.path.expanduser("~"), ".fonts")
            os.makedirs(home_fonts, exist_ok=True)
            changed = False
            for path in fonts:
                if os.path.exists(path):
                    dest = os.path.join(home_fonts, os.path.basename(path))
                    if not os.path.exists(dest):
                        shutil.copy2(path, dest)
                        changed = True
            if changed:
                os.system("fc-cache -f > /dev/null 2>&1")
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  Hlavní GUI
# ─────────────────────────────────────────────────────────────────────────────

class AgilaxLogFilterApp:

    def __init__(self, root):
        self.root = root
        self.root.title("Agilax Log Filter")
        self.root.geometry("1450x900")
        self.root.minsize(1100, 680)

        self._dark          = False
        self._T             = THEMES["light"]
        self.log_file:      str | None = None
        self.log_meta:      dict | None = None   # výsledek scan_metadata
        self.filtered_entries: list = []
        self._toggles:      dict = {}
        self._date_entries: list = []

        self._setup_fonts()
        self._build_ui()

    def _setup_fonts(self):
        import tkinter.font as tkfont

        # Pokusit se zaregistrovat přibalené .ttf fonty do systému
        # (funguje na Windows i Linuxu — tiše selže pokud to nejde)
        _register_bundled_fonts()

        avail = set(tkfont.families())

        # Preferovaný pořadí: DejaVu Sans (přibalený), pak systémové fallbacky
        UI_FONT = None
        for c in ("DejaVu Sans", "nimbus sans l", "Nimbus Sans L",
                  "Ubuntu", "helvetica", "TkDefaultFont"):
            if c in avail or c.startswith("Tk"):
                UI_FONT = c
                break
        if UI_FONT is None:
            UI_FONT = "TkDefaultFont"

        self.F      = (UI_FONT, 12)
        self.F_BOLD = (UI_FONT, 12, "bold")
        self.F_BIG  = (UI_FONT, 13, "bold")
        self.F_SM   = (UI_FONT, 10)
        self.F_TINY = (UI_FONT, 9)

        # Monospace pro výsledky logu
        MONO_FONT = None
        for c in ("DejaVu Sans Mono", "nimbus mono l", "courier 10 pitch",
                  "Courier New", "courier", "TkFixedFont"):
            if c in avail or c.startswith("Tk"):
                MONO_FONT = c
                break
        if MONO_FONT is None:
            MONO_FONT = "TkFixedFont"

        self.F_MONO = (MONO_FONT, 11)

    # ── Celé UI ───────────────────────────────────────────────────────────────

    def _build_ui(self):
        T = self._T
        self.root.configure(bg=T["bg"])

        # Horní lišta
        self._top = tk.Frame(self.root, bg=T["bg_header"], pady=6)
        self._top.pack(fill=tk.X)

        self._logo_image = None
        png = os.path.join(os.path.dirname(os.path.abspath(__file__)), "MODEL_Logo_M.png")
        if os.path.exists(png):
            try:
                raw = tk.PhotoImage(file=png)
                f = max(1, raw.height() // 40)
                self._logo_image = raw.subsample(f, f)
            except Exception: pass
        if self._logo_image:
            tk.Label(self._top, image=self._logo_image,
                     bg=T["bg_header"]).pack(side=tk.LEFT, padx=(12,15))

        fi = tk.Frame(self._top, bg=T["bg_header"]); fi.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(fi, text="📄", font=self.F_BIG,
                 bg=T["bg_header"], fg=T["fg_header"]).pack(side=tk.LEFT, padx=(0,5))
        tk.Label(fi, text="Log soubor:", font=self.F_BOLD,
                 bg=T["bg_header"], fg=T["fg_header"]).pack(side=tk.LEFT, padx=5)
        self.file_lbl = tk.Label(fi, text="Žádný soubor",
                                 fg=T["fg_dim"], font=self.F, bg=T["bg_header"])
        self.file_lbl.pack(side=tk.LEFT, padx=5)

        # Tlačítka vpravo
        self._theme_btn = tk.Button(
            self._top, text="\u263d Tmav\u00fd", font=self.F_SM,
            bg=T["sep"], fg=T["fg"], relief=tk.FLAT, cursor="hand2",
            padx=10, pady=4, activebackground=T["bg"],
            command=self._toggle_theme)
        self._theme_btn.pack(side=tk.RIGHT, padx=8)

        self._export_btn = tk.Button(
            self._top, text="💾 Export TXT", font=self.F_SM,
            bg="#27ae60", fg="white", relief=tk.FLAT, cursor="hand2",
            padx=10, pady=4, activebackground="#2ecc71",
            command=self._export)
        self._export_btn.pack(side=tk.RIGHT, padx=4)

        self._load_btn = tk.Button(
            self._top, text="📂 Vybrat log", font=self.F_SM,
            bg=T["fg_header"], fg="white", relief=tk.FLAT, cursor="hand2",
            padx=10, pady=4, activebackground=T["sep"],
            command=self._browse)
        self._load_btn.pack(side=tk.RIGHT, padx=4)

        # Oddělovač + branding
        self._sep1 = tk.Frame(self.root, bg=T["sep"], height=1); self._sep1.pack(fill=tk.X)
        self._brand = tk.Frame(self.root, bg=T["bg"], pady=4); self._brand.pack(fill=tk.X, padx=12)
        self._brand_lbl1 = tk.Label(self._brand, text="Agilax Log Filter",
                                    font=self.F_BOLD, bg=T["bg"], fg=T["fg"])
        self._brand_lbl1.pack(side=tk.LEFT)
        self._brand_lbl2 = tk.Label(self._brand, text="| Model Group",
                                    font=self.F, bg=T["bg"], fg=T["fg_dim"])
        self._brand_lbl2.pack(side=tk.LEFT, padx=8)
        self._sep2 = tk.Frame(self.root, bg=T["sep"], height=1); self._sep2.pack(fill=tk.X)

        # Progress bar (skrytý)
        self._prog_frame  = tk.Frame(self.root, bg=T["bg"], height=5)
        self._prog_canvas = tk.Canvas(self._prog_frame, height=5,
                                      bg=T["sep"], highlightthickness=0)
        self._prog_bar    = self._prog_canvas.create_rectangle(
            0, 0, 0, 5, fill=T["fg_header"], outline="")

        # Záložky
        nb_kw = {"bootstyle": "primary"} if TTKBS else {}
        self.nb = ttk.Notebook(self.root, **nb_kw)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        self.tab_filter = tk.Frame(self.nb, bg=T["bg"])
        self.nb.add(self.tab_filter, text="🔍 Filtrování logu")
        self._build_filter_tab()

        self.tab_stats = tk.Frame(self.nb, bg=T["bg"])
        self.nb.add(self.tab_stats, text="📊 Statistiky")
        self._build_stats_tab()

        # Stavový řádek
        self._status_frame = tk.Frame(self.root, bg=T["bg_header"])
        self._status_frame.pack(fill=tk.X, side=tk.BOTTOM)
        self.status = tk.Label(self._status_frame,
                               text="✓ Připraveno — vyberte log soubor",
                               anchor=tk.W, padx=10, pady=4, font=self.F_SM,
                               bg=T["bg_header"], fg=T["fg_header"])
        self.status.pack(fill=tk.X)

    # ── Záložka filtrování ────────────────────────────────────────────────────

    def _build_filter_tab(self):
        T   = self._T
        tab = self.tab_filter

        paned = tk.PanedWindow(tab, orient=tk.HORIZONTAL, sashwidth=6,
                               sashrelief=tk.RAISED, bg=T["sep"])
        paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self._paned = paned

        # ── Levý scrollovatelný panel ─────────────────────────────────────────
        left_outer = tk.Frame(paned, bg=T["bg"]); paned.add(left_outer, minsize=300)
        self._left_outer = left_outer

        self._lc = tk.Canvas(left_outer, bg=T["bg"], highlightthickness=0)
        self._ls = ttk.Scrollbar(left_outer, orient=tk.VERTICAL, command=self._lc.yview)
        self._lc.configure(yscrollcommand=self._ls.set)
        self._ls.pack(side=tk.RIGHT, fill=tk.Y)
        self._lc.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._left = tk.Frame(self._lc, bg=T["bg"], padx=10, pady=8)
        self._lc.create_window((0,0), window=self._left, anchor="nw", tags="lw")

        def _cfg(e):
            self._lc.configure(scrollregion=self._lc.bbox("all"))
            self._lc.itemconfig("lw", width=self._lc.winfo_width())
        self._left.bind("<Configure>", _cfg)
        self._lc.bind("<Configure>", lambda e: self._lc.itemconfig("lw", width=e.width))
        self._lc.bind_all("<Button-4>", lambda e: self._lc.yview_scroll(-1,"units"))
        self._lc.bind_all("<Button-5>", lambda e: self._lc.yview_scroll(1,"units"))

        L = self._left

        # ── Datum a čas ───────────────────────────────────────────────────────
        self._sec(L, "📅  Datum a čas").pack(fill=tk.X, pady=(0,6))

        dtf = tk.Frame(L, bg=T["bg"]); dtf.pack(fill=tk.X, pady=2)

        self._lbl2(dtf, "Datum od:", 0)
        self.w_date_from = DatePickerEntry(dtf, font=self.F, width=12, theme=T)
        self.w_date_from.grid(row=0, column=1, sticky=tk.W, padx=5, pady=3)
        self._date_entries.append(self.w_date_from)

        self._lbl2(dtf, "Datum do:", 1)
        self.w_date_to = DatePickerEntry(dtf, font=self.F, width=12, theme=T)
        self.w_date_to.grid(row=1, column=1, sticky=tk.W, padx=5, pady=3)
        self._date_entries.append(self.w_date_to)

        self._lbl2(dtf, "Čas od:", 2)
        tf_r = tk.Frame(dtf, bg=T["bg"]); tf_r.grid(row=2, column=1, sticky=tk.W, padx=5, pady=3)
        self.w_time_from = self._entry(tf_r, width=10); self.w_time_from.pack(side=tk.LEFT)
        tk.Label(tf_r, text="HH:MM:SS", font=self.F_SM,
                 fg=T["fg_dim"], bg=T["bg"]).pack(side=tk.LEFT, padx=4)

        self._lbl2(dtf, "Čas do:", 3)
        tt_r = tk.Frame(dtf, bg=T["bg"]); tt_r.grid(row=3, column=1, sticky=tk.W, padx=5, pady=3)
        self.w_time_to = self._entry(tt_r, width=10); self.w_time_to.pack(side=tk.LEFT)
        tk.Label(tt_r, text="HH:MM:SS", font=self.F_SM,
                 fg=T["fg_dim"], bg=T["bg"]).pack(side=tk.LEFT, padx=4)

        # Info o rozsahu logu
        self.range_lbl = tk.Label(L, text="", font=self.F_SM,
                                  fg=T["fg_dim"], bg=T["bg"], justify=tk.LEFT)
        self.range_lbl.pack(anchor=tk.W, pady=(2,0))

        self._hsep(L)

        # ── Hledaný text ──────────────────────────────────────────────────────
        self._sec(L, "🔎  Hledaný text").pack(fill=tk.X, pady=(0,6))
        self.v_search = tk.StringVar()
        self.w_search = self._entry(L)
        self.w_search.config(textvariable=self.v_search)
        self.w_search.pack(fill=tk.X, pady=4)
        self.w_search.bind('<Return>', lambda e: self._apply())

        opts = tk.Frame(L, bg=T["bg"]); opts.pack(fill=tk.X)
        self.v_case  = tk.BooleanVar(value=False)
        self.v_regex = tk.BooleanVar(value=False)
        self._chk(opts, "Rozlišovat velikost", self.v_case).pack(side=tk.LEFT)
        self._chk(opts, "Regex", self.v_regex).pack(side=tk.LEFT, padx=8)

        self._hsep(L)

        # ── Moduly ────────────────────────────────────────────────────────────
        self._sec(L, "🔧  Moduly").pack(fill=tk.X, pady=(0,4))

        mb = tk.Frame(L, bg=T["bg"]); mb.pack(fill=tk.X, pady=(0,6))
        self._btn_all_on = tk.Button(mb, text="✔ Vše zobrazit", font=self.F_SM,
                                     bg=T["btn_on"], fg=T["bg2"], relief=tk.FLAT,
                                     cursor="hand2", padx=6, pady=3,
                                     activebackground=T["btn_on_hov"],
                                     command=lambda: self._set_all_modules(True))
        self._btn_all_on.pack(side=tk.LEFT, padx=(0,4))
        self._btn_all_off = tk.Button(mb, text="✕ Vše ignorovat", font=self.F_SM,
                                      bg=T["btn_off"], fg=T["bg2"], relief=tk.FLAT,
                                      cursor="hand2", padx=6, pady=3,
                                      activebackground=T["btn_off_hov"],
                                      command=lambda: self._set_all_modules(False))
        self._btn_all_off.pack(side=tk.LEFT)

        self.toggles_frame = tk.Frame(L, bg=T["bg"]); self.toggles_frame.pack(fill=tk.X, pady=2)
        self._build_toggles(DEFAULT_MODULES)

        self._hsep(L)

        # ── Úroveň ────────────────────────────────────────────────────────────
        self._sec(L, "⚠  Úroveň záznamu").pack(fill=tk.X, pady=(0,6))
        self.v_level = tk.StringVar(value="Vše")
        self.w_level = ttk.Combobox(L, textvariable=self.v_level,
                                    font=self.F, width=16, state="readonly")
        self.w_level['values'] = ["Vše","ERROR","FATAL","WARNING","INFO","DEBUG","TRACE","RAW"]
        self.w_level.pack(fill=tk.X, pady=2)

        self._hsep(L)

        # ── Tlačítka ──────────────────────────────────────────────────────────
        self._btn_filter = tk.Button(L, text="🔍 Filtrovat a načíst", font=self.F_BOLD,
                                     bg=T["fg_header"], fg="white", relief=tk.FLAT,
                                     cursor="hand2", pady=8,
                                     activebackground=T["sep"],
                                     command=self._apply)
        self._btn_filter.pack(fill=tk.X, pady=3)

        self._btn_clear = tk.Button(L, text="✖ Vymazat filtry", font=self.F,
                                    bg=T["sep"], fg=T["fg"], relief=tk.FLAT,
                                    cursor="hand2", pady=5,
                                    activebackground=T["bg"],
                                    command=self._clear)
        self._btn_clear.pack(fill=tk.X, pady=3)

        self.count_lbl = tk.Label(L, text="", font=self.F_SM,
                                  fg=T["fg_dim"], bg=T["bg"])
        self.count_lbl.pack(pady=6)

        # ── Pravý panel: výsledky ─────────────────────────────────────────────
        right = tk.Frame(paned, bg=T["bg"], padx=5, pady=5)
        paned.add(right, minsize=600)
        self._right = right

        tb = tk.Frame(right, bg=T["bg"]); tb.pack(fill=tk.X, pady=(0,4))
        tk.Label(tb, text="Zobrazení:", font=self.F_SM,
                 bg=T["bg"], fg=T["fg_dim"]).pack(side=tk.LEFT)
        self.v_wrap  = tk.BooleanVar(value=False)
        self.v_color = tk.BooleanVar(value=True)
        self._chk(tb, "Zalamovat řádky", self.v_wrap,
                  cmd=self._toggle_wrap).pack(side=tk.LEFT, padx=8)
        self._chk(tb, "Barevné úrovně", self.v_color,
                  cmd=self._redraw).pack(side=tk.LEFT, padx=4)

        tf2 = tk.Frame(right, bg=T["bg"]); tf2.pack(fill=tk.BOTH, expand=True)
        self.txt = tk.Text(tf2, font=self.F_MONO, wrap=tk.NONE, state=tk.DISABLED,
                           relief=tk.FLAT, bg=T["result_bg"], fg=T["result_fg"],
                           selectbackground=T["fg_header"], selectforeground=T["bg2"],
                           insertbackground=T["result_fg"], padx=8, pady=6)
        sy = ttk.Scrollbar(tf2, orient=tk.VERTICAL,   command=self.txt.yview)
        sx = ttk.Scrollbar(tf2, orient=tk.HORIZONTAL, command=self.txt.xview)
        self.txt.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side=tk.RIGHT, fill=tk.Y)
        sx.pack(side=tk.BOTTOM, fill=tk.X)
        self.txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._setup_tags()

    def _build_stats_tab(self):
        T = self._T
        f = tk.Frame(self.tab_stats, bg=T["bg"], padx=15, pady=10)
        f.pack(fill=tk.BOTH, expand=True)
        tk.Label(f, text="📊 Statistiky posledního filtrování",
                 font=self.F_BIG, bg=T["bg"], fg=T["fg"]).pack(anchor=tk.W, pady=(0,10))
        self.stats_txt = tk.Text(f, font=self.F_MONO, state=tk.DISABLED,
                                 relief=tk.FLAT, bg=T["result_bg"], fg=T["result_fg"],
                                 padx=10, pady=8)
        ss = ttk.Scrollbar(f, orient=tk.VERTICAL, command=self.stats_txt.yview)
        self.stats_txt.configure(yscrollcommand=ss.set)
        ss.pack(side=tk.RIGHT, fill=tk.Y)
        self.stats_txt.pack(fill=tk.BOTH, expand=True)

    # ── Pomocné UI ────────────────────────────────────────────────────────────

    def _sec(self, p, text):
        T = self._T
        return tk.Label(p, text=text, font=self.F_BIG,
                        bg=T["bg_header"], fg=T["fg_header"],
                        anchor=tk.W, padx=8, pady=4, relief=tk.FLAT)

    def _hsep(self, p):
        tk.Frame(p, bg=self._T["sep"], height=1).pack(fill=tk.X, pady=8)

    def _lbl2(self, p, text, row):
        tk.Label(p, text=text, font=self.F_BOLD, bg=self._T["bg"], fg=self._T["fg"],
                 anchor=tk.W, width=10).grid(row=row, column=0, sticky=tk.W, pady=3)

    def _entry(self, p, width=22):
        T = self._T
        return tk.Entry(p, font=self.F, width=width,
                        bg=T["entry_bg"], fg=T["entry_fg"],
                        insertbackground=T["entry_fg"],
                        relief=tk.FLAT, bd=1, highlightthickness=1,
                        highlightbackground=T["sep"])

    def _chk(self, p, text, var, cmd=None):
        T = self._T
        return tk.Checkbutton(p, text=text, variable=var,
                              bg=T["bg"], fg=T["fg"], selectcolor=T["bg"],
                              activebackground=T["bg"], font=self.F_SM,
                              command=cmd)

    def _setup_tags(self):
        bold = (self.F_MONO[0], self.F_MONO[1], 'bold')
        if self._dark:
            self.txt.tag_configure('ERROR',   foreground='#f38ba8', font=bold)
            self.txt.tag_configure('FATAL',   foreground='#eba0ac', font=bold)
            self.txt.tag_configure('WARNING', foreground='#fab387')
            self.txt.tag_configure('INFO',    foreground='#89b4fa')
            self.txt.tag_configure('DEBUG',   foreground='#a6e3a1')
            self.txt.tag_configure('TRACE',   foreground='#cba6f7')
            self.txt.tag_configure('RAW',     foreground='#6c7086')
            self.txt.tag_configure('HIGHLIGHT', background='#f9e2af', foreground='#1e1e2e')
        else:
            self.txt.tag_configure('ERROR',   foreground='#e74c3c', font=bold)
            self.txt.tag_configure('FATAL',   foreground='#c0392b', font=bold)
            self.txt.tag_configure('WARNING', foreground='#e67e22')
            self.txt.tag_configure('INFO',    foreground='#2980b9')
            self.txt.tag_configure('DEBUG',   foreground='#27ae60')
            self.txt.tag_configure('TRACE',   foreground='#8e44ad')
            self.txt.tag_configure('RAW',     foreground='#888888')
            self.txt.tag_configure('HIGHLIGHT', background='#fff176', foreground='#222222')

    def _build_toggles(self, modules: list):
        T = self._T
        for w in self.toggles_frame.winfo_children(): w.destroy()
        self._toggles.clear()
        self.toggles_frame.columnconfigure(0, weight=1)
        self.toggles_frame.columnconfigure(1, weight=1)
        for i, mod in enumerate(sorted(modules)):
            row, col = divmod(i, 2)
            t = ModuleToggle(self.toggles_frame, mod,
                             on_change=lambda n, s: None, theme=T)
            t.grid(row=row, column=col, padx=3, pady=2, sticky=tk.EW)
            self._toggles[mod] = t

    def _set_all_modules(self, state: bool):
        for t in self._toggles.values(): t.set_state(state)

    def _get_active_modules(self) -> set:
        return {n for n, t in self._toggles.items() if t.get_state()}

    # ── Výběr souboru — jen metadata, žádné čtení ─────────────────────────────

    def _browse(self):
        path = filedialog.askopenfilename(
            title="Vyberte Agilax log soubor",
            filetypes=[("Log soubory","*.log"),("Textové soubory","*.txt"),
                       ("Všechny soubory","*.*")])
        if not path: return

        self._set_status(f"⏳ Skenuji metadata: {os.path.basename(path)} ...")
        self._load_btn.config(state=tk.DISABLED)
        self.root.update_idletasks()

        def worker():
            try:
                meta = scan_metadata(path)
                self.root.after(0, self._meta_done, path, meta)
            except Exception as ex:
                self.root.after(0, self._meta_error, str(ex))

        threading.Thread(target=worker, daemon=True).start()

    def _meta_done(self, path, meta):
        self.log_file = path
        self.log_meta = meta
        self._load_btn.config(state=tk.NORMAL)

        fname = os.path.basename(path)
        size_mb = meta['file_size'] / 1024 / 1024
        self.file_lbl.config(text=f"{fname}  ({size_mb:.0f} MB)", fg=self._T["fg"])

        first_date = meta.get('first_date')
        last_date  = meta.get('last_date')

        # Nastavit rozsah platných dat do obou kalendářů
        for de in self._date_entries:
            de.set_valid_range(first_date, last_date)

        # Zobrazit rozsah dat v panelu
        info = ""
        if first_date and last_date:
            info = f"📅 Rozsah: {first_date}  →  {last_date}"
            # Předvyplnit datum od/do
            self.w_date_from.delete(0, tk.END)
            self.w_date_from.insert(0, first_date)
            self.w_date_to.delete(0, tk.END)
            self.w_date_to.insert(0, last_date)
        approx = meta['approx_lines']
        info += f"\n≈ {approx:,} řádků — klikněte Filtrovat pro načtení"
        self.range_lbl.config(text=info)

        # Přidat nové moduly z logu
        all_mods = sorted(set(DEFAULT_MODULES) | set(meta['modules']))
        if set(all_mods) != set(self._toggles.keys()):
            self._build_toggles(all_mods)

        # Vyčistit výsledky z předchozího logu
        self.filtered_entries = []
        self._txt_set("← Nastavte filtry a klikněte na  🔍 Filtrovat a načíst\n\n"
                      f"Soubor: {fname}\n"
                      f"Velikost: {size_mb:.1f} MB\n"
                      f"Rozsah dat: {first_date} → {last_date}\n"
                      f"Přibližný počet řádků: {approx:,}\n")
        self.count_lbl.config(text="")

        self._set_status(
            f"✓ Soubor připraven — {fname}  ({size_mb:.0f} MB, ≈{approx:,} řádků)")

    def _meta_error(self, msg):
        self._load_btn.config(state=tk.NORMAL)
        messagebox.showerror("Chyba", msg)
        self._set_status("✗ Chyba při čtení souboru")

    # ── Filtrování — čte soubor jen pro zadaný rozsah ─────────────────────────

    def _apply(self):
        if not self.log_file:
            messagebox.showinfo("Info", "Nejprve vyberte log soubor.")
            return

        # Validace vstupů
        date_from = self.w_date_from.get().strip() or None
        date_to   = self.w_date_to.get().strip()   or None
        time_from = self.w_time_from.get().strip()  or None
        time_to   = self.w_time_to.get().strip()    or None

        if date_from:
            try: datetime.strptime(date_from, '%Y-%m-%d')
            except ValueError:
                messagebox.showerror("Chyba", f"Neplatné datum od: '{date_from}'\nPoužijte RRRR-MM-DD")
                return
        if date_to:
            try: datetime.strptime(date_to, '%Y-%m-%d')
            except ValueError:
                messagebox.showerror("Chyba", f"Neplatné datum do: '{date_to}'\nPoužijte RRRR-MM-DD")
                return
        if time_from:
            try: datetime.strptime(time_from, '%H:%M:%S')
            except ValueError:
                messagebox.showerror("Chyba", f"Neplatný čas od: '{time_from}'\nPoužijte HH:MM:SS")
                return
        if time_to:
            try: datetime.strptime(time_to, '%H:%M:%S')
            except ValueError:
                messagebox.showerror("Chyba", f"Neplatný čas do: '{time_to}'\nPoužijte HH:MM:SS")
                return

        active_mods = self._get_active_modules()
        all_mods    = set(self._toggles.keys())
        mods_filter = active_mods if active_mods != all_mods else None

        lvl_filter  = self.v_level.get()
        if lvl_filter == "Vše": lvl_filter = None

        search_text  = self.v_search.get().strip() or None
        search_case  = self.v_case.get()
        search_regex = self.v_regex.get()

        # Zakázat tlačítka, zobrazit progress
        self._load_btn.config(state=tk.DISABLED)
        self._btn_filter.config(state=tk.DISABLED)
        self._prog_frame.pack(fill=tk.X, before=self._sep2)
        self._prog_canvas.pack(fill=tk.X)
        self._set_progress(0)
        self._set_status("⏳ Načítám a filtruji log ...")
        self._txt_set("⏳ Načítám...\n")

        def worker():
            try:
                def progress(pct):
                    self.root.after(0, self._set_progress, pct)
                    self.root.after(0, self._set_status,
                        f"⏳ Filtruji log ... {pct} %")

                results = stream_filter(
                    self.log_file,
                    date_from=date_from, date_to=date_to,
                    time_from=time_from, time_to=time_to,
                    active_modules=mods_filter,
                    level_filter=lvl_filter,
                    search_text=search_text,
                    search_case=search_case,
                    search_regex=search_regex,
                    progress_cb=progress,
                )
                self.root.after(0, self._filter_done, results)
            except Exception as ex:
                self.root.after(0, self._filter_error, str(ex))

        threading.Thread(target=worker, daemon=True).start()

    def _filter_done(self, results):
        self.filtered_entries = results
        self._prog_frame.pack_forget()
        self._load_btn.config(state=tk.NORMAL)
        self._btn_filter.config(state=tk.NORMAL)

        n = len(results)
        self.count_lbl.config(
            text=f"Nalezeno: {n:,} záznamů",
            fg="#e74c3c" if n == 0 else self._T["fg_dim"])
        self._set_status(f"✓ Hotovo — nalezeno {n:,} záznamů")

        # Zobrazit výsledky a statistiky ve vlákně
        self.root.after(20, self._redraw)
        self.root.after(50, self._run_stats_thread)

    def _filter_error(self, msg):
        self._prog_frame.pack_forget()
        self._load_btn.config(state=tk.NORMAL)
        self._btn_filter.config(state=tk.NORMAL)
        messagebox.showerror("Chyba filtrování", msg)
        self._set_status("✗ Chyba při filtrování")

    def _clear(self):
        if self.log_meta:
            self.w_date_from.delete(0, tk.END)
            self.w_date_from.insert(0, self.log_meta.get('first_date',''))
            self.w_date_to.delete(0, tk.END)
            self.w_date_to.insert(0, self.log_meta.get('last_date',''))
        else:
            self.w_date_from.delete(0, tk.END)
            self.w_date_to.delete(0, tk.END)
        self.w_time_from.delete(0, tk.END)
        self.w_time_to.delete(0, tk.END)
        self.v_search.set("")
        self.v_case.set(False)
        self.v_regex.set(False)
        self.v_level.set("Vše")
        self._set_all_modules(True)
        self._set_status("✓ Filtry vymazány — klikněte Filtrovat pro načtení")

    # ── Zobrazení výsledků ────────────────────────────────────────────────────

    def _redraw(self):
        color  = self.v_color.get()
        q      = self.v_search.get().strip()
        use_re = self.v_regex.get()
        case   = self.v_case.get()

        MAX_DISPLAY = 50_000
        entries   = self.filtered_entries
        truncated = len(entries) > MAX_DISPLAY
        display   = entries[:MAX_DISPLAY] if truncated else entries

        self._txt_set("⏳ Připravuji zobrazení...\n")

        def worker():
            lines      = []
            tag_ranges = []
            pos        = 0
            for e in display:
                if e_date(e):
                    line = f"{e_date(e)} {e_time_ms(e) or e_time(e)}  {e_module(e):<14}  {e_msg(e)}\n"
                else:
                    line = e_raw(e) + '\n'
                if color and e_date(e):
                    tag_ranges.append((e_level(e), pos, pos + len(line) - 1))
                lines.append(line)
                pos += len(line)

            full_text = "".join(lines)
            suffix = ""
            if truncated:
                suffix = (f"\n⚠ Zobrazeno prvních {MAX_DISPLAY:,} z {len(entries):,} "
                          "záznamů. Zpřesněte filtry pro zúžení výsledků.\n")
            self.root.after(0, self._redraw_apply,
                            full_text, suffix, tag_ranges, color, q, use_re, case)

        threading.Thread(target=worker, daemon=True).start()

    def _redraw_apply(self, full_text, suffix, tag_ranges, color, q, use_re, case):
        self.txt.config(state=tk.NORMAL)
        self.txt.delete('1.0', tk.END)
        self.txt.insert('1.0', full_text)
        if suffix: self.txt.insert(tk.END, suffix)
        if color:
            for tag, s, e in tag_ranges:
                self.txt.tag_add(tag, f"1.0+{s}c", f"1.0+{e}c")
        if q and color:
            try:
                content = self.txt.get('1.0', tk.END)
                if use_re:
                    for m in re.finditer(q, content, 0 if case else re.IGNORECASE):
                        self.txt.tag_add('HIGHLIGHT', f"1.0+{m.start()}c", f"1.0+{m.end()}c")
                else:
                    text   = content if case else content.lower()
                    needle = q if case else q.lower()
                    idx = 0
                    while True:
                        p = text.find(needle, idx)
                        if p == -1: break
                        self.txt.tag_add('HIGHLIGHT', f"1.0+{p}c", f"1.0+{p+len(q)}c")
                        idx = p + 1
            except Exception: pass
        self.txt.config(state=tk.DISABLED)

    def _txt_set(self, text: str):
        self.txt.config(state=tk.NORMAL)
        self.txt.delete('1.0', tk.END)
        self.txt.insert('1.0', text)
        self.txt.config(state=tk.DISABLED)

    def _toggle_wrap(self):
        self.txt.config(wrap=tk.WORD if self.v_wrap.get() else tk.NONE)

    # ── Statistiky ────────────────────────────────────────────────────────────

    def _run_stats_thread(self):
        entries = self.filtered_entries
        if not entries: return
        def worker():
            text = self._compute_stats(entries)
            self.root.after(0, self._show_stats, text)
        threading.Thread(target=worker, daemon=True).start()

    def _compute_stats(self, entries) -> str:
        total = len(entries)
        level_cnt: dict = {}; module_cnt: dict = {}
        dates: set = set(); first_dt = None; last_dt = None
        for e in entries:
            level_cnt[e_level(e)] = level_cnt.get(e_level(e), 0) + 1
            if e_module(e): module_cnt[e_module(e)] = module_cnt.get(e_module(e), 0) + 1
            if e_date(e):
                dates.add(e_date(e))
                if e_time(e):
                    try:
                        dt = datetime.strptime(f"{e_date(e)} {e_time(e)}", '%Y-%m-%d %H:%M:%S')
                        if first_dt is None or dt < first_dt: first_dt = dt
                        if last_dt  is None or dt > last_dt:  last_dt  = dt
                    except Exception: pass
        lines = ["="*62, f"  Soubor:           {os.path.basename(self.log_file)}", "="*62,
                 f"  Nalezeno záznamů: {total:,}", f"  Unikátních dnů:   {len(dates)}"]
        if first_dt: lines.append(f"  Od:               {first_dt.strftime('%Y-%m-%d %H:%M:%S')}")
        if last_dt:  lines.append(f"  Do:               {last_dt.strftime('%Y-%m-%d %H:%M:%S')}")
        lines += ["", "  Záznamy podle úrovně:", "  "+"-"*48]
        max_c = max(level_cnt.values()) if level_cnt else 1
        for lvl in ["ERROR","FATAL","WARNING","INFO","DEBUG","TRACE","RAW"]:
            c = level_cnt.get(lvl, 0)
            if c: lines.append(f"  {lvl:<10} {c:>9,}  {'█'*max(1,c*28//max_c)}")
        lines += ["", "  Záznamy podle modulu:", "  "+"-"*48]
        max_m = max(module_cnt.values()) if module_cnt else 1
        for mod, c in sorted(module_cnt.items(), key=lambda x: -x[1]):
            lines.append(f"  {mod:<18} {c:>9,}  {'█'*max(1,c*28//max_m)}")
        lines.append("="*62)
        return '\n'.join(lines)

    def _show_stats(self, text: str):
        self.stats_txt.config(state=tk.NORMAL)
        self.stats_txt.delete('1.0', tk.END)
        self.stats_txt.insert(tk.END, text)
        self.stats_txt.config(state=tk.DISABLED)

    # ── Progress bar ──────────────────────────────────────────────────────────

    def _set_progress(self, pct):
        self._prog_canvas.update_idletasks()
        w = self._prog_canvas.winfo_width()
        if w > 1:
            self._prog_canvas.coords(self._prog_bar, 0, 0, int(w * pct / 100), 5)

    # ── Export ────────────────────────────────────────────────────────────────

    def _export(self):
        if not self.filtered_entries:
            messagebox.showinfo("Export", "Nejprve proveďte filtrování.")
            return
        base = os.path.splitext(os.path.basename(self.log_file))[0] if self.log_file else "export"
        path = filedialog.asksaveasfilename(
            title="Uložit export", defaultextension=".txt",
            initialfile=f"{base}_export.txt",
            filetypes=[("Textové soubory","*.txt"),("Všechny soubory","*.*")])
        if not path: return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(f"# Agilax Log Export\n# Soubor: {self.log_file}\n")
                f.write(f"# Exportováno: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"# Záznamů: {len(self.filtered_entries):,}\n{'='*80}\n\n")
                for e in self.filtered_entries:
                    if e_date(e):
                        f.write(f"{e_date(e)} {e_time_ms(e) or e_time(e)}  "
                                f"{e_module(e):<14}  {e_msg(e)}\n")
                    else:
                        f.write(e_raw(e) + '\n')
            messagebox.showinfo("Export",
                f"✓ Exportováno {len(self.filtered_entries):,} záznamů\n{path}")
            self._set_status(f"✓ Export uložen: {os.path.basename(path)}")
        except Exception as ex:
            messagebox.showerror("Chyba exportu", str(ex))

    # ── Tmavý / světlý režim ──────────────────────────────────────────────────

    def _toggle_theme(self):
        self._dark = not self._dark
        self._T = THEMES["dark"] if self._dark else THEMES["light"]
        T = self._T
        if TTKBS:
            try: self.root.style.theme_use(T["ttkbs_theme"])
            except Exception: pass
        self._theme_btn.config(text="\u2600 Světlý" if self._dark else "\u263d Tmavý",
                               bg=T["sep"], fg=T["fg"], activebackground=T["bg"])
        self._apply_theme_all(T)
        self._setup_tags()
        self.txt.config(bg=T["result_bg"], fg=T["result_fg"],
                        selectbackground=T["fg_header"], selectforeground=T["bg2"])
        self.stats_txt.config(bg=T["result_bg"], fg=T["result_fg"])
        for de in self._date_entries: de.apply_theme(T)
        for t in self._toggles.values(): t.apply_theme(T)
        self._btn_all_on.config(bg=T["btn_on"], fg=T["bg2"], activebackground=T["btn_on_hov"])
        self._btn_all_off.config(bg=T["btn_off"], fg=T["bg2"], activebackground=T["btn_off_hov"])
        self._redraw()

    def _apply_theme_all(self, T):
        """Přebarvit všechny základní widgety."""
        self.root.configure(bg=T["bg"])
        for w in [self._top, self._status_frame]:
            w.config(bg=T["bg_header"])
            for c in w.winfo_children():
                try: c.config(bg=T["bg_header"], fg=T["fg_header"])
                except Exception: pass
        self._sep1.config(bg=T["sep"]); self._sep2.config(bg=T["sep"])
        self._brand.config(bg=T["bg"])
        self._brand_lbl1.config(bg=T["bg"], fg=T["fg"])
        self._brand_lbl2.config(bg=T["bg"], fg=T["fg_dim"])
        self._left_outer.config(bg=T["bg"])
        self._lc.config(bg=T["bg"])
        self._left.config(bg=T["bg"])
        self._right.config(bg=T["bg"])
        self.tab_filter.config(bg=T["bg"])
        self.tab_stats.config(bg=T["bg"])
        self._prog_frame.config(bg=T["bg"])
        self._prog_canvas.config(bg=T["sep"])
        self._prog_canvas.itemconfig(self._prog_bar, fill=T["fg_header"])
        self._recurse_theme(self._left, T)
        self._recurse_theme(self._right, T)

    def _recurse_theme(self, widget, T):
        cls = widget.__class__.__name__
        try:
            if cls == 'Frame':       widget.config(bg=T["bg"])
            elif cls == 'Label':     widget.config(bg=T["bg"], fg=T["fg"])
            elif cls == 'Checkbutton':
                widget.config(bg=T["bg"], fg=T["fg"],
                              selectcolor=T["bg"], activebackground=T["bg"])
            elif cls == 'Entry':
                widget.config(bg=T["entry_bg"], fg=T["entry_fg"],
                              insertbackground=T["entry_fg"],
                              highlightbackground=T["sep"])
            elif cls == 'Canvas':    widget.config(bg=T["bg"])
        except Exception: pass
        for child in widget.winfo_children():
            self._recurse_theme(child, T)

    def _set_status(self, text):
        self.status.config(text=text)
        self.root.update_idletasks()


# ─────────────────────────────────────────────────────────────────────────────
#  Spuštění
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if TTKBS:
        root = ttk.Window(themename="litera")
    else:
        root = tk.Tk()
    AgilaxLogFilterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
