"""AIMOD v2 - tkinter front end."""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import queue
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import memory_map as mm
from .game import Game
from .adjust import CrewChief
from .behavior import SAVE_FUEL, BehaviorConfig, BehaviorEngine, gather as behavior_inputs
from .behavior_mem import BehaviorWriter

CHASSIS_VALUE = 5878 + 12 * 10                    # car block: the session value of the chassis rating
from .aisetup import ai_setup
from .gearbox import Gearbox, default_on as gearbox_default_on, offered as gearbox_offered
from .pitcrew import PitCrew, default_modern
from .pitgate import PitGate
from .pitlog import COLUMNS as PITLOG_COLUMNS, PitLog
from .presets import Preset, PresetStore, parse_stage_ends
from .companion import CompanionFeed
from .strategy import (DRAFT_RULES, OTHER_RULES, ROAD_COURSE, SHORT_TRACK, SUPERSPEEDWAY, INTERMEDIATE,
                       TRACK_TYPE_NAMES, PitEvent, StrategyEngine, StrategyParams, TeamRules)
from .overlay import PitBoardWindow
from .pitboard import payload as board_payload
from .companion_art import AUTO, ArtData, companion_dir
from .crewview import CrewView
from .humancalls import Desk
from .radio import Radio, tv_slot
from .radio_overlay import TalkIndicator
from .radio_tab import RadioTab
from .scrollframe import scrolled
from .tiremodel import TireModel
from .wavelog import WaveLog
from .table import Column, ColorTable, Row
from .teams import RulesDialog, TeamsDialog, normalize as normalize_teams
from .webserver import BoardServer, lan_ip
from .widget_html import WIDGET_FILE, WIDGET_HTML

VERSION = "2.1.0"
TICK_MS = 150
ATTACH_RETRY_S = 2.0
TRACK_CHECK_S = 1.0
FEED_LAP_HOLD_S = 10.0
MONITOR_REFRESH_S = 0.4
DRAFTING_TRACKS = ("talladega", "daytona")
ROAD_COURSE_NAMES = ("watkins", "sonoma", "infineon", "road america", "roadamerica", "road_america", "cota",
                     "circuit of the americas", "americas", "mid-ohio", "midohio", "mid_ohio", "portland", "roval",
                     "chicago street", "chicago_street", "indianapolis_gp", "indygp", "indy_gp", "mosport",
                     "canadian tire", "canadiantire", "montreal", "gilles", "laguna", "road atlanta", "barber",
                     "daytona_rc", "daytona_road", "roadcourse", "road_course", "road course", "_rc_", "glen")
SHORT_TRACK_M = 1530.0           # under ~0.95 mile: short track

BG, PANEL, FIELD, FG, DIM, ACCENT, BAD = "#1a1a1a", "#252525", "#111111", "#e6e6e6", "#8a8a8a", "#00d26a", "#ff5c5c"
GOOD_C, OK_C, BAD_C = "#5ad17a", "#ffd54f", "#ff5c5c"
KIND_COLORS = {"out": "#5f5f5f", "pitting": "#ffd54f", "stayout": "#4fc3f7",
               "finished": "#81c784", "damage": "#ff6e6e", "green": FG, "player": "#b39ddb"}

MONITOR_COLUMNS = [
    Column("pos", "Pos", 40, "e", "Running position. Changes only when a car crosses the start/finish line, "
                                  "like real scoring (from game memory)."),
    Column("num", "#", 38, "e", "Car number"),
    Column("name", "Driver", 150, "w", "Hover a driver for make and memory slot. Click a row to highlight it."),
    Column("lap", "Lap", 40, "e", "Lap this car is on"),
    Column("gap", "Gap", 58, "e", "Seconds behind the leader (game time), or laps down. Only updated "
                                  "under green; frozen while a caution is out."),
    Column("rating", "Rat", 36, "e", "Strategy rating (0-100). Higher = follows the plan more often and pits "
                                     "closer to the ideal lap"),
    Column("grip", "Grip", 52, "e"),
    Column("wear_l", "Left", 52, "e", "Average left-side tire wear"),
    Column("wear_r", "Right", 52, "e", "Average right-side tire wear"),
    Column("prob_l", "L-Prob", 52, "e", "Left wear vs. your Left threshold. Drives the tire choice when the "
                                        "car pits"),
    Column("prob_r", "R-Prob", 52, "e", "Right wear vs. your Right threshold"),
    Column("damage", "Damage", 56, "e", "Damage since last green flag"),
    Column("urgency", "Urgency", 58, "e", "Caution pit urgency (0-100), built from grip loss"),
    Column("fuel", "Tank laps", 64, "e", "Green-flag laps run since the last stop (counts up). Colour: how much "
                                          "of the tank is used, from your tank size"),
    Column("fuelpct", "Fuel", 50, "e", "Fuel in the tank, % of a full tank - the car's real fuel level read from "
                                       "NR2003 (~ = estimated from laps since its last stop)"),
    Column("fuelleft", "Fuel laps", 62, "e", "Laps of fuel left: real fuel level / this car's fuel mileage"),
    Column("mileage", "Laps/tank", 66, "e", "Laps a full tank lasts for this car, learned from its own real fuel "
                                            "use on clean green-flag laps (~ = field average until it has its own)"),
    Column("fuelst", "To finish", 92, "w", "Laps of fuel over (+) or short (-) of the finish. +4 = makes it with "
                                           "4 laps to spare, -37 = 37 laps short (needs a stop)"),
    Column("pace", "Pace", 52, "e", "Seconds per lap slower (+) or faster (-) than the cars around it (3 places "
                                   "either side, same lap), from its last clean green-flag laps. A worn car "
                                   "that keeps pace is less likely to pit for tires."),
    Column("falloff", "Falloff", 56, "e", "How much slower it is now than early in this run on these tires "
                                         "(% of a lap)."),
    Column("tires", "Tires", 52, "w", "Tire sets left per side (L/R) when a limit is set"),
    Column("stops", "Stops", 40, "e"),
    Column("last", "Last pit", 92, "w", "Lap and service of the last stop. D = damage repairs, F = fuel, "
                                        "L = left side, R = right side, L/R = four tires"),
    Column("next", "Next pit", 170, "w", "Planned green-flag stop (race lap), its likely service (~, projected "
                                         "from each side's wear rate - still rolled from L/R-Prob when the car "
                                         "pits) and the team's stint plan: short-pit, split or stretch. "
                                         "GAME = the game pits it, at the latest on that lap. STAGE n BREAK = waits for the stage "
                                         "caution; flip = pits just before the stage ends to stay out at the break. "
                                         "Under caution it keeps showing the plan from before the caution"),
    Column("pen", "Flag", 54, "w", "BLACK = black flag (penalty, from game memory; stops made while "
                                   "black-flagged are logged with reason BLACK FLAG). FREE PASS = RaceControl's lucky dog (gets its "
                                   "lap back this caution). WAVE = given the wave "
                                   "around (from RaceControl's or bc_wave's log), shown for a few laps."),
    Column("behavior", "Behavior", 92, "w", "Driver behavior (when on): SAVE FUEL, SAVE TIRES, COOL TIRES or PUSH. "
                                            "Why each car switched: the BEHAVIOR lines in aimod.log."),
    Column("status", "State / service", 300, "w"),
]

STOP_TIP = ("Each stop: race lap, service and service time (car stationary in its stall, game seconds). "
            "D = damage repairs, F = fuel, L = left side, R = right side, L/R = four tires. "
            "Green = gained places, red = lost. Hover for details.")

PARAM_FIELDS = {  # key: (label, type, tooltip)
    "wear_r": ("Right tire wear limit", float,
        "The right-side wear (Right column) you call 'used up'. L/R-Prob = wear / this. At 100% a tire change "
        "is certain when the car pits; it also flags cars whose tires are going away. Set it to the wear cars "
        "reach at the end of a normal full-tank green run, so tires and fuel run out together. Easiest: run a "
        "practice or race, then press 'Apply learned thresholds'. Too low = everyone looks worn early."
    ),
    "wear_l": ("Left tire wear limit", float,
        "Same as the right limit, for the left side (usually wears less on ovals). Use the left wear at "
        "the end of a normal full-tank run."
    ),
    "max_tire_sets": ("Tire sets (0 = no limit)", int,
                      "Maximum sets of tires per car for the race, counting the starting set. 4 tires uses one "
                      "pair from each side, 2 tires uses one pair from that side. When a side runs out, stops "
                      "become 2 tires or fuel only."),
    "tank_laps": ("Tank size (laps)", float, "Laps a full tank lasts. Used when Fuel source is Manual."),
    "finish_fuel": ("Fuel left at the finish (laps)", float,
                    "The last stop before the finish or a stage break only takes the fuel needed to get there with "
                    "this many laps spare, so cars finish near empty and that stop is shorter. Typical 2-3."),
    "fuel_margin": ("Fuel cushion (laps)", float,
        "Spare laps of fuel a crew keeps in hand when deciding if a car makes it. With AIMOD reading the real "
        "fuel, 1-2 is plenty; higher = earlier, safer stops and more fuel left at the finish."
    ),
    "urgency_factor": ("Tire urgency factor", float,
        "Caution only. Grip lost since the last green x this = the Urgency column (0-100); the higher, the "
        "likelier a car pits for tires under caution when fuel doesn't force it. Tune by watching Urgency at "
        "the end of a typical green run: worn cars should be around 60-100, fresh ones under 30."
    ),
    "damage_threshold": ("Must-repair damage level", float,
                         "Damage (since last green) above which a car is forced to pit for repairs."),
    "dispersion_divisor": ("Crew chief call spread", float,
        "How far a crew chief strays from the ideal lap for early stops: up to (100 - rating) / this laps "
        "either way. 5 = a 50-rated crew can be 10 laps off, a 90-rated 2. Higher = tighter, more uniform "
        "plans. Doesn't affect the final stop (that is timed by 'Fuel left at the finish')."
    ),
    "min_stop_gap": ("Min laps between stops", float,
                     "A green-flag stop the game asks for within this many laps of the car's last stop is called off "
                     "(stops the car pitting twice in a row). 0 = off."),
    "lead_pack_gap": ("Lead pack gap (seconds)", float,
                      "Green flag. Lead-lap cars within this many seconds of the leader are the lead pack: they "
                      "lean towards the even split, and cars running within twice this gap of a car that pits "
                      "may answer it. Under caution the pack is frozen as it was when the caution came out, "
                      "for following the leader's call."),
    "threat_positions": ("Pit pressure window (positions)", int,
                         "Caution. When a car can make it on fuel, the cars this many positions behind it are "
                         "weighed against its own grip: fresher cars right behind push it to pit, worn ones "
                         "make staying out safer."),
    "track_position_bias": ("Track position vs tires (0-100)", int,
                            "How much teams value track position over fresh tires in late-race calls. 50 = "
                            "neutral, higher = more stay-outs and 2-tire stops, lower = more 4-tire stops."),
    "stint_spread": ("Pit window spread (%)", int,
                     "How far apart short-pit, split and stretch stops are, as % of the fuel window. 0 = every "
                     "team pits on the same lap, 100 = short-pit at the start of the window, stretch at the end."),
    "ss_stint_spread": ("Superspeedway pit spread (%)", int,
                        "Same as the pit window spread, for superspeedways. Teams there pit in tight groups: every "
                        "car on the same plan pits on the same lap."),
    "ss_topoff_pct": ("Superspeedway top-off threshold (%)", int,
                      "Superspeedways, caution: a car pits for fuel when it can't reach the next stage break / the "
                      "finish and a fill now gets it there or saves a stop before it; a car whose fuel reaches the "
                      "break stays out (the break is a free stop). It also always pits with less than (100 - this) % "
                      "of a tank left: 75 = below 25 %. Aggressive or hopeless cars may come back for a second "
                      "splash, only with real room in the tank and while still short of the next chance."),
    "worn_pct": ("Worn tire threshold (% lap-time loss)", float,
        "How much slower than its own early-run laps a car must be before its tires count as worn. Used by "
        "the tire model (the learned thresholds come from the wear at the end of full green runs). Typical 1-2% (0.3-0.6 s on a 30 s lap). Big values "
        "(10%+) mean 'never worn', so nothing gets learned."
    ),
    "fuel_reach": ("Late caution pit threshold (laps)", float,
        "Caution only. If filling up now would leave the car within this many laps of making the finish, it "
        "must pit (one splash later gets it home). Typical: 10-20% of a tank (e.g. 7-12 on a 65-lap tank). "
        "Higher = more cars pit under late cautions."
    ),
    "stage_early": ("Stage early caution window (laps)", int,
                    "A caution this many laps (or fewer) before a stage end is treated as that stage's break, as "
                    "stage racing companion apps that end a stage early under caution do. 0 = only a caution at the stage end."),
    "flip_pct": ("Stage flip — cars that jump the break (%)", int,
                 "Cars outside the top 8 that can reach well into the next stage on fuel may pit a few laps "
                 "before the stage ends, stay out at the break and restart up front. 0 = nobody flips."),
    "behavior_strength": ("Driver behavior intensity (%)", int,
                          "Size of every behavior's effect. 100 = the tested sizes (saving tires: about 1.5 % less "
                          "cornering grip and 25 % less wear; saving fuel: 2 % less power and 15 % less fuel; "
                          "pushing: 1.5 % more grip, 25 % more wear, 8 % more fuel). Default 70. 50 = half, 200 = double."),
    "behavior_late_pct": ("Final push — last (%) of the race", int,
                          "In this final share of the race every car that has the fuel pushes (not the ones that "
                          "must save to finish). 10 = the last 10 %. 0 = off."),
}
BOOL_FIELDS = ("oem_strategy", "stint_strategy", "lapped_wave",
               "team_strategy", "follow_player", "behavior_on", "behavior_reactions",
               "behavior_teams")

log = logging.getLogger("aimod")


def base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(self.format(record))


class Tooltip:
    def __init__(self, widget, text: str):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, bg="#333333", fg=FG, justify="left", wraplength=340,
                 font=("Segoe UI", 9), padx=8, pady=5, relief="solid", bd=1).pack()

    def hide(self, _=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class App:
    def __init__(self):
        self.base = base_dir()
        self.settings_path = os.path.join(self.base, "settings.json")
        self.settings = self._load_settings()
        self.store = PresetStore(self.settings.get("presets_dir") or os.path.join(self.base, "presets"))
        self.pitlog = PitLog(os.path.join(self.base, "logs"))
        self.game = Game()
        if isinstance(self.settings.get("make_names"), list):
            self.game.make_names = [str(x) for x in self.settings["make_names"]]
        self.tire_model = TireModel(os.path.join(self.base, "tire_models.json"))
        self.engine = StrategyEngine(tire_model=self.tire_model)
        self.engine.crew_chief = CrewChief()          # chassis adjustments on AI stops (from the AI setup)
        self.behavior = BehaviorEngine()                # driver behavior: save fuel / tires, cool tires, push
        self.behavior_writer = BehaviorWriter()
        self._chassis_base: dict[int, float] = {}    # car slot -> its session chassis rating (before adjustments)
        self._model_saved = time.monotonic()
        self.feed = CompanionFeed(self.settings["companion_host"], self.settings["companion_port"])
        self.artdata = ArtData([os.path.join(self.base, "art")], companion=self.settings["use_companion"])
        self.artdata.refresh(force=True)

        self.log_q: queue.Queue = queue.Queue()
        self._setup_logging()

        self.params = Preset()
        self.loaded_name: str | None = None
        self.loaded_dict: dict | None = None
        self.track = ""
        self._last_attach_try = 0.0
        self._last_track_check = 0.0
        self._last_session = None
        self._last_laps = 0.0
        self._last_lap_src = None
        self._feed_laps = None
        self._last_monitor = 0.0
        self._last_rows = []
        self.pit_events: list[PitEvent] = []
        self._last_snap = None
        self._last_leader = 0
        self._last_clock = 0.0
        self._drop_ticks = 0
        self._flagged: set[int] = set()
        self.mod = ""                              # NR2003 series (mod) folder of the loaded session
        self.pitcrew = PitCrew()
        self.pitgate = PitGate()
        self._gate_armed = False
        self._gate_logged: dict[int, float] = {}   # slot -> game time the gate's last hold was logged
        self._gate_held_total = 0
        self._crew_applied = None                  # (pid, modern) the crew was last set for
        self.gearbox = Gearbox()
        self._gear_pid = None                      # NR2003 process the gear count was set in
        self._gear_want = None                     # (pid, gears) last applied
        self._mem_flags: set[int] = set()
        self._flag_check = 0.0
        self._pit_dirty = True
        self.radio = Radio(self.settings.setdefault("radio", {}),    # crew chief / spotter calls (Radio tab)
                           voices_dir=os.path.join(self.base, "voices"))
        self.radio.teams = self._radio_teams
        self._radio_error = 0.0
        self.crew_view = CrewView()                    # crew chief on a phone (beta, settings "crew_beta")
        # beta features only for testers: a beta.txt file next to AIMOD.exe switches them on
        self.beta = os.path.isfile(os.path.join(self.base, "beta.txt"))
        self.desk = Desk(log=log.info)                 # phones running AI cars' caution calls (beta)
        self._crew_at = 0.0
        self._crew_error = 0.0

        self.root = tk.Tk()
        self.root.title(f"AIMOD v{VERSION} - NR2003 AI Pit Strategy")
        self.root.geometry(self.settings.get("geometry", "1440x820"))
        self.root.configure(bg=BG)
        self.root.minsize(1100, 600)
        self._style()
        self._build()
        if self.settings.get("setup_collapsed"):
            self._toggle_setup(True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        last = self.settings.get("last_preset")
        if last and last in self.store.names():
            self.load_preset(last, quiet=True)
        else:
            self._set_fields(self.params)
            self._mark_clean(None)
        if self.settings["use_companion"]:
            self.feed.start()
        self.wavelog = WaveLog(self.settings.get("bc_wave_log") or None)
        self._pb_error = False
        self._tick_errors: dict[str, float] = {}
        self._last_sp = None
        self._apply_make_names()
        self.board_server = BoardServer(int(self.settings.get("board_port", 8767)), artdata=self.artdata,
                                        lan=bool(self.settings.get("web_lan", False)))
        self.board_server.radio = self.radio          # the scanner for a phone on the network (via another app)
        self.board_server.desk = self.desk if self.beta else None
        self.board_server.start()
        self._update_live_urls()
        self.pitboard = PitBoardWindow(self.root, self.settings["pitboard"], on_moved=self._save_settings,
                                       artdata=self.artdata, on_resized=self._pb_resized)
        if self.settings["pitboard"].get("enabled"):
            self.pitboard.show()
        self.talk_ind = TalkIndicator(self.root, self.radio, artdata=self.artdata, on_moved=self._save_settings)
        self.radio_tab.indicator = self.talk_ind
        if self.settings["use_companion"]:
            self._sync_companion_widget()
        self.root.after(TICK_MS, self.tick)

    # ------------------------------------------------------------ settings
    def _load_settings(self) -> dict:
        s = {"auto_load": True, "patches": ["no_game_plan_stops"], "restore_on_exit": True,
             "companion_host": "127.0.0.1", "companion_port": 8765, "hide_out": False,
             "color_levels": True, "pit_view": "driver", "pit_sort": "Running order",
             "make_names": ["Chevrolet", "Dodge", "Ford", "Toyota"],
             "use_companion": bool(companion_dir()),
             "pitboard": {}}
        try:
            with open(self.settings_path, encoding="utf-8") as f:
                s.update(json.load(f))
        except FileNotFoundError:
            pass
        except Exception as e:
            print("settings.json unreadable:", e)
        if not s.get("patches_v37"):              # new in 3.7: on by default for existing settings too
            s["patches_v37"] = True
            if "no_game_plan_stops" not in s["patches"]:
                s["patches"] = list(s["patches"]) + ["no_game_plan_stops"]
        if not s.get("patches_v38"):              # 2.2: the condition patch and the pit gate, on for everyone
            s["patches_v38"] = True
            if "no_game_condition_stops" not in s["patches"]:
                s["patches"] = list(s["patches"]) + ["no_game_condition_stops"]
            s["pit_gate"] = True
        if not s.get("patches_v39"):              # caution stops repair (the game's pace car repair halt), on for all
            s["patches_v39"] = True
            if "caution_repairs" not in s["patches"]:
                s["patches"] = list(s["patches"]) + ["caution_repairs"]
        _removed_patches = {"pit_takeover", "pit_takeover_extra", "no_game_repair_stops"}
        s["patches"] = [k for k in s["patches"] if k not in _removed_patches]
        pb = {"enabled": False, "x": 40, "y": 160, "scale": 1.0, "width": 280, "opacity": 1.0, "max_rows": 10,
              "hold": 12, "auto_hide": True, "compact": False}
        pb.update(s.get("pitboard") or {})
        s["pitboard"] = pb
        return s

    def _save_settings(self):
        self.settings["geometry"] = self.root.geometry()
        self.settings["sort_col"] = self.monitor.sort_col
        self.settings["sort_rev"] = self.monitor.sort_rev
        try:
            with open(self.settings_path, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=4)
        except Exception as e:
            log.warning("Could not save settings: %s", e)

    def _setup_logging(self):
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        qh = QueueHandler(self.log_q)
        qh.setFormatter(fmt)
        root.addHandler(qh)
        try:
            from logging.handlers import RotatingFileHandler
            fh = RotatingFileHandler(os.path.join(self.base, "aimod.log"), maxBytes=2_000_000, backupCount=2,
                                     encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError:
            pass

    # ---------------------------------------------------------------- style
    def _style(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=FG, fieldbackground=FIELD, bordercolor="#3a3a3a",
                     font=("Segoe UI", 9))
        st.configure("TLabelframe", background=PANEL, bordercolor="#3a3a3a")
        st.configure("TLabelframe.Label", background=PANEL, foreground=ACCENT, font=("Segoe UI", 9, "bold"))
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("Panel.TLabel", background=PANEL, foreground=FG)
        st.configure("Dim.TLabel", background=PANEL, foreground=DIM)
        st.configure("Status.TLabel", background="#101010", foreground=FG, padding=(10, 5), font=("Segoe UI", 9, "bold"))
        st.configure("TCheckbutton", background=PANEL, foreground=FG)
        st.configure("TRadiobutton", background=PANEL, foreground=FG)
        st.configure("Bar.TRadiobutton", background=BG, foreground=FG)
        st.map("Bar.TRadiobutton", background=[("active", BG)])
        st.map("TCheckbutton", background=[("active", PANEL)])
        st.map("TRadiobutton", background=[("active", PANEL)])
        st.configure("TEntry", foreground=FG, insertcolor=FG)
        st.configure("Bad.TEntry", foreground=BAD, fieldbackground="#2a1111")
        st.configure("TCombobox", foreground=FG, arrowcolor=FG)
        st.map("TCombobox", fieldbackground=[("!disabled", FIELD)], foreground=[("!disabled", FG)],
               selectbackground=[("!disabled", FIELD)], selectforeground=[("!disabled", FG)])
        st.configure("TButton", background="#333333", foreground=FG, padding=(8, 3))
        st.map("TButton", background=[("active", "#444444"), ("disabled", "#262626")],
               foreground=[("disabled", "#666666")])
        st.configure("Accent.TButton", background="#006b3c")
        st.map("Accent.TButton", background=[("active", "#008a4d")])
        st.configure("Treeview", background=FIELD, fieldbackground=FIELD, foreground=FG, rowheight=20,
                     font=("Consolas", 9))
        st.configure("Treeview.Heading", background="#2c2c2c", foreground=ACCENT, font=("Segoe UI", 9, "bold"))
        st.map("Treeview", background=[("selected", "#1f4f3a")])
        st.configure("TNotebook", background=BG)
        st.configure("TNotebook.Tab", background="#2c2c2c", foreground=FG, padding=(12, 4))
        st.map("TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", ACCENT)])
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)

    # ---------------------------------------------------------------- build
    def _build(self):
        bar = tk.Frame(self.root, bg="#101010")
        bar.pack(fill="x")
        self.lbl_conn = ttk.Label(bar, style="Status.TLabel")
        self.lbl_flag = ttk.Label(bar, style="Status.TLabel")
        self.lbl_laps = ttk.Label(bar, style="Status.TLabel")
        self.lbl_fuel = ttk.Label(bar, style="Status.TLabel")
        self.lbl_track = ttk.Label(bar, style="Status.TLabel")
        self.lbl_preset = ttk.Label(bar, style="Status.TLabel")
        for w in (self.lbl_conn, self.lbl_flag, self.lbl_laps, self.lbl_fuel, self.lbl_track, self.lbl_preset):
            w.pack(side="left")

        self.btn_setup = tk.Button(bar, text="\u2630 Setup", bg="#101010", fg=FG, activebackground="#2c2c2c",
                                   activeforeground=FG, relief="flat", bd=0, padx=10, cursor="hand2",
                                   font=("Segoe UI", 9, "bold"), command=self._toggle_setup)
        self.btn_setup.pack(side="left", before=self.lbl_conn)
        Tooltip(self.btn_setup, "Show or hide the setup panel")
        self.btn_clear = tk.Button(bar, text="\u27f2 New session", bg="#101010", fg=FG, activebackground="#2c2c2c",
                                   activeforeground=FG, relief="flat", bd=0, padx=10, cursor="hand2",
                                   font=("Segoe UI", 9, "bold"), command=self._clear_clicked)
        self.btn_clear.pack(side="right")
        Tooltip(self.btn_clear, "Clear AIMOD's data from the previous race. AIMOD also does this by itself when "
                                "a new race starts (lap count or race clock restarts, session or track changes).")
        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=6, pady=6)
        holder = ttk.Frame(body, style="Panel.TFrame")
        right = ttk.Frame(body)
        body.add(holder, weight=0)
        self.body, self.setup_holder = body, holder
        body.add(right, weight=1)
        left = self._scrollable(holder)
        self._build_settings(left)
        self._build_tabs(right)

    def _scrollable(self, holder) -> ttk.Frame:
        """Settings column that scrolls when the window is shorter than it."""
        canvas = tk.Canvas(holder, bg=PANEL, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, style="Panel.TFrame", padding=6)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)

        def on_inner(_=None):
            canvas.configure(scrollregion=canvas.bbox("all"), width=inner.winfo_reqwidth())
            fits = inner.winfo_reqheight() <= canvas.winfo_height()
            if fits:
                sb.pack_forget()
                canvas.yview_moveto(0)
            elif not sb.winfo_ismapped():
                sb.pack(side="right", fill="y", before=canvas)
        inner.bind("<Configure>", on_inner)
        canvas.bind("<Configure>", lambda e: (canvas.itemconfigure(win, width=e.width), on_inner()))
        canvas.pack(side="left", fill="both", expand=True)

        def wheel(e):
            if sb.winfo_ismapped():
                canvas.yview_scroll(int(-e.delta / 120), "units")
        holder.bind("<Enter>", lambda _: holder.bind_all("<MouseWheel>", wheel))
        holder.bind("<Leave>", lambda _: holder.unbind_all("<MouseWheel>"))
        return inner

    def _entry_row(self, parent, row: int, key: str):
        label, _, tip = PARAM_FIELDS[key]
        lbl = ttk.Label(parent, text=label, style="Panel.TLabel")
        lbl.grid(row=row, column=0, sticky="w", pady=2)
        var = tk.StringVar()
        ent = ttk.Entry(parent, textvariable=var, width=10, justify="right")
        ent.grid(row=row, column=1, sticky="e", pady=2, padx=(8, 0))
        Tooltip(lbl, tip)
        Tooltip(ent, tip)
        var.trace_add("write", lambda *_: self._on_param_change())
        self.vars[key] = var
        self.entries[key] = ent

    def _build_settings(self, parent):
        self.vars: dict[str, tk.Variable] = {}
        self.entries: dict[str, ttk.Entry] = {}

        pf = ttk.LabelFrame(parent, text="Preset", padding=8)
        pf.pack(fill="x")
        self.cmb_preset = ttk.Combobox(pf, values=self.store.names(), width=34)
        self.cmb_preset.grid(row=0, column=0, columnspan=4, sticky="we")
        self.cmb_preset.bind("<<ComboboxSelected>>", lambda e: self.load_preset(self.cmb_preset.get()))
        Tooltip(self.cmb_preset, "Pick a preset to load it. New... saves the current settings as a new preset.")
        ttk.Button(pf, text="Save", style="Accent.TButton", command=self.save_preset).grid(row=1, column=0, sticky="we", pady=(6, 0))
        btn_new = ttk.Button(pf, text="New...", command=self.new_preset)
        btn_new.grid(row=1, column=1, sticky="we", pady=(6, 0), padx=(4, 0))
        Tooltip(btn_new, "Save the settings below as a new preset (asks for a name)")
        ttk.Button(pf, text="Revert", command=lambda: self.load_preset(self.loaded_name) if self.loaded_name else None
                   ).grid(row=1, column=2, sticky="we", pady=(6, 0), padx=4)
        ttk.Button(pf, text="Folder", command=lambda: os.startfile(self.store.dir)).grid(row=1, column=3, sticky="we", pady=(6, 0))
        pf.columnconfigure((0, 1, 2, 3), weight=1)
        self.lbl_tracks = ttk.Label(pf, style="Dim.TLabel", wraplength=280)
        self.lbl_tracks.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.btn_assign = ttk.Button(pf, text="Use this preset for the current track", command=self.assign_track)
        self.btn_assign.grid(row=3, column=0, columnspan=4, sticky="we", pady=(4, 0))
        Tooltip(self.btn_assign, "Links the current track to this preset, so it loads automatically next time.")
        self.var_auto = tk.BooleanVar(value=self.settings["auto_load"])
        ttk.Checkbutton(pf, text="Auto-load preset when the track changes", variable=self.var_auto,
                        command=lambda: self.settings.update(auto_load=self.var_auto.get())
                        ).grid(row=4, column=0, columnspan=4, sticky="w", pady=(4, 0))

        tf = ttk.LabelFrame(parent, text="Tires", padding=8)
        tf.pack(fill="x", pady=(8, 0))
        for r, k in enumerate(("wear_r", "wear_l", "max_tire_sets")):
            self._entry_row(tf, r, k)
        self._entry_row(tf, 3, "worn_pct")
        self.lbl_learned = ttk.Label(tf, style="Dim.TLabel", wraplength=280, text="Learned thresholds: collecting...")
        self.lbl_learned.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.btn_learned = ttk.Button(tf, text="Apply learned thresholds", command=self._apply_learned)
        self.btn_learned.grid(row=5, column=0, columnspan=2, sticky="we", pady=(4, 0))
        self.btn_learned.state(["disabled"])
        Tooltip(self.btn_learned, "At the end of every long green run (cut-short caution runs left out) the wear "
                                  "of each side is recorded. With 5+ runs the median is offered "
                                  "here. Apply copies it into the thresholds above; Save to keep it in the preset.")
        self.lbl_model = ttk.Label(tf, style="Dim.TLabel", wraplength=280, text="Tire model: waiting for a session")
        self.lbl_model.grid(row=6, column=0, columnspan=2, sticky="w", pady=(2, 0))
        tf.columnconfigure(1, weight=1)

        ff = ttk.LabelFrame(parent, text="Fuel", padding=8)
        ff.pack(fill="x", pady=(8, 0))
        self.vars["tank_source"] = tk.StringVar(value="manual")
        rb = ttk.Frame(ff, style="Panel.TFrame")
        rb.grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(rb, text="Fuel source:", style="Panel.TLabel").pack(side="left")
        for val, txt in (("manual", "Manual"), ("memory", "Learned (real fuel)")):
            ttk.Radiobutton(rb, text=txt, value=val, variable=self.vars["tank_source"],
                            command=self._on_param_change).pack(side="left", padx=(6, 0))
        self._entry_row(ff, 1, "tank_laps")
        self.lbl_mem_tank = ttk.Label(ff, style="Dim.TLabel", text="Learned: -")
        self.lbl_mem_tank.grid(row=2, column=0, sticky="w")
        Tooltip(self.lbl_mem_tank, "Laps a full tank lasts here, learned from the field's real fuel use on clean "
                                   "green-flag laps and saved per NR2003 mod + track. With 'Learned (real fuel)' "
                                   "AIMOD uses it instead of the manual value. (The old 'Game reports' value only "
                                   "worked while you drove the car.)")
        self.btn_use_learned = ttk.Button(ff, text="Use", width=5, command=self._use_learned_tank)
        self.btn_use_learned.grid(row=2, column=1, sticky="e")
        Tooltip(self.btn_use_learned, "Copy the learned laps per tank into the manual Tank size box.")
        self._entry_row(ff, 3, "fuel_margin")
        self._entry_row(ff, 4, "finish_fuel")
        self.lbl_mileage = ttk.Label(ff, style="Dim.TLabel")
        self.lbl_mileage.grid(row=5, column=0, columnspan=2, sticky="w")
        Tooltip(self.lbl_mileage, "Learned from each car's real fuel level on clean green-flag laps. Each car "
                                  "plans with its own mileage; this is the field's median.")
        ff.columnconfigure(1, weight=1)

        gf = ttk.LabelFrame(parent, text="Green flag stops", padding=8)
        gf.pack(fill="x", pady=(8, 0))

        def check(frame, key, text, tip, row):
            self.vars[key] = tk.BooleanVar(value=True)
            cb = ttk.Checkbutton(frame, text=text, variable=self.vars[key], command=self._on_param_change)
            cb.grid(row=row, column=0, columnspan=2, sticky="w")
            Tooltip(cb, tip)
        check(gf, "stint_strategy", "Varied team pit strategies",
              "Each team picks where in its fuel window to stop: short-pit (as soon as it can make it to the end "
              "/ undercut), split (even stints) or stretch (as late as fuel allows, hoping for a caution). The "
              "choice is weighted by tire falloff, how many cautions the race has had, running position and "
              "rating. Once cars around it start pitting, a team may answer the same lap or the next "
              "(strongly if a car behind pits); some stick to their plan.", 0)
        self._entry_row(gf, 1, "stint_spread")
        self._entry_row(gf, 2, "lead_pack_gap")
        self._entry_row(gf, 3, "dispersion_divisor")
        self._entry_row(gf, 4, "min_stop_gap")
        gf.columnconfigure(1, weight=1)

        cf = ttk.LabelFrame(parent, text="Caution stops", padding=8)
        cf.pack(fill="x", pady=(8, 0))
        check(cf, "lapped_wave", "Lapped cars stay out for the wave around",
              "A lapped car running ahead of the leader on track (between the leader and the pace car) stays out "
              "so it is waved around and gets a lap back, unless it must pit for fuel or damage. The first car "
              "one lap down (lucky dog) gets its lap back anyway. Other lapped cars pit when their tires are worn "
              "(nothing to lose).", 0)
        self._entry_row(cf, 1, "fuel_reach")
        self._entry_row(cf, 2, "threat_positions")
        self._entry_row(cf, 3, "track_position_bias")
        self._entry_row(cf, 4, "urgency_factor")
        cf.columnconfigure(1, weight=1)

        sf = ttk.LabelFrame(parent, text="Pit service", padding=8)
        sf.pack(fill="x", pady=(8, 0))
        self._entry_row(sf, 0, "damage_threshold")
        sf.columnconfigure(1, weight=1)

        gf2 = ttk.LabelFrame(parent, text="Stages", padding=8)
        gf2.pack(fill="x", pady=(8, 0))
        ttk.Label(gf2, text="Stages", style="Panel.TLabel").grid(row=0, column=0, sticky="w", pady=2)
        self.vars["stages"] = tk.StringVar(value="off")
        cmb = ttk.Combobox(gf2, textvariable=self.vars["stages"], values=("off", "2", "3", "4"), width=7,
                           state="readonly")
        cmb.grid(row=0, column=1, sticky="e", pady=2)
        cmb.bind("<<ComboboxSelected>>", lambda e: self._on_param_change())
        Tooltip(cmb, "Stage racing. Each stage end is a sure chance to pit (the stage caution), so cars plan "
                     "their stops around it: wait for the break, flip the stage, or split / stretch a long stage. "
                     "Set the stage ends to match your stage racing companion app (AIMOD does not throw the stage cautions).")
        ttk.Label(gf2, text="Stage ends given as", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=2)
        self.vars["stage_by"] = tk.StringVar(value="laps")
        cmb = ttk.Combobox(gf2, textvariable=self.vars["stage_by"], values=("laps", "% of race"), width=9,
                           state="readonly")
        cmb.grid(row=1, column=1, sticky="e", pady=2)
        cmb.bind("<<ComboboxSelected>>", lambda e: self._on_param_change())
        lbl = ttk.Label(gf2, text="Stage ends", style="Panel.TLabel")
        lbl.grid(row=2, column=0, sticky="w", pady=2)
        self.vars["stage_ends"] = tk.StringVar()
        ent = ttk.Entry(gf2, textvariable=self.vars["stage_ends"], width=14, justify="right")
        ent.grid(row=2, column=1, sticky="e", pady=2, padx=(8, 0))
        self.entries["stage_ends"] = ent
        self.vars["stage_ends"].trace_add("write", lambda *_: self._on_param_change())
        tip = ("Where each stage ends, all but the last (the last ends at the finish), separated by commas. "
               "Laps: race lap the stage ends on, e.g. 80, 160. % of race: e.g. 25, 50 on a 200-lap race = "
               "laps 50 and 100 (rounded).")
        Tooltip(lbl, tip)
        Tooltip(ent, tip)
        self._entry_row(gf2, 3, "stage_early")
        self._entry_row(gf2, 4, "flip_pct")
        self.lbl_stages = ttk.Label(gf2, style="Dim.TLabel", wraplength=280)
        self.lbl_stages.grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))
        gf2.columnconfigure(1, weight=1)

        xf = ttk.LabelFrame(parent, text="Superspeedway settings", padding=8)
        xf.pack(fill="x", pady=(8, 0))
        ttk.Label(xf, text="Drafting track", style="Panel.TLabel").grid(row=0, column=0, sticky="w", pady=2)
        self.vars["drafting"] = tk.StringVar(value="auto")
        cmb = ttk.Combobox(xf, textvariable=self.vars["drafting"], values=("auto", "yes", "no"), width=7,
                           state="readonly")
        cmb.grid(row=0, column=1, sticky="e", pady=2)
        cmb.bind("<<ComboboxSelected>>", lambda e: self._on_param_change())
        Tooltip(cmb, "auto = Talladega and Daytona. At drafting tracks fuel matters more than tires, teams on "
                     "the same plan pit on the same lap, nearly everyone pits under caution, and same-make "
                     "drafting partners work together much more.")
        self.lbl_drafting = ttk.Label(xf, style="Dim.TLabel")
        self.lbl_drafting.grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Label(xf, text="Track type", style="Panel.TLabel").grid(row=4, column=0, sticky="w", pady=2)
        self.vars["track_type"] = tk.StringVar(value="auto")
        cmb = ttk.Combobox(xf, textvariable=self.vars["track_type"],
                           values=("auto", "superspeedway", "intermediate", "short", "road"), width=12,
                           state="readonly")
        cmb.grid(row=4, column=1, sticky="e", pady=2)
        cmb.bind("<<ComboboxSelected>>", lambda e: self._on_param_change())
        Tooltip(cmb, "Each track type has its own pit strategy. Superspeedway: fuel and aero, cars pit in "
                     "blocks. Intermediate: tire wear decides (high wear = 4 tires and undercuts, low wear = "
                     "stretch and short service). Short track: a green stop costs laps, so only fuel-forced "
                     "green stops; cautions decide. Road course: the final stop is set by the last full tank to "
                     "the finish, whatever the stage. auto = from the drafting setting, a road-course name list "
                     "and the track length.")
        self.lbl_track_type = ttk.Label(xf, style="Dim.TLabel")
        self.lbl_track_type.grid(row=5, column=0, columnspan=2, sticky="w")
        self._entry_row(xf, 2, "ss_stint_spread")
        self._entry_row(xf, 3, "ss_topoff_pct")
        xf.columnconfigure(1, weight=1)

        of = ttk.LabelFrame(parent, text="Manufacturers & teams", padding=8)
        of.pack(fill="x", pady=(8, 0))
        self.vars["oem_strategy"] = tk.BooleanVar(value=True)
        cb = ttk.Checkbutton(of, text="Same-make cars work together", variable=self.vars["oem_strategy"],
                             command=self._on_param_change)
        cb.grid(row=0, column=0, columnspan=2, sticky="w")
        Tooltip(cb, "Same-make cars running close together line up their green-flag stops and tend to follow "
                    "each other under caution. The closer they run, the likelier (see Strategy rules).")
        b = ttk.Button(of, text="Strategy rules (drafting / other tracks)...", command=self._edit_rules)
        b.grid(row=1, column=0, columnspan=2, sticky="we", pady=(4, 0))
        Tooltip(b, "Who works together first (teammates or same make), whether teammates split their plans "
                   "or pit together, the gaps and how often they follow each other - separately for drafting "
                   "tracks and other tracks. Saved per NR2003 mod.")
        b = ttk.Button(of, text="Name manufacturers...", command=self._name_makes)
        b.grid(row=2, column=0, columnspan=2, sticky="we", pady=(4, 0))
        self.vars["team_strategy"] = tk.BooleanVar(value=True)
        cb = ttk.Checkbutton(of, text="Teams work together", variable=self.vars["team_strategy"],
                             command=self._on_param_change)
        cb.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        Tooltip(cb, "Teammates split their green-flag plans or pit together, and follow their best-placed "
                    "teammate's caution call (see Strategy rules).")
        self.vars["follow_player"] = tk.BooleanVar(value=True)
        cb = ttk.Checkbutton(of, text="Teammates follow your stops", variable=self.vars["follow_player"],
                             command=self._on_param_change)
        cb.grid(row=4, column=0, columnspan=2, sticky="w")
        Tooltip(cb, "When your car is on a team: under caution, AI teammates running behind you wait to see "
                    "whether you pit (then follow your call like any lead teammate's), and under green a "
                    "teammate whose stop is due within a couple of laps can pit with you (if the team rules "
                    "say pit together). Off = they ignore your car's stops.")
        b = ttk.Button(of, text="Teams...", command=self._edit_teams)
        b.grid(row=5, column=0, columnspan=2, sticky="we", pady=(4, 0))
        Tooltip(b, "NR2003 stores only a make number per car; the names depend on the mod. Set them once per "
                   "NR2003 mod.")
        of.columnconfigure(1, weight=1)

        bf = ttk.LabelFrame(parent, text="Driver behavior (beta)", padding=8)
        bf.pack(fill="x", pady=(8, 0))
        check(bf, "behavior_on", "AI drivers save fuel, save / cool tires and push",
              "Each AI car gets a plan at the start of every run (the start, and after each stop): tire "
              "managers (strategy rating, temperament, long runs) save tires through the middle of the run and "
              "push the last laps; the rest run it out; a car that makes the finish or the stage break only by "
              "saving fuel saves all run; at drafting tracks the pack rides and saves fuel for a shorter stop "
              "and pushes for the stage end, cars following their team's (else make's) lead car in the pack. "
              "Changes the car's own grip / power and its real wear and fuel use (memory only; your car is "
              "never touched).", 0)
        check(bf, "behavior_reactions", "Change on the fly in real situations",
              "Top 3 within a second push (battling for the lead); a lead-lap car pushes to hold off the "
              "leader; a car whose tires go away early saves the rest of the run; a car stuck behind a slower "
              "one for 5 laps drops back to cool its tires, then makes a run (once a run). Off = plans only.", 1)
        check(bf, "behavior_teams", "Teams and makes work together",
              "Teammates never attack or defend against each other. At drafting tracks the cars of one team "
              "(else make) in the same pack save and push with their lead car.", 2)
        self._entry_row(bf, 3, "behavior_strength")
        self._entry_row(bf, 4, "behavior_late_pct")
        bf.columnconfigure(1, weight=1)

        xf2 = ttk.LabelFrame(parent, text="Other apps (optional)", padding=8)
        xf2.pack(fill="x", pady=(8, 0))
        self.var_sf = tk.BooleanVar(value=self.settings["use_companion"])
        cb = ttk.Checkbutton(xf2, text="Use an installed companion app's data", variable=self.var_sf,
                             command=self._companion_toggle)
        cb.grid(row=0, column=0, sticky="w")
        Tooltip(cb, "Not needed: AIMOD reads track, laps, positions and black flags from NR2003 itself and ships "
                    "its own artwork. When on, AIMOD also uses a compatible companion app's active series, artwork "
                    "folder and live feed as a backup, and can install the pit board as one of its widgets.")
        ttk.Label(xf2, style="Dim.TLabel", wraplength=280, text=(
            "Wave-around tags come from RaceControl's or bc_wave's log if one is there.")).grid(row=1, column=0, sticky="w")

        af = ttk.LabelFrame(parent, text="Game patches", padding=8)
        af.pack(fill="x", pady=(8, 0))
        self.patch_vars = {}
        for r, (key, p) in enumerate(mm.PATCHES.items()):
            v = tk.BooleanVar(value=key in self.settings["patches"])
            self.patch_vars[key] = v
            text = {"no_game_plan_stops": "No game-planned stops",
                    "no_game_condition_stops": "No 'car condition' stops",
                    "caution_repairs": "Repair damage on caution stops"}.get(key, f"0x{p['addr']:X}")
            c = ttk.Checkbutton(af, text=text, variable=v, command=self._on_patch_change)
            c.grid(row=r, column=0, sticky="w")
            Tooltip(c, p["label"])
        self.var_restore = tk.BooleanVar(value=self.settings["restore_on_exit"])
        c = ttk.Checkbutton(af, text="Restore game code on exit", variable=self.var_restore,
                            command=lambda: self.settings.update(restore_on_exit=self.var_restore.get()))
        c.grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.var_gate = tk.BooleanVar(value=bool(self.settings.get("pit_gate", True)))
        c = ttk.Checkbutton(af, text="Pit gate: only AIMOD sends cars to pit road", variable=self.var_gate,
                            command=lambda: self.settings.update(pit_gate=self.var_gate.get()))
        c.grid(row=len(mm.PATCHES), column=0, columnspan=2, sticky="w")
        Tooltip(c, "In a race, an AI car turns for pit road only when AIMOD called it in, it has a penalty, "
                   "repair or tow stop to serve, or it is out of fuel. NR2003's own reasons (its caution "
                   "logic, its stop plan, its 'car condition') no longer take cars in behind AIMOD's back. "
                   "Memory only; NR2003's stock pit logic comes back within a minute of game time if AIMOD "
                   "closes. Off in practice and qualifying.")
        self.var_crew = tk.BooleanVar(value=False)
        self.chk_crew = ttk.Checkbutton(af, text="Modern pit crew (this mod)", variable=self.var_crew,
                                        command=self._on_crew_change)
        self.chk_crew.grid(row=len(mm.PATCHES) + 1, column=0, columnspan=2, sticky="w")
        Tooltip(self.chk_crew, "One lug nut per wheel and quicker jack and tire changes: about 10 s for four "
                               "tires instead of about 14 s. Every car, including yours. Saved per NR2003 mod "
                               "(on by default for FCRD NCS22). Memory only: the game files are never changed, "
                               "and the stock crew comes back when AIMOD closes. AIMOD learns pit stop times "
                               "separately for the modern crew.")
        gf = ttk.Frame(af)
        gf.grid(row=len(mm.PATCHES) + 2, column=0, columnspan=2, sticky="w")
        self.var_gear = tk.BooleanVar(value=False)
        self.chk_gear = ttk.Checkbutton(gf, text="Gearbox (this mod):", variable=self.var_gear,
                                        command=self._on_gear_change)
        self.chk_gear.pack(side="left")
        self.var_gears = tk.StringVar(value="5")
        self.cmb_gears = ttk.Combobox(gf, textvariable=self.var_gears, values=["5", "6"], width=3, state="readonly")
        self.cmb_gears.pack(side="left", padx=(2, 2))
        self.cmb_gears.bind("<<ComboboxSelected>>", lambda e: self._on_gear_change(count_changed=True))
        ttk.Label(gf, text="speed").pack(side="left")
        self.btn_gear_restore = ttk.Button(gf, text="Restore", width=8, command=self._gear_restore)
        self.btn_gear_restore.pack(side="left", padx=(6, 0))
        Tooltip(self.chk_gear, "Modern 5-speed (or 6-speed) gearbox for every car, yours included: each car's "
                               "gears are spread in equal steps between its own 1st and top gear (final drive "
                               "and reverse unchanged). Saved per NR2003 mod; offered for FCRD NCS22 only. "
                               "Memory only - the garage still shows 4 gears, that's expected. Re-applied when "
                               "a session loads and after the garage. Turn it on only once you are IN THE CAR "
                               "(on track), not from the menus or the garage.")
        Tooltip(self.btn_gear_restore, "Put the stock gearbox back (best in the garage or between sessions, "
                                       "not while cars are using 5th gear). Also turns the option off.")
        self.lbl_patch = ttk.Label(af, style="Dim.TLabel", wraplength=280)
        self.lbl_patch.grid(row=len(mm.PATCHES) + 3, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def _build_tabs(self, parent):
        nb = ttk.Notebook(parent)
        nb.pack(fill="both", expand=True)

        mon = ttk.Frame(nb)
        nb.add(mon, text="Monitor")
        top = ttk.Frame(mon)
        top.pack(fill="x", pady=(0, 4))
        self.var_hide_out = tk.BooleanVar(value=self.settings.get("hide_out", False))
        ttk.Checkbutton(top, text="Hide cars that are out", variable=self.var_hide_out,
                        command=lambda: self.settings.update(hide_out=self.var_hide_out.get())).pack(side="left")
        self.var_colors = tk.BooleanVar(value=self.settings.get("color_levels", True))
        cb = ttk.Checkbutton(top, text="Colour fuel & wear", variable=self.var_colors,
                             command=lambda: (self.settings.update(color_levels=self.var_colors.get()),
                                              self._render_monitor()))
        cb.pack(side="left", padx=(12, 0))
        Tooltip(cb, "Green = good, yellow = getting old, red = old / low. Wear: L-/R-Prob under 60% / under 90% "
                    "/ 90%+. Fuel: over half a tank / over a fifth / less.")
        for kind, txt in (("pitting", "pitting"), ("stayout", "staying out"), ("finished", "stop done"),
                          ("damage", "damaged"), ("out", "out"), ("player", "your car")):
            tk.Label(top, text="■ " + txt, fg=KIND_COLORS[kind], bg=BG, font=("Segoe UI", 9)).pack(side="right", padx=6)
        ttk.Button(top, text="Reset columns", command=self._reset_columns).pack(side="left", padx=(12, 0))
        mb = ttk.Menubutton(top, text="Columns \u25be")
        mb.pack(side="left", padx=(6, 0))
        menu = tk.Menu(mb, tearoff=False, bg=FIELD, fg=FG, activebackground="#1f4f3a", selectcolor=ACCENT)
        hidden = set(self.settings.get("hidden_cols", []))
        self.col_vars = {}
        for c in MONITOR_COLUMNS:
            v = tk.BooleanVar(value=c.id not in hidden)
            self.col_vars[c.id] = v
            menu.add_checkbutton(label=c.head, variable=v, command=self._columns_changed)
        mb["menu"] = menu
        Tooltip(mb, "Choose which columns the monitor shows")
        self.monitor = ColorTable(mon, [dataclasses.replace(c) for c in MONITOR_COLUMNS],
                                  sort_col=self.settings.get("sort_col", "pos"), hover=self._monitor_hover)
        self.monitor.set_widths(self.settings.get("monitor_widths", {}))
        self._columns_changed(save=False)
        self.monitor.on_resize = lambda w: self.settings.__setitem__("monitor_widths", w)
        self.monitor.sort_rev = bool(self.settings.get("sort_rev", False))
        self.monitor.sort_by(self.monitor.sort_col, self.monitor.sort_rev)
        self.monitor.pack(fill="both", expand=True)

        pl = ttk.Frame(nb)
        nb.add(pl, text="Pit log")
        bar = ttk.Frame(pl)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text="View:").pack(side="left")
        self.var_pit_view = tk.StringVar(value=self.settings.get("pit_view", "driver"))
        for val, txt in (("driver", "By driver"), ("caution", "By caution"), ("stops", "Every stop")):
            ttk.Radiobutton(bar, text=txt, value=val, variable=self.var_pit_view, style="Bar.TRadiobutton",
                            command=self._pit_view_changed).pack(side="left", padx=(6, 0))
        ttk.Label(bar, text="   Sort:").pack(side="left")
        self.var_pit_sort = tk.StringVar(value=self.settings.get("pit_sort", "Running order"))
        self.cmb_pit_sort = ttk.Combobox(bar, textvariable=self.var_pit_sort, state="readonly", width=16,
                                         values=("Running order", "Car #", "Driver", "Most stops",
                                                 "Fastest stop", "Places gained"))
        self.cmb_pit_sort.pack(side="left", padx=(6, 0))
        self.cmb_pit_sort.bind("<<ComboboxSelected>>", lambda e: self._pit_view_changed())
        ttk.Button(bar, text="Open logs folder", command=self._open_logs).pack(side="right")
        self.pit_table = ColorTable(pl, [], hover=self._pit_hover, sortable=True)
        self.pit_table.on_resize = lambda w: self.settings.setdefault("pit_widths", {}).update(w)
        self.pit_table.pack(fill="both", expand=True)
        self.pit_table.on_sort = lambda c, r: None

        self._build_pitboard_tab(nb)
        self.radio_tab = RadioTab(nb, self.radio, self._save_settings, {"field": FIELD, "fg": FG}, Tooltip)

        lg = ttk.Frame(nb)
        nb.add(lg, text="Log")
        self.txt_log = tk.Text(lg, bg=FIELD, fg=FG, font=("Consolas", 9), relief="flat", state="disabled")
        self.txt_log.pack(fill="both", expand=True)

    # -------------------------------------------------------------- presets
    def _set_fields(self, p: Preset):
        self._loading = True
        for key in PARAM_FIELDS:
            v = getattr(p, key)
            self.vars[key].set(f"{v:g}" if isinstance(v, float) else str(v))
        for key in BOOL_FIELDS:
            self.vars[key].set(getattr(p, key))
        self.vars["tank_source"].set(p.tank_source)
        self.vars["drafting"].set(p.drafting)
        self.vars["track_type"].set(p.track_type)
        self.vars["stages"].set(str(p.stages) if p.stages >= 2 else "off")
        self.vars["stage_by"].set("% of race" if p.stage_by == "percent" else "laps")
        self.vars["stage_ends"].set(p.stage_ends)
        self._loading = False
        self.params = p
        self._on_param_change()

    def _read_fields(self) -> Preset:
        """Current UI values; invalid entries keep the last valid value and turn red."""
        p = Preset(**{k: getattr(self.params, k) for k in Preset.__dataclass_fields__})
        p.tracks = list(self.params.tracks)
        for key, (_, typ, _) in PARAM_FIELDS.items():
            ent = self.entries[key]
            try:
                val = typ(float(self.vars[key].get().strip())) if typ is int else typ(self.vars[key].get().strip())
                if val < 0:
                    raise ValueError
                setattr(p, key, val)
                ent.configure(style="TEntry")
            except ValueError:
                ent.configure(style="Bad.TEntry")
        for key in BOOL_FIELDS:
            setattr(p, key, bool(self.vars[key].get()))
        p.tank_source = self.vars["tank_source"].get()
        p.drafting = self.vars["drafting"].get()
        p.track_type = self.vars["track_type"].get()
        st = self.vars["stages"].get()
        p.stages = int(st) if st.isdigit() else 0
        p.stage_by = "percent" if self.vars["stage_by"].get().startswith("%") else "laps"
        p.stage_ends = self.vars["stage_ends"].get().strip()
        vals = parse_stage_ends(p.stage_ends)
        ok = p.stages < 2 or (vals is not None and len(vals) == p.stages - 1
                              and (p.stage_by != "percent" or all(v < 100 for v in vals)))
        self.entries["stage_ends"].configure(style="TEntry" if ok else "Bad.TEntry")
        return p

    def _on_param_change(self):
        if getattr(self, "_loading", False):
            return
        self.params = self._read_fields()
        self._update_preset_label()

    def _mark_clean(self, name):
        self.loaded_name = name
        self.loaded_dict = self.params.to_dict() if name else None
        self._update_preset_label()

    def _update_preset_label(self):
        if not hasattr(self, "lbl_preset"):
            return
        dirty = self.loaded_dict is not None and self.params.to_dict() != self.loaded_dict
        name = self.loaded_name or "(no preset)"
        self.lbl_preset.configure(text=f"Preset: {name}{' *unsaved' if dirty else ''}",
                                  foreground="#ffd54f" if dirty else FG)
        tracks = ", ".join(self.params.tracks) if self.params.tracks else "none"
        self.lbl_tracks.configure(text=f"Auto-loads for: {tracks}")

    def load_preset(self, name: str, quiet: bool = False):
        if not name:
            return
        try:
            p = self.store.load(name)
        except Exception as e:
            messagebox.showerror("Load preset", f"Could not load '{name}':\n{e}", parent=self.root)
            return
        self._set_fields(p)
        self.cmb_preset.set(name)
        self._mark_clean(name)
        self.settings["last_preset"] = name
        log.info("Loaded preset '%s'", name)
        if not quiet:
            self._flash(self.lbl_preset)

    def new_preset(self):
        base = self.loaded_name or "My preset"
        name = simpledialog.askstring("New preset", "Name for the new preset\n(saves the current settings):",
                                      initialvalue=f"{base} copy" if self.loaded_name else base, parent=self.root)
        if name and name.strip():
            self.save_preset(name.strip())

    def save_preset(self, name: str | None = None):
        name = (name or self.cmb_preset.get()).strip()
        if not name:
            messagebox.showwarning("Save preset", "Type a name for the preset first.", parent=self.root)
            return
        if any(c in name for c in '\\/:*?"<>|'):
            messagebox.showwarning("Save preset", 'A preset name cannot contain \\ / : * ? " < > |', parent=self.root)
            return
        p = self._read_fields()
        if name != self.loaded_name and name in self.store.names():
            if not messagebox.askyesno("Save preset", f"Overwrite existing preset '{name}'?", parent=self.root):
                return
            p.tracks = self.store.load(name).tracks
        elif name != self.loaded_name:
            p.tracks = []
        try:
            self.store.save(name, p)
        except Exception as e:
            messagebox.showerror("Save preset", f"Could not save '{name}':\n{e}", parent=self.root)
            return
        self.params = p
        self.cmb_preset.configure(values=self.store.names())
        self.cmb_preset.set(name)
        self._mark_clean(name)
        self.settings["last_preset"] = name
        log.info("Saved preset '%s'", name)
        self._flash(self.lbl_preset)

    def assign_track(self):
        if not self.track:
            messagebox.showinfo("Assign track", "No track detected yet. Load a session in NR2003.", parent=self.root)
            return
        if not self.loaded_name:
            messagebox.showinfo("Assign track", "Save the preset first.", parent=self.root)
            return
        self.store.assign_track(self.loaded_name, self.track)
        self.params.tracks = self.store.load(self.loaded_name).tracks
        self.loaded_dict = self.params.to_dict() if self.loaded_dict else None
        self._update_preset_label()
        log.info("Preset '%s' will auto-load for track '%s'", self.loaded_name, self.track)

    def _flash(self, widget):
        widget.configure(foreground=ACCENT)
        self.root.after(900, self._update_preset_label)

    def _on_patch_change(self):
        self.settings["patches"] = [k for k, v in self.patch_vars.items() if v.get()]
        if self.game.pm is not None:
            self.game.apply_patches(self.settings["patches"])

    def _crew_modern(self) -> bool:
        mod = self.mod or ""
        return bool((self.settings.get("pit_crew_by_mod") or {}).get(mod, default_modern(mod)))

    def _on_crew_change(self):
        self.settings.setdefault("pit_crew_by_mod", {})[self.mod or ""] = bool(self.var_crew.get())
        self._crew_applied = None                 # re-apply on the next tick
        self._use_tire_model()

    def _setup_crew_chief(self):
        """The track's AI setup (what every AI car starts from) and each car's pit crew rating, for the crew
        chief's chassis adjustments."""
        from . import carfiles
        base_dir = (self.game.nr2003_dir() if self.game.pm is not None else "") or self.settings.get("nr2003_dir", "")
        if base_dir:
            self.settings["nr2003_dir"] = base_dir
            self.radio.voice.nr2003_dir = base_dir           # the driver's radio uses the game's engine sounds
        setup = ai_setup(base_dir, self.track) if self.track else None
        self.engine.crew_chief.reset(setup)
        ratings = {}
        profiles = {}
        if self.game.pm is not None and self.mod:
            from .crewchief import CrewChiefProfile
            for path in self.game.session_car_files(self.mod):
                info = carfiles.car_info(path)
                if not info:
                    continue
                num = info["number"]
                if info.get("pitcrew_speed") is not None:
                    ratings[num] = (info["pitcrew_speed"] + (info.get("pitcrew_consistency")
                                                             or info["pitcrew_speed"])) / 2
                profiles[num] = CrewChiefProfile.from_car_info(info, driver_name=info.get("name", ""))
        self.engine.crew_rating = ratings
        self.engine.cc_profiles = profiles
        if setup:
            log.info("AI setup for %s: wedge %+d lb, track bar %.2f/%.2f in, tape %d%%, RR %.1f psi "
                     "(%d crew ratings)", self.track, setup["wedge"], *setup["track_bar"], setup["tape"],
                     setup["pressure"][3], len(ratings))

    def _use_tire_model(self):
        """Learned stop times are kept apart for the modern crew (they are ~4 s shorter)."""
        if self.track and self.mod:       # (not before the mod is known: no blank-mod entries)
            self._setup_crew_chief()
            self.tire_model.use((self.mod or "") + (" [modern pit crew]" if self._crew_modern() else ""),
                                self.track, seed_mod=self.mod, seed_tire_scale=9.9 / 14.4)

    def _gear_mod(self) -> str:
        """The mod the gearbox setting is for: the loaded session's, else the last one seen (at the main menu
        no mod is loaded yet, and the count is best set before the session loads)."""
        return self.mod or self.settings.get("last_mod", "")

    def _gear_setting(self) -> dict:
        mod = self._gear_mod()
        cfg = (self.settings.get("gearbox_by_mod") or {}).get(mod)
        return dict(cfg) if cfg else {"on": gearbox_default_on(mod), "gears": 5}

    def _on_gear_change(self, count_changed: bool = False):
        cfg = {"on": bool(self.var_gear.get()), "gears": int(self.var_gears.get() or 5)}
        self.settings.setdefault("gearbox_by_mod", {})[self._gear_mod()] = cfg
        self._gear_want = None                    # apply on the next tick

    def _gear_restore(self):
        self.var_gear.set(False)
        self._on_gear_change()

    def _sync_gearbox(self):
        pm = self.game.pm
        mod = self._gear_mod()
        if self.mod and self.settings.get("last_mod") != self.mod:
            self.settings["last_mod"] = self.mod
        ok = gearbox_offered(mod)
        for w in (self.chk_gear, self.cmb_gears, self.btn_gear_restore):
            w.state(["!disabled"] if ok else ["disabled"])
        if pm is None:
            return
        cfg = self._gear_setting()
        if self.var_gear.get() != cfg["on"]:
            self.var_gear.set(cfg["on"])
        if self.var_gears.get() != str(cfg["gears"]):
            self.var_gears.set(str(cfg["gears"]))
        gears = cfg["gears"] if (ok and cfg["on"]) else 4
        self._sync_setup_gearing(gears)
        want = (pm.process_id, gears)
        if want == self._gear_want:
            return
        if gears == 4 and self._gear_want is None and self.gearbox.applied is None:
            # never changed by AIMOD in this NR2003: leave the game's own count alone
            self._gear_want = want
            return
        self.gearbox.apply(pm, gears)
        self._gear_want = want

    def _sync_setup_gearing(self, gears: int):
        """Re-gearing the tracks' fast setups is OFF: NR2003 refuses an edited fast.cup.sim ("Could not load the
        track", 2026-10-18 - most likely a check on the contents). Any originals an earlier 2.0.1 build re-geared
        are put back. setupfiles.convert_all is kept for when that check is understood."""
        from . import setupfiles
        base = self.settings.get("nr2003_dir") or (self.game.nr2003_dir() if self.game.pm is not None else "")
        if not base or getattr(self, "_setup_gears", None) == gears:
            return
        try:
            if setupfiles.converted(base):
                n = setupfiles.restore_all(base)
                log.info("Put back %d original track fast setups (an earlier build re-geared them)", n)
            self._setup_gears = gears
        except Exception as e:
            log.warning("Setup gearing failed: %s", e)
            self._setup_gears = gears

    def _sync_pit_crew(self, snap):
        if self.game.pm is None or not self.mod:
            return
        modern = self._crew_modern()
        if self.var_crew.get() != modern:
            self.var_crew.set(modern)
        want = (self.game.pm.process_id, modern)
        now = time.monotonic()
        if self._crew_applied == want and now - getattr(self, "_crew_check", 0.0) > 10.0:
            self._crew_check = now                # still as we set it? (another AIMOD closing restores stock)
            try:
                if self.pitcrew.is_modern(self.game.pm) != modern:
                    log.info("Pit crew was changed outside this AIMOD (another copy closed?) - setting it again")
                    self._crew_applied = None
            except Exception:
                pass
        if self._crew_applied == want:
            return
        on_road = any(c.valid and c.on_pit_road for c in snap.cars)
        if self.pitcrew.apply(self.game.pm, modern, on_road):
            self._crew_applied = want
            log.info("%s (%s)", self.pitcrew.status, self.mod)

    def _open_logs(self):
        os.makedirs(self.pitlog.dir, exist_ok=True)
        os.startfile(self.pitlog.dir)

    # ----------------------------------------------------------------- loop
    def tick(self):
        try:
            self._tick()
        except Exception as e:
            # log each different error once a minute, not every tick (it would flood aimod.log)
            key, now = f"{type(e).__name__}: {e}", time.monotonic()
            last = self._tick_errors.get(key)
            if last is None or now - last > 60:
                self._tick_errors[key] = now
                log.exception("Tick failed (repeats are logged once a minute)")
        self._drain_log()
        self.root.after(TICK_MS, self.tick)

    def _tick(self):
        now = time.monotonic()
        if now - self._last_track_check >= TRACK_CHECK_S:
            self._last_track_check = now
            self._check_track()

        if not self.game.attached:
            self.lbl_conn.configure(text="● NR2003: waiting for game", foreground=DIM)
            self.lbl_flag.configure(text="")
            self.lbl_laps.configure(text="")
            self._update_fuel_label(None)
            if now - self._last_attach_try >= ATTACH_RETRY_S:
                self._last_attach_try = now
                if self.game.attach():
                    self.engine.reset()
                    self.behavior.reset()
                    self.behavior_writer.reset()
                    self.game.apply_patches(self.settings["patches"])
            self.lbl_patch.configure(text=self.game.patch_status)
            return

        ok = self.game.write_enabled
        self.lbl_conn.configure(text="● NR2003: connected" if ok else "● NR2003: READ-ONLY",
                                foreground=ACCENT if ok else BAD)
        extra = [x for x in (self.pitcrew.status, self.gearbox.status, self.pitgate.status) if x]
        self.lbl_patch.configure(text="\n".join([self.game.patch_status] + extra), foreground=DIM if ok else BAD)

        snap = self.game.snapshot()
        self._last_snap = snap
        self._sync_pit_crew(snap)
        self._sync_gearbox()
        lap_src = self._apply_lap_source(snap, now)
        flagged = self._black_flags()
        if snap.wave_around is None:
            # no RaceControl in this race: only Better Cautions' wave around (bc_wave log written lately) waves cars
            bc = self.settings.get("bc_wave_log") or ""
            try:
                snap.wave_around = bool(bc) and time.time() - os.path.getmtime(bc) < 900
            except OSError:
                snap.wave_around = False
        for lap, nums in self.wavelog.poll():
            log.info("WAVE AROUND (lap %d): %s", lap, " ".join(f"#{n}" for n in nums))
        for lap, n in self.wavelog.new_free_pass:
            log.info("FREE PASS (lap %d): #%s gets its lap back", lap, n)
        self.wavelog.new_free_pass.clear()
        team_of = self._team_of()
        pending = self.game.rules_pending() if snap.session == mm.SESSION_RACE else set()
        for c in snap.cars:
            c.team = team_of.get(c.number, -1)
            c.black_flag = c.idx in flagged
            c.rule_pending = c.idx in pending
            c.waved = c.valid and self.wavelog.is_waved(c.number, int(snap.laps_done))
            c.free_pass = c.valid and self.wavelog.is_free_pass(c.number, int(snap.laps_done))
            c.going_around = c.valid and self.wavelog.is_going_around(c.number, int(snap.laps_done))
        for idx in flagged - self._flagged:
            c = next((c for c in snap.cars if c.idx == idx), None)
            if c is not None and c.valid:
                log.info("BLACK FLAG: #%s %s", c.number, c.name)
        self._flagged = flagged
        snap.drafting = self._is_drafting()
        why = self._new_session_reason(snap, lap_src)
        if why:
            self.clear_session(why)
        self._last_session, self._last_laps, self._last_lap_src = snap.session, snap.laps_done, lap_src

        sp = self._strategy_params(snap.tank_laps)
        self._last_sp = sp
        if self.beta and self.settings.get("crew_beta"):
            self.desk.apply(self.engine, snap)          # cars people run from their phones, and their calls
        else:
            self.engine.human_owner, self.engine.human_calls = {}, {}
            self.engine.human_plans, self.engine.human_adjusts = {}, {}
        self.engine.condition_off = "no_game_condition_stops" in self.game.applied_patches
        self.engine.gate_on = self._gate_armed
        res = self.engine.tick(snap, sp)
        if self.beta and self.settings.get("crew_beta"):
            self.desk.sync(self.engine, snap)
        try:
            # (before the writes: a car AIMOD calls in this tick is on the gate's allow list before its pit request
            # reaches the game)
            self._gate_tick(snap, res)
        except Exception:                        # the gate must never stop the strategy tick
            if now - getattr(self, "_gate_error", 0.0) > 60:
                self._gate_error = now
                log.exception("Pit gate failed (repeats are logged once a minute)")
        self.game.apply_writes(res.writes)
        try:
            self._behavior_tick(snap, res, sp)
        except Exception:                        # behavior must never stop the strategy tick
            if now - getattr(self, "_behavior_error", 0.0) > 60:
                self._behavior_error = now
                log.exception("Driver behavior failed (repeats are logged once a minute)")
        self._keep_out = frozenset(res.keep_out) if self.settings.get("pit_guard", True) else frozenset()
        self._start_pit_guard()

        green = snap.flag == mm.FLAG_GREEN
        self.lbl_flag.configure(text="GREEN" if green else f"CAUTION ({snap.flag})",
                                foreground=ACCENT if green else "#ffd54f")
        sess = "RACE" if snap.session == mm.SESSION_RACE else f"session {snap.session}"
        if snap.laps_done > 0 and snap.laps_total and snap.laps_done > snap.laps_total:
            laps_txt = f"Lap {int(snap.laps_done)}/{snap.laps_total} OVERTIME"
        elif snap.laps_done > 0:
            laps_txt = f"Lap {int(snap.laps_done)}/{snap.laps_total} ({res.laps_to_go:.0f} to go)"
        else:
            laps_txt = f"Lap ?/{snap.laps_total} - waiting for the first lap"
        nxt = self.engine.next_stage_end() if snap.session == mm.SESSION_RACE else None
        if nxt is not None:
            laps_txt += f"  |  stage {self.engine.stage_no(nxt)} ends lap {nxt}"
            if self.engine.stage_source != "preset":
                laps_txt += f" ({self.engine.stage_source})"
        elif self.engine.stage_break is not None:
            laps_txt += f"  |  stage {self.engine.stage_no(self.engine.stage_break)} break"
        self.lbl_laps.configure(text=f"{sess}  |  {laps_txt}  [{lap_src}]",
                                foreground=FG if snap.laps_done > 0 else BAD)
        self._update_fuel_label(snap.tank_laps, sp.tank_laps)

        for ev in res.events:
            self._apply_adjust_effect(ev)
            self.pitlog.start(ev, self.track)
            self.pit_events.append(ev)
            self._pit_dirty = True
            log.info("#%s %s pits lap %d: %s (%s)%s, P%s", ev.number, ev.name, ev.lap, ev.code, ev.reason,
                     f" [{ev.note}]" if ev.note else "", ev.pos_in or "?")
        for ev in res.exits:
            if not ev.pos_pending:               # caution stops are written once their position is known
                self.pitlog.finish(ev)
            self._pit_dirty = True
        for ev in res.resolved:
            self.pitlog.finish(ev)
            self._pit_dirty = True
        for ev in res.cancels:                   # asked to pit but never reached pit road
            self.pitlog.pending.pop(id(ev), None)
            if ev in self.pit_events:
                self.pit_events.remove(ev)
            self._pit_dirty = True
            log.info("#%s %s: pit request withdrawn before pit road", ev.number, ev.name)
        self.pitlog.retry()                      # rows held back while the CSV was locked (open in Excel)
        self._last_rows = res.rows
        try:
            tv = tv_slot(self.game.pm) if self.radio.scanner and self.radio.cfg.get("follow_tv") else None
            self.radio.update(snap, res, self.engine, sp, tv)
        except Exception:                        # the radio must never stop the strategy tick
            if now - self._radio_error > 60:
                self._radio_error = now
                log.exception("Radio update failed (repeats are logged once a minute)")
        if self.beta and self.settings.get("crew_beta"):
            try:                                 # read-only: built here on the tick thread, served as bytes
                if now - self._crew_at >= 1.0:
                    self._crew_at = now
                    self.board_server.publish_named("crew", self.crew_view.build(snap, res, self.engine, self.radio,
                                                                                 self.desk))
            except Exception:
                if now - self._crew_error > 60:
                    self._crew_error = now
                    log.exception("Crew view failed (repeats are logged once a minute)")
        try:
            self._update_pitboard(snap, res)
        except Exception:                        # a broken overlay window must not stop the monitor / pit log
            if not self._pb_error:
                log.exception("Pit board update failed")
            self._pb_error = True
        else:
            self._pb_error = False
        if now - self._last_monitor >= MONITOR_REFRESH_S:
            self._last_monitor = now
            self._render_monitor()
            self._update_learned()
            self._update_stage_label()
            self._save_learned_tank()
            self.lbl_model.configure(text=self.tire_model.summary())
            if now - self._model_saved > 300:
                self._model_saved = now
                self.tire_model.save()
            if self._pit_dirty:
                self._render_pitlog()

    def _behavior_tick(self, snap, res, sp):
        """Driver behavior after the strategy tick: pick each AI car's mode, put its effect into the game."""
        p = self.params
        cfg = self.behavior.cfg
        cfg.enabled, cfg.strength, cfg.late_push_pct = p.behavior_on, p.behavior_strength, p.behavior_late_pct
        cfg.reactions, cfg.teams = p.behavior_reactions, p.behavior_teams
        pm = self.game.pm
        can_write = self.game.write_enabled and self.behavior_writer.available(pm)
        if not (cfg.enabled and can_write):
            cfg.enabled = False
        self.engine.behavior_on = bool(cfg.enabled)     # stop styles and caution calls follow the driver's tendency
        ctx, cars = behavior_inputs(self.engine, res, snap, sp)
        # the planner counts on the pack saving fuel at drafting tracks (a stop fewer a stage)
        keep = self.behavior.effect(SAVE_FUEL, True).fuel
        self.engine.fuel_save_gain = (1.0 / keep - 1.0) if cfg.enabled and ctx.drafting and 0 < keep < 1 else 0.0
        out = self.behavior.tick(ctx, cars)
        for sw in out.switches:
            if sw.new:
                log.info("BEHAVIOR lap %d: #%s %s -> %s (%s)", self.engine.lap_now, sw.number, sw.name, sw.new,
                         sw.reason)
            elif sw.old and ctx.green and ctx.race:
                log.info("BEHAVIOR lap %d: #%s %s back to normal", self.engine.lap_now, sw.number, sw.name)
        on_track = {c.idx: c.on_track for c in cars}
        if can_write and (out.effects or self.behavior_writer.scale or self.behavior_writer.wear_last
                          or self.behavior_writer.fuel_last):
            self.behavior_writer.apply(pm, out.effects, on_track)
        self.engine.behavior_cars = set(out.effects)
        self.engine.behavior_switches = out.switches        # the radio calls them (crew chief: why it saves / goes)
        for r in res.rows:
            m = out.modes.get(r.idx)
            r.behavior = m[0] if m else ""

    def _behavior_restore(self):
        if self.game.pm is not None and self.game.write_enabled:
            n = self.behavior_writer.restore(self.game.pm)
            if n:
                log.info("Driver behavior: game pace values restored on %d cars", n)
        else:
            self.behavior_writer.reset()
        self.behavior.reset()
        self.engine.behavior_cars = set()
        self.engine.behavior_switches = []

    def _apply_lap_source(self, snap, now: float) -> str:
        """Lap source, best first: the leader's lap from the per-car counters in game
        memory; the companion app's feed (held a few seconds over feed hiccups); the
        player-car counter v1 used (0xBE0E48, stays 0 while you spectate)."""
        laps = self.feed.race_laps() if self.settings["use_companion"] else None
        if laps:
            self._feed_laps = (laps, now)
        elif self._feed_laps and now - self._feed_laps[1] < FEED_LAP_HOLD_S:
            laps = self._feed_laps[0]
        if snap.lap_source == "game":             # per-car lap counters in game memory
            if snap.laps_total <= 0 and laps:
                snap.laps_total = laps[1]
            return "game"
        if laps:
            complete, total = laps
            snap.laps_done = float(min(complete + 1, total))
            snap.laps_total = total
            return "companion app"
        return "player car"

    def _strategy_params(self, mem_tank: float) -> StrategyParams:
        p = self.params
        tank = p.tank_laps
        learned = self.learned_tank() if p.tank_source == "memory" else None
        src = "preset"
        if learned and learned >= 5:
            tank, src = learned, "learned"
        elif p.tank_source == "memory" and 5 <= mem_tank <= 1000:
            tank, src = mem_tank, "game"                         # (a tiny game value meant 2-lap tanks: pit loop)
        if getattr(self, "_tank_logged", None) != (src, round(tank, 1)):
            self._tank_logged = (src, round(tank, 1))
            log.info("Fuel: planning on %.1f laps a tank (%s; game value %.2f)", tank, src, mem_tank)
        return StrategyParams(
            wear_r=p.wear_r, wear_l=p.wear_l, urgency_factor=p.urgency_factor,
            damage_threshold=p.damage_threshold, tank_laps=tank, fuel_margin=p.fuel_margin,
            plan_margin=p.plan_margin, dispersion_divisor=p.dispersion_divisor,
            max_tire_sets=p.max_tire_sets, sets_include_start=True,
            plan_multi_stop=True, guard_game_stops=True,
            min_stop_gap=p.min_stop_gap, tire_only_stops=True, free_fuel=True,
            free_fuel_four=5 if self._crew_modern() else 4,       # modern crew: a full tank fits in 4 tires
            position_strategy=True, lead_pack_gap=p.lead_pack_gap,
            threat_positions=p.threat_positions, stint_strategy=p.stint_strategy,
            track_position_bias=p.track_position_bias, oem_strategy=p.oem_strategy, oem_gap=p.oem_gap,
            stint_spread=p.stint_spread, ss_stint_spread=p.ss_stint_spread, fuel_reach=p.fuel_reach,
            ss_topoff_pct=p.ss_topoff_pct, lapped_wave=p.lapped_wave, pace_strategy=True,
            worn_pct=p.worn_pct, team_strategy=p.team_strategy, follow_player=p.follow_player,
            value_tires=True,
            finish_fuel=p.finish_fuel, track_type=self.track_type(),
            game_plan_off="no_game_plan_stops" in self.game.applied_patches,
            rules_draft=self._team_rules()["draft"], rules_other=self._team_rules()["other"],
            stages=p.stages, stage_by=p.stage_by, stage_ends=tuple(parse_stage_ends(p.stage_ends) or ()),
            stage_early=p.stage_early, flip_pct=p.flip_pct,
            crew_brain=True)

    def _tank_key(self) -> str:
        return f"{self.mod or 'unknown mod'}|{self.track}"

    def learned_tank(self) -> float | None:
        """Laps per tank here: this session's learned value, else the one saved for this mod + track."""
        live = self.engine.field_tank
        if live:
            return live
        return (self.settings.get("tank_by_track") or {}).get(self._tank_key())

    def _save_learned_tank(self):
        live = self.engine.field_tank
        if live and self.track:
            book = self.settings.setdefault("tank_by_track", {})
            old = book.get(self._tank_key())
            book[self._tank_key()] = round(live, 1)
            if old is None or abs(old - live) >= 1.5:
                log.info("Fuel: %.1f laps a tank at %s (learned from real fuel)", live, self._tank_key())

    def _use_learned_tank(self):
        t = self.learned_tank()
        if t:
            self.vars["tank_laps"].set(f"{t:.1f}")

    def _update_fuel_label(self, mem_tank, used=None):
        t = self.learned_tank()
        if self.engine.field_tank:
            txt = f"Learned: {t:.1f} laps a tank (this race)"
        elif t:
            txt = f"Learned: {t:.1f} laps a tank (saved for this track)"
        else:
            txt = "Learned: - (needs a few green-flag laps)"
        self.lbl_mem_tank.configure(text=txt)
        if hasattr(self, "btn_use_learned"):
            self.btn_use_learned.state(["!disabled"] if t else ["disabled"])
        src = "learned" if self.params.tank_source == "memory" and t else "manual"
        if used is None:
            used = self.params.tank_laps
        self.lbl_fuel.configure(text=f"Tank: {used:.1f} laps ({src})")
        if hasattr(self, "lbl_mileage"):
            ft = self.engine.field_tank
            self.lbl_mileage.configure(text=f"Real fuel: {ft:.1f} laps a tank (field, learned)" if ft else
                                       "Real fuel: learning from green-flag laps")

    def _check_track(self):
        track = mod = ""
        if self.game.attached:
            track, mod = self.game.session_names()
        src = "game"
        if not track and self.settings["use_companion"] and self.feed.connected:
            track, src = self.feed.track, "companion app"
        if mod and mod != self.mod:
            self.mod = mod
            self._crew_applied = None             # each mod has its own crew choice
            log.info("NR2003 series (mod): %s", mod)
            self.engine.hand_off_runs()           # the last runs belong to the model still in use (old mod)
            self._use_tire_model()
            self._apply_art_choice()
            self._apply_make_names()
        if not track:
            self.lbl_track.configure(text="Track: - (waiting for a session)", foreground=DIM)
            self.btn_assign.state(["disabled"])
            return
        self.lbl_track.configure(text=f"Track: {track}" + (f"  [{self.mod}]" if self.mod else ""), foreground=FG)
        self.btn_assign.state(["!disabled"])
        if track != self.track:
            self.track = track
            self.engine.hand_off_runs()           # (before the switch: the old track's runs aren't the new one's)
            self._use_tire_model()
            log.info("Track detected (%s): %s", src, track)
            if self._last_session is not None:
                self.clear_session("track changed")
            if self.var_auto.get():
                name = self.store.find_for_track(track)
                if name and name != self.loaded_name:
                    self.load_preset(name)
                elif not name:
                    log.info("No preset linked to '%s' - load one and press 'Use this preset for the current track'",
                             track)

    def _columns_changed(self, save: bool = True):
        widths = {c.id: c.width for c in self.monitor.columns}
        widths.update(self.settings.get("monitor_widths", {}))
        visible = [dataclasses.replace(c, width=widths.get(c.id, c.width)) for c in MONITOR_COLUMNS
                   if self.col_vars[c.id].get()]
        if not visible:                                   # keep at least one column
            self.col_vars["name"].set(True)
            return self._columns_changed(save)
        self.settings["hidden_cols"] = [c.id for c in MONITOR_COLUMNS if not self.col_vars[c.id].get()]
        self.monitor.set_columns(visible)
        if save:
            self._render_monitor()

    def _toggle_setup(self, collapse: bool | None = None):
        shown = str(self.setup_holder) in self.body.panes()
        collapse = shown if collapse is None else collapse
        if collapse and shown:
            self.body.forget(self.setup_holder)
        elif not collapse and not shown:
            self.body.insert(0, self.setup_holder, weight=0)
        self.settings["setup_collapsed"] = collapse

    def _reset_columns(self):
        self.settings.pop("monitor_widths", None)
        self.settings.pop("pit_widths", None)
        self.monitor.set_widths({c.id: c.width for c in MONITOR_COLUMNS})
        self._columns_changed()
        self.pit_table.set_columns([dataclasses.replace(c) for c in self.pit_table.columns])
        self._render_pitlog(new_view=False)

    def _monitor_hover(self, key, col):
        if col != "name":
            return None
        r = next((r for r in self._last_rows if r.idx == key), None)
        if r is None:
            return None
        return f"#{r.number} {r.name}\n{r.make or 'make unknown'}  -  memory slot {r.idx}"

    # ------------------------------------------------------------ pit board
    def _build_pitboard_tab(self, nb):
        pb = self.settings["pitboard"]
        outer = ttk.Frame(nb)
        nb.add(outer, text="Pit board")
        tab = scrolled(outer, BG, padding=12)     # taller than the window: scrolls (it used to cut off the bottom)
        self.var_crew = tk.BooleanVar(value=bool(self.settings.get("crew_beta", False)))
        if self.beta:                            # testers only (beta.txt next to AIMOD.exe): first, so it's always seen
            beta = ttk.LabelFrame(tab, text="Beta", padding=10)
            beta.pack(fill="x", pady=(0, 12))
            cb = ttk.Checkbutton(beta, text="Crew chief on phones", variable=self.var_crew,
                                 command=self._crew_toggled)
            cb.pack(side="left")
            Tooltip(cb, "Beta. Phones (through RaceVision's Phone page) can follow any AI car the way its crew chief "
                        "does: fuel, tires, lap times, pit plan, radio. A phone that takes a car runs its pit "
                        "stops: caution calls (pit or stay out, tires, fuel), its next green-flag stop, and "
                        "chassis adjustments. AIMOD's safety rules still win, and AIMOD makes the call if a phone "
                        "doesn't answer in time.")
            b = ttk.Button(beta, text="Hand every car back to AIMOD", command=lambda: self.desk.release_all())
            b.pack(side="left", padx=(12, 0))
            Tooltip(b, "Every phone loses its car at once; AIMOD makes all the calls again.")
            self.lbl_crew = ttk.Label(beta, text="", style="Dim.TLabel")
            self.lbl_crew.pack(side="left", padx=(12, 0))
            self._crew_label()
        box = ttk.LabelFrame(tab, text="In-game pit board", padding=10)
        box.pack(fill="x")
        self.var_pb_on = tk.BooleanVar(value=pb["enabled"])
        ttk.Checkbutton(box, text="Show the pit board over NR2003", variable=self.var_pb_on,
                        command=self._pb_toggle).grid(row=0, column=0, columnspan=3, sticky="w")
        self.btn_pb_move = ttk.Button(box, text="Move", command=self._pb_move)
        self.btn_pb_move.grid(row=0, column=3, sticky="e", padx=(12, 0))
        Tooltip(self.btn_pb_move, "Unlocks the board so you can drag it into place, then press Lock. Drag the "
                                  "blue corner grip to resize: right/left = width, down/up = scale. Locked, it "
                                  "lets mouse clicks through to the game.")
        self.pb_vars = {}
        fields = (("scale", "Scale", float, "Overall size of the board (text, rows and width)."),
                  ("width", "Width (px)", int, "Narrow or widen the board (200-700, at scale 1). Wider gives "
                                               "long names and the service line more room."),
                  ("opacity", "Opacity (0.2-1)", float, "Whole-board transparency."),
                  ("max_rows", "Max rows", int, "Cars shown at once; the rest are summarised as '+N more'."),
                  ("hold", "Keep finished stops (s)", float,
                   "How long a car stays on the board after it leaves pit road, showing its service time and "
                   "places gained or lost (game seconds). Then it drops off and the rows below move up."))
        for r, (key, label, typ, tip) in enumerate(fields, start=1):
            lbl = ttk.Label(box, text=label, style="Panel.TLabel")
            lbl.grid(row=r, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=f"{pb[key]:g}" if isinstance(pb[key], float) else str(pb[key]))
            ent = ttk.Entry(box, textvariable=var, width=8, justify="right")
            ent.grid(row=r, column=1, sticky="w", padx=(8, 0), pady=2)
            Tooltip(lbl, tip)
            var.trace_add("write", lambda *_, k=key, t=typ, v=var, e=ent: self._pb_field(k, t, v, e))
            self.pb_vars[key] = var
        self.var_pb_compact = tk.BooleanVar(value=pb["compact"])
        cb = ttk.Checkbutton(box, text="Compact (no photos, one line per car)", variable=self.var_pb_compact,
                             command=lambda: (pb.update(compact=self.var_pb_compact.get()),
                                              self.pitboard.apply_settings()))
        cb.grid(row=len(fields) + 2, column=0, columnspan=3, sticky="w")
        Tooltip(cb, "Number on the driver's colour, position, name, places gained, service and time - no "
                    "headshots or big number art. For OBS add &compact=1 to the URL.")
        self.var_pb_hide = tk.BooleanVar(value=pb["auto_hide"])
        ttk.Checkbutton(box, text="Hide when pit road is empty", variable=self.var_pb_hide,
                        command=lambda: (pb.update(auto_hide=self.var_pb_hide.get()), self.pitboard.apply_settings())
                        ).grid(row=len(fields) + 1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(box, style="Dim.TLabel", wraplength=560, justify="left", text=(
            "Shows over NR2003 when the game runs windowed or borderless, like other overlays. Green "
            "flag: live pit road (PITTING, PIT ROAD, a service clock while the car sits in its stall, then "
            "what it took, its service time and places gained or lost). Caution: RACE OFF PIT ROAD - cars "
            "in the order they rejoined, with the drivers who stayed out listed on top. Number art, "
            "headshots and driver colours come from the artwork series below.")
                  ).grid(row=len(fields) + 4, column=0, columnspan=4, sticky="w", pady=(8, 0))

        art = ttk.LabelFrame(tab, text="Driver artwork", padding=10)
        art.pack(fill="x", pady=(12, 0))
        ttk.Label(art, text="Series", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        self.var_art = tk.StringVar(value=self.artdata.choice)
        self.cmb_art = ttk.Combobox(art, textvariable=self.var_art, state="readonly", width=18,
                                    values=[AUTO] + self.artdata.available_series())
        self.cmb_art.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.cmb_art.bind("<<ComboboxSelected>>", lambda e: self._art_changed())
        Tooltip(self.cmb_art, "Number art, headshots and driver colours for the pit board. auto = the companion app's "
                              "active series if one is used, otherwise the series whose number art best "
                              "matches the cars on track. Saved per NR2003 mod.")
        self.lbl_art = ttk.Label(art, style="Dim.TLabel", wraplength=560, justify="left")
        self.lbl_art.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        lg = ttk.LabelFrame(tab, text="Series logo", padding=10)
        lg.pack(fill="x", pady=(12, 0))
        self.var_logo = tk.BooleanVar(value=self.settings["pitboard"].get("logo", True))
        cb = ttk.Checkbutton(lg, text="Show a logo in the series bar", variable=self.var_logo,
                             command=self._logo_toggled)
        cb.grid(row=0, column=0, sticky="w")
        b = ttk.Button(lg, text="Choose logo...", command=self._choose_logo)
        b.grid(row=0, column=1, sticky="w", padx=(12, 0))
        Tooltip(b, "Pick a PNG or JPG (a real series logo, transparent PNG works best). It is copied into "
                   "AIMOD's art\\logos folder for the current NR2003 mod and used on the pit board and the OBS page.")
        b = ttk.Button(lg, text="Remove", command=self._remove_logo)
        b.grid(row=0, column=2, sticky="w", padx=(6, 0))
        Tooltip(b, "Remove your logo for this mod (the artwork series' own logo is used, if it has one).")
        self.lbl_logo = ttk.Label(lg, style="Dim.TLabel", wraplength=560, justify="left")
        self.lbl_logo.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self._update_art_label()

        web = ttk.LabelFrame(tab, text="Third-party OBS apps", padding=10)
        web.pack(fill="x", pady=(12, 0))
        port = self.board_server.port if hasattr(self, "board_server") else self.settings.get("board_port", 8767)
        self.btn_companion_widget = ttk.Button(web, text="Install / update overlay-app widget",
                                         command=lambda: self._sync_companion_widget(force=True))
        self.btn_companion_widget.grid(row=0, column=0, sticky="w")
        if not self.settings["use_companion"]:
            self.btn_companion_widget.state(["disabled"])
        self.lbl_companion_widget = ttk.Label(web, style="Dim.TLabel", wraplength=560, justify="left")
        self.lbl_companion_widget.grid(row=1, column=0, sticky="w", pady=(6, 0))
        urls = ttk.Entry(web, width=80)
        urls.insert(0, f"http://localhost:{port}/overlay      (via companion app: http://localhost:8766/widget/aimodpits)")
        urls.configure(state="readonly")
        urls.grid(row=2, column=0, sticky="we", pady=(6, 0))

        live = ttk.LabelFrame(tab, text="AIMOD Live (web dashboard)", padding=10)
        live.pack(fill="x", pady=(12, 0))
        self.var_lan = tk.BooleanVar(value=bool(self.settings.get("web_lan", False)))
        cb = ttk.Checkbutton(live, text="Let other computers on my network open AIMOD's pages", variable=self.var_lan,
                             command=self._lan_toggled)
        cb.grid(row=0, column=0, sticky="w")
        Tooltip(cb, "Read-only. Lets a second PC, laptop or tablet on the same network show the live running "
                    "order, pit log and pit board (for recording, streaming or a second screen). Windows asks "
                    "once to allow AIMOD through its firewall on private networks.")
        b = ttk.Button(live, text="Open dashboard", command=lambda: os.startfile(f"http://localhost:{port}/"))
        b.grid(row=0, column=1, sticky="e", padx=(12, 0))
        self.ent_live = ttk.Entry(live, width=80)
        self.ent_live.grid(row=1, column=0, columnspan=2, sticky="we", pady=(6, 0))
        ttk.Label(live, style="Dim.TLabel", wraplength=560, justify="left", text=(
            "Dashboard tabs: Running order (all Monitor columns), Pit log, Pit board. As an OBS browser source "
            "(1920 x 1080): /?view=monitor&bare=1 (add &cols=pos,num,name,gap,fuel,next,status to pick columns, "
            "&hideout=1, &scale=1.3), /?view=pitlog&bare=1, or /overlay for the pit board alone.")
                  ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._update_live_urls()
        ttk.Label(web, style="Dim.TLabel", wraplength=560, justify="left", text=(
            "Add either URL as an OBS browser source (300 x 700, transparent). Optional URL settings: "
            "?width=320&scale=1.5&rows=12&compact=1&always=1. The page reads live data and the driver art from "
            "AIMOD, so AIMOD must be running.")
                  ).grid(row=3, column=0, sticky="w", pady=(6, 0))

    def _crew_toggled(self):
        self.settings["crew_beta"] = bool(self.var_crew.get())
        if not self.settings["crew_beta"]:
            self.desk.release_all("handed back to AIMOD (crew chief beta off)")
            self.board_server.publish_named("crew", {"enabled": False})
        self._save_settings()

    def _crew_label(self):
        self.lbl_crew.configure(text="Phones running cars: " + self.desk.summary())
        self.lbl_crew.after(2000, self._crew_label)

    def _update_live_urls(self):
        if not hasattr(self, "ent_live") or not hasattr(self, "board_server"):
            return
        port = self.board_server.port
        text = f"This PC: http://localhost:{port}/"
        if self.settings.get("web_lan"):
            text += f"      Other computers: http://{lan_ip()}:{port}/"
        if not self.board_server.running:
            text += "      (server not running - port in use?)"
        self.ent_live.configure(state="normal")
        self.ent_live.delete(0, "end")
        self.ent_live.insert(0, text)
        self.ent_live.configure(state="readonly")

    def _lan_toggled(self):
        on = bool(self.var_lan.get())
        self.settings["web_lan"] = on
        self.board_server.restart(on)
        if on:
            self._allow_firewall(self.board_server.port)
        else:
            self._remove_firewall(self.board_server.port)
        self._update_live_urls()
        self._save_settings()

    def _remove_firewall(self, port: int):
        import subprocess
        name = f"AIMOD Live (TCP {port})"
        try:
            r = subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"],
                               capture_output=True, text=True, creationflags=0x08000000)
            if r.returncode == 0:
                log.info("Windows firewall: removed %s", name)
        except OSError:
            pass

    def _allow_firewall(self, port: int):
        """Inbound rule for the dashboard port on private networks (AIMOD runs as administrator)."""
        import subprocess
        name = f"AIMOD Live (TCP {port})"
        try:
            have = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                                  capture_output=True, text=True, creationflags=0x08000000)
            if have.returncode == 0 and "Rule Name" in have.stdout:
                return
            r = subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={name}", "dir=in",
                                "action=allow", "protocol=TCP", f"localport={port}", "profile=private"],
                               capture_output=True, text=True, creationflags=0x08000000)
            if r.returncode == 0:
                log.info("Windows firewall: allowed %s on private networks", name)
            else:
                log.warning("Could not add the firewall rule (%s); allow AIMOD when Windows asks",
                            (r.stdout or r.stderr).strip())
        except OSError as e:
            log.warning("Could not add the firewall rule: %s", e)

    def _art_changed(self):
        choice = self.var_art.get()
        self.settings.setdefault("art_series_by_mod", {})[self.mod or "default"] = choice
        self.artdata.choice = choice
        self.artdata.refresh(force=True)
        self.pitboard.apply_settings()
        self._update_art_label()

    def _logo_dir(self) -> str:
        return os.path.join(self.base, "art", "logos")

    def _logo_toggled(self):
        self.settings["pitboard"]["logo"] = bool(self.var_logo.get())
        self._update_art_label()

    def _choose_logo(self):
        path = filedialog.askopenfilename(parent=self.root, title="Series logo for the pit board",
                                          filetypes=[("Images", "*.png *.jpg *.jpeg"), ("All files", "*.*")])
        if not path:
            return
        dest = os.path.join(self._logo_dir(), self.artdata.logo_name(self.mod))
        try:
            from PIL import Image
            os.makedirs(self._logo_dir(), exist_ok=True)
            im = Image.open(path).convert("RGBA")
            if im.height > 300:                              # plenty for a 30 px bar at 3x scale
                im = im.resize((max(1, round(im.width * 300 / im.height)), 300), Image.LANCZOS)
            im.save(dest, "PNG")
        except Exception as e:
            messagebox.showerror("Series logo", f"Could not use that image:\n{e}", parent=self.root)
            return
        log.info("Pit board logo for %s: %s", self.mod or "default", dest)
        self.var_logo.set(True)
        self._logo_toggled()
        self.pitboard.apply_settings()

    def _remove_logo(self):
        dest = os.path.join(self._logo_dir(), self.artdata.logo_name(self.mod))
        try:
            os.remove(dest)
            log.info("Removed pit board logo %s", dest)
        except FileNotFoundError:
            pass
        except OSError as e:
            messagebox.showerror("Series logo", f"Could not remove it:\n{e}", parent=self.root)
        self._update_art_label()
        self.pitboard.apply_settings()

    def _update_art_label(self):
        if hasattr(self, "lbl_logo"):
            rel, src = self.artdata.rel_logo()
            on = self.settings["pitboard"].get("logo", True)
            self.lbl_logo.configure(text=(f"Using: {src}." if on else "Off: the series name is shown as text.")
                                    + f"\nYour logos are saved in {self._logo_dir()} as <NR2003 mod>.png "
                                      f"(this mod: {self.artdata.logo_name(self.mod)}).")
        if not hasattr(self, "lbl_art"):
            return
        folders = ", ".join(self.artdata.roots) or "none found"
        self.lbl_art.configure(text=f"Using: {self.artdata.series or 'none'} ({self.artdata.series_name or '-'}) - "
                                    f"{getattr(self.artdata, 'how', '')}.\nArtwork folders: {folders}. Add photos as "
                                    "series/<series>/headshots/<car number>.png.")

    def _pb_resized(self):
        """Corner-grip resize in Move mode: show the new numbers in the Scale / Width boxes."""
        pb = self.settings["pitboard"]
        self._pb_syncing = True
        for key in ("scale", "width"):
            if key in self.pb_vars:
                v = pb[key]
                self.pb_vars[key].set(f"{v:g}" if isinstance(v, float) else str(v))
        self._pb_syncing = False

    def _pb_field(self, key, typ, var, ent):
        if getattr(self, "_pb_syncing", False):
            return
        try:
            val = typ(float(var.get())) if typ is int else typ(var.get())
            if val <= 0:
                raise ValueError
            self.settings["pitboard"][key] = val
            ent.configure(style="TEntry")
            self.pitboard.apply_settings()
        except ValueError:
            ent.configure(style="Bad.TEntry")

    def _pb_toggle(self):
        on = self.var_pb_on.get()
        self.settings["pitboard"]["enabled"] = on
        if on:
            self.pitboard.show()
        else:
            self.pitboard.set_unlocked(False)
            self.btn_pb_move.configure(text="Move")
            self.pitboard.hide()

    def _pb_move(self):
        if self.pitboard.win is None:
            self.var_pb_on.set(True)
            self._pb_toggle()
        unlock = not self.pitboard.unlocked
        self.pitboard.set_unlocked(unlock)
        self.btn_pb_move.configure(text="Lock" if unlock else "Move")
        if not unlock:
            self._save_settings()

    def _companion_dir(self) -> str:
        return companion_dir()

    def _sync_companion_widget(self, force: bool = False):
        """Keep nr2003_aimodpits.html in the companion app folder up to date. The companion app's bridge serves
        any nr2003_<name>.html there at /widget/<name> and doesn't overwrite files it doesn't own."""
        d = self._companion_dir()
        path = os.path.join(d, WIDGET_FILE)
        if not os.path.isdir(d):
            msg = "Companion app folder not found - use the AIMOD URL instead."
        elif not force and not os.path.exists(path):
            msg = "Not installed in the companion app yet."
        else:
            try:
                cur = open(path, encoding="utf-8").read() if os.path.exists(path) else None
                if cur != WIDGET_HTML:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(WIDGET_HTML)
                    log.info("Installed overlay-app widget %s", path)
                msg = f"Installed: {path}"
            except OSError as e:
                msg = f"Could not write {path}: {e}"
        if hasattr(self, "lbl_companion_widget"):
            self.lbl_companion_widget.configure(text=msg)

    def _update_pitboard(self, snap, res):
        hold = float(self.settings["pitboard"].get("hold", 12))
        series = self.artdata.series
        self.artdata.set_field(c.number for c in snap.cars if c.valid)
        self.artdata.refresh()
        if self.artdata.series != series:
            self._apply_make_names()
            self._update_art_label()
        events = self.engine.pit_board(hold)
        positions = {r.idx: r.pos for r in res.rows if r.pos}
        stayed = sorted((r for r in res.rows if r.kind == "stayout" and not r.laps_down and r.pos),
                        key=lambda r: r.pos)
        data = board_payload(events, self.engine.now, snap.flag == mm.FLAG_GREEN, int(snap.laps_done),
                             res.laps_total or snap.laps_total, positions, artdata=self.artdata, stayed_out=stayed,
                             track=self.track, race=snap.session == mm.SESSION_RACE,
                             logo=self.settings["pitboard"].get("logo", True))
        self.board_server.publish(data)
        self.pitboard.render(data)

    def _series_key(self) -> str:
        return self.mod or self.artdata.series or "default"

    def _apply_art_choice(self):
        self.artdata.set_hint(self.mod)
        self.artdata.choice = (self.settings.get("art_series_by_mod") or {}).get(self.mod or "default", AUTO)
        self.artdata.refresh(force=True)
        if hasattr(self, "var_art"):
            self.var_art.set(self.artdata.choice)
            self._update_art_label()

    def _apply_make_names(self):
        names = (self.settings.get("make_names_by_series") or {}).get(self._series_key())
        if not names:
            names = self.settings.get("make_names") or mm.MAKE_NAMES
        self.game.make_names = [str(x) for x in names]

    def _name_makes(self):
        """Show each make number in the field with a few of its drivers, and let the user name it."""
        seen: dict[int, list[str]] = {}
        for c in (self._last_snap.cars if self._last_snap else []):
            if c.valid and c.make_id >= 0 and c.number >= 0:
                seen.setdefault(c.make_id, []).append(f"#{c.number} {c.name.split()[-1] if c.name else ''}")
        n = max([len(self.game.make_names)] + [k + 1 for k in seen])
        dlg = tk.Toplevel(self.root)
        dlg.title("Name manufacturers")
        dlg.configure(bg=PANEL, padx=12, pady=12)
        dlg.transient(self.root)
        ttk.Label(dlg, style="Panel.TLabel", wraplength=460, justify="left", text=(
            f"NR2003 series: {self._series_key()}. NR2003 gives each car a make number; type the make for each "
            "number (the drivers listed help tell them apart). Start a race first so the cars are known.")
                  ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        entries = []
        for k in range(n):
            ttk.Label(dlg, text=f"Make {k}", style="Panel.TLabel").grid(row=k + 1, column=0, sticky="w", padx=(0, 8))
            v = tk.StringVar(value=self.game.make_names[k] if k < len(self.game.make_names) else "")
            ttk.Entry(dlg, textvariable=v, width=14).grid(row=k + 1, column=1, sticky="w", pady=2)
            sample = ", ".join(seen.get(k, [])[:4]) or "(no cars in this session)"
            ttk.Label(dlg, text=sample, style="Dim.TLabel").grid(row=k + 1, column=2, sticky="w", padx=(8, 0))
            entries.append(v)

        def save():
            names = [v.get().strip() or f"Make {k}" for k, v in enumerate(entries)]
            self.settings.setdefault("make_names_by_series", {})[self._series_key()] = names
            self._apply_make_names()
            self._save_settings()
            log.info("Manufacturer names for %s: %s", self._series_key(), ", ".join(names))
            dlg.destroy()
        ttk.Button(dlg, text="Save", style="Accent.TButton", command=save).grid(row=n + 1, column=1, sticky="w",
                                                                                pady=(10, 0))

    def _team_rules(self) -> dict[str, TeamRules]:
        saved = (self.settings.get("team_rules_by_mod") or {}).get(self._series_key()) or {}
        return {"draft": TeamRules.from_dict(saved.get("draft"), DRAFT_RULES),
                "other": TeamRules.from_dict(saved.get("other"), OTHER_RULES)}

    def _edit_rules(self):
        def save(new):
            self.settings.setdefault("team_rules_by_mod", {})[self._series_key()] = new
            self._save_settings()
            log.info("Team/manufacturer rules for %s: drafting %s; other %s", self._series_key(),
                     new["draft"], new["other"])
        RulesDialog(self.root, self._team_rules(), {"draft": DRAFT_RULES, "other": OTHER_RULES}, save,
                    title=f"Team & manufacturer strategy - {self._series_key()}")

    def _radio_teams(self) -> list[dict]:
        """Teams for the scanner: the loaded mod's; before a session is loaded (mod not known yet) or for a mod
        with none, every saved team."""
        saved = self.settings.get("teams_by_mod") or {}
        teams = normalize_teams(saved.get(self._series_key()) or [])
        if not teams:
            seen = set()
            for v in saved.values():
                for t in normalize_teams(v):
                    if t["name"] and t["name"] not in seen:
                        seen.add(t["name"])
                        teams.append(t)
        return teams

    def _team_of(self) -> dict[int, int]:
        teams = normalize_teams((self.settings.get("teams_by_mod") or {}).get(self._series_key()) or [])
        return {n: k for k, t in enumerate(teams) for n in t["cars"]}

    def _edit_teams(self):
        numbers = [c.number for c in (self._last_snap.cars if self._last_snap else []) if c.valid and c.number >= 0]
        teams = (self.settings.get("teams_by_mod") or {}).get(self._series_key()) or []
        colors = {}
        for c in (self._last_snap.cars if self._last_snap else []):
            if c.valid:
                colors[c.number] = (self.artdata.color(c.number) or
                                    {"chevrolet": "#c99a2e", "dodge": "#b3122e", "ford": "#1f4e9c",
                                     "toyota": "#d7261e"}.get((c.make or "").lower(), "#3a3a3a"))

        def save(new_teams):
            self.settings.setdefault("teams_by_mod", {})[self._series_key()] = new_teams
            self._save_settings()
            log.info("Teams for %s: %s", self._series_key(),
                     "; ".join(f"{t['name'] or 'Team'}: " + " ".join(f"#{n}" for n in t["cars"])
                               for t in new_teams) or "none")
        TeamsDialog(self.root, numbers, teams, colors, save, title=f"Teams - {self._series_key()}",
                    from_car_files=self._teams_from_car_files)

    def _teams_from_car_files(self):
        """Teams from the .car files: the session's cars when NR2003 is in one, else the whole cars folder."""
        from . import carfiles
        paths, how = [], ""
        if self.game.pm is not None and self.mod:
            paths = self.game.session_car_files(self.mod)
            how = "this session's car files"
        if not paths:
            folder = carfiles.cars_folder(self.settings.get("nr2003_dir") or r"C:\Papyrus\NASCAR Racing 2003 Season",
                                          self.mod or "")
            if self.mod and os.path.isdir(folder):
                paths = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(".car")]
                how = "the mod's cars folder"
        teams = carfiles.teams_from([carfiles.car_info(x) for x in paths])
        log.info("Teams from %s: %d car files, %d teams", how or "car files", len(paths), len(teams))
        return teams, (how if teams else "No car files found (start a session in NR2003 first).")

    def _update_stage_label(self):
        p = self.params
        if not hasattr(self, "lbl_stages"):
            return
        if p.stages < 2:
            self.lbl_stages.configure(text="No stages")
            return
        ends = self.engine.stage_list
        if not ends:
            total = self._last_snap.laps_total if self._last_snap else 0
            self.lbl_stages.configure(text=("Stage ends: waiting for the race length" if not total else
                                            "Stage ends don't fit this race - check the values"))
            return
        done = self.engine.stage_done
        self.lbl_stages.configure(text="Stage ends: " + ", ".join(
            f"lap {e}" + (" (done)" if e in done else "") for e in ends))

    def _update_learned(self):
        got = self.engine.pace.learned()
        if not got:
            n = len(self.engine.pace.samples)
            self.lbl_learned.configure(text=f"Learned thresholds: collecting ({n}/5 runs)")
            self.btn_learned.state(["disabled"])
            return
        left, right, runs = got
        self._learned = (left, right)
        self.lbl_learned.configure(text=f"Learned at this track: right {right:.3f}, left {left:.3f} ({runs} runs)")
        self.btn_learned.state(["!disabled"])

    def _apply_learned(self):
        if getattr(self, "_learned", None):
            left, right = self._learned
            self.vars["wear_l"].set(f"{left:.3f}")
            self.vars["wear_r"].set(f"{right:.3f}")
            log.info("Applied learned wear thresholds: right %.3f, left %.3f (Save to keep them)", right, left)

    def _black_flags(self) -> set[int]:
        now = time.monotonic()
        if now - self._flag_check >= 1.0:
            self._flag_check = now
            self._mem_flags = self.game.black_flags()
        flagged = set(self._mem_flags)
        use_feed = self.settings["use_companion"] and self.feed.fresh
        for s_ in (self.feed.latest().get("standings") or []) if use_feed else []:
            if isinstance(s_, dict) and s_.get("blackFlag"):
                flagged.add(s_.get("carIdx"))
        return flagged

    def _companion_toggle(self):
        on = bool(self.var_sf.get())
        self.settings["use_companion"] = on
        self.artdata.companion = on
        self.artdata.refresh(force=True)
        if on and not self.feed.running:
            self.feed.start()
        elif not on and self.feed.running:
            self.feed.stop()
        self.cmb_art.configure(values=[AUTO] + self.artdata.available_series())
        self.btn_companion_widget.state(["!disabled"] if on else ["disabled"])
        self._update_art_label()
        log.info("Companion app data %s", "used" if on else "not used")

    def track_type(self) -> str:
        """Preset choice, else: drafting track -> superspeedway, road-course name -> road course,
        under ~0.95 mile -> short track, everything else intermediate."""
        choice = self.params.track_type
        if choice in (SUPERSPEEDWAY, INTERMEDIATE, SHORT_TRACK, ROAD_COURSE):
            kind = choice
        elif self._is_drafting():
            kind = SUPERSPEEDWAY
        elif any(n in (self.track or "").lower() for n in ROAD_COURSE_NAMES):
            kind = ROAD_COURSE
        elif 100 < self.engine.track_len < SHORT_TRACK_M:
            kind = SHORT_TRACK
        else:
            kind = INTERMEDIATE
        if hasattr(self, "lbl_track_type"):
            miles = self.engine.track_len / 1609.34 if self.engine.track_len > 100 else 0
            wear = self.engine.wear_level(self._last_sp) if kind == INTERMEDIATE and self._last_sp else ""
            self.lbl_track_type.configure(text=f"{'Detected' if choice == 'auto' else 'Set'}: "
                                               f"{TRACK_TYPE_NAMES[kind]}" + (f", {miles:.2f} mi" if miles else "")
                                               + (f", {wear} tire wear" if wear else ""))
        return kind

    def _is_drafting(self) -> bool:
        mode = self.params.drafting
        auto = any(t in self.track.lower() for t in DRAFTING_TRACKS)
        on = auto if mode == "auto" else mode == "yes"
        if hasattr(self, "lbl_drafting"):
            self.lbl_drafting.configure(text=f"{'Drafting' if on else 'Not a drafting'} track"
                                             f"{' (auto)' if mode == 'auto' else ''}")
        return on

    @staticmethod
    def _level(value, good, ok, higher_is_better=False):
        if value is None:
            return None
        if higher_is_better:
            return GOOD_C if value > good else OK_C if value > ok else BAD_C
        return GOOD_C if value < good else OK_C if value < ok else BAD_C

    def _render_monitor(self):
        hide_out = self.var_hide_out.get()
        colors_on = self.var_colors.get()
        ids = [c.id for c in self.monitor.columns]
        rows, web = [], []
        for r in self._last_rows:
            if hide_out and r.out:
                continue
            gap = ""
            if r.laps_down:
                gap = f"-{r.laps_down}L"
            elif r.pos == 1:
                gap = "LEADER"
            elif r.gap is not None:
                gap = f"+{r.gap:.1f}"
            cells = {"pen": "BLACK" if r.black_flag else ("FREE PASS" if r.free_pass else "WAVE" if r.waved else ""),
                     "pos": r.pos or "",
                     "num": r.number, "name": r.name, "make": r.make, "lap": r.lap or "",
                     "gap": gap, "rating": r.rating, "stops": r.stops, "last": r.last_pit, "status": r.status,
                     "behavior": r.behavior,
                     "idx": r.idx}
            if not r.out:
                cells.update(grip=f"{r.grip:.3f}", wear_l=f"{r.wear_l:.3f}", wear_r=f"{r.wear_r:.3f}",
                             prob_l=f"{r.prob_l}%", prob_r=f"{r.prob_r}%", damage=f"{r.damage:.3f}",
                             urgency=f"{r.urgency:.1f}", tires=r.tires_left, next=r.next_pit,
                             pace=f"{r.pace_delta:+.2f}" if r.pace_delta is not None else "",
                             falloff=f"{r.falloff * 100:.1f}%" if r.falloff is not None else "",
                             fuelst=f"{r.fuel_margin:+.0f}" if r.fuel_margin is not None else r.fuel_state,
                             fuel=str(r.green_laps) if r.fuel_laps is not None else "",
                             fuelpct=(("" if r.fuel_real else "~") + f"{min(1.0, r.fuel_frac) * 100:.0f}%")
                             if r.fuel_frac is not None else "",
                             fuelleft=(("" if r.fuel_real else "~") + f"{r.fuel_laps:.1f}")
                             if r.fuel_laps is not None else "",
                             mileage=(("" if r.own_mileage else "~") + f"{r.car_tank:.1f}")
                             if r.car_tank else "")
            if not r.out and (r.grip_est or (r.kind == "player" and not r.grip)):
                # the game keeps no grip figure for the player's car: ~ = from the AI on tires worn like it
                cells["grip"] = f"~{r.grip:.3f}" if r.grip else ""
            if not r.out and not r.wear_known:
                cells.update(wear_l="", wear_r="", prob_l="", prob_r="")
            colors = {}
            if colors_on and not r.out:
                cl, cr = self._level(r.prob_l, 60, 90), self._level(r.prob_r, 60, 90)
                cf = self._level(r.fuel_frac, 0.5, 0.2, higher_is_better=True)
                cm = None
                if r.fuel_margin is not None and r.fuel_margin >= 0:
                    cm = GOOD_C if r.fuel_margin >= self.params.fuel_margin else OK_C
                colors = {"wear_l": cl, "prob_l": cl, "wear_r": cr, "prob_r": cr, "fuel": cf, "fuelst": cm,
                          "fuelpct": cf, "fuelleft": cf}
            if colors_on and r.pace_delta is not None:
                colors["pace"] = self._level(r.pace_delta, 0.10, 0.40)
            if colors_on and r.falloff is not None:
                colors["falloff"] = self._level(r.falloff * 100, 0.5, 1.0)
            if r.black_flag:
                colors["pen"] = BAD_C
            elif r.waved or r.free_pass:
                colors["pen"] = "#4aa8ff"
            rows.append(Row(r.idx, [str(cells.get(i, "")) for i in ids], [colors.get(i) for i in ids],
                            fg=KIND_COLORS.get(r.kind, FG), sort={"gap": (r.laps_down * 1000 + (r.gap or 0))
                                                                  if not r.out else None}))
            col = self.artdata.color(r.number) if r.number is not None else None
            web.append({"cells": {k: str(v) for k, v in cells.items() if k != "idx"}, "fg": KIND_COLORS.get(r.kind, FG),
                        "colors": {k: v for k, v in colors.items() if v}, "out": r.out, "color": col,
                        "color_fg": self._text_on(col) if col else None})
        self.monitor.set_rows(rows)
        self._publish_web(web)

    @staticmethod
    def _text_on(color: str) -> str:
        try:
            r, g, b = (int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
            return "#000000" if 0.299 * r + 0.587 * g + 0.114 * b > 160 else "#ffffff"
        except ValueError:
            return "#ffffff"

    def _publish_web(self, web_rows):
        """AIMOD Live dashboard data: the monitor (all columns) and the latest stops."""
        if not hasattr(self, "board_server"):
            return
        head = {"track": self.track or "", "laps": self.lbl_laps.cget("text"), "flag": self.lbl_flag.cget("text"),
                "caution": bool(self.lbl_flag.cget("text")) and self.lbl_flag.cget("text") != "GREEN",
                "stage": "", "rows": web_rows}
        nxt = self.engine.next_stage_end() if self.params.stages >= 2 else None
        if nxt is not None:
            head["stage"] = f"Stage {self.engine.stage_no(nxt)} ends lap {nxt}"
        self.board_server.publish_named("monitor", head)
        stops = []
        for e in reversed(self.pit_events[-120:]):
            stops.append({"lap": e.lap, "number": str(e.number), "name": e.name, "stop_no": e.stop_no, "code": e.code,
                          "reason": e.reason, "green": e.green, "caution_no": e.caution_no, "pos_in": e.pos_in,
                          "pos_out": e.pos_out, "box_time": round(self._stop_time(e) or 0, 2),
                          "road_time": round(e.road_time or 0, 1), "note": e.note})
        self.board_server.publish_named("pitlog", {"stops": stops})

    # -------------------------------------------------------------- pit log
    def _new_session_reason(self, snap, lap_src) -> str:
        """Why this snapshot is a new session (empty = same session). Uses signals that change in a
        new race even without restarting NR2003: session type, the leader's lap count from the
        per-car counters, and NR2003's race clock going backwards."""
        valid = [c.laps_done for c in snap.cars if c.valid]
        leader = max(valid, default=0)
        last_leader, last_clock = self._last_leader, self._last_clock
        dropped = len(valid) >= 5 and last_leader >= 3 and leader + 2 < last_leader
        # a drop must hold for ~1 s (8 ticks) so one bad memory read can't wipe a race
        self._drop_ticks = self._drop_ticks + 1 if dropped else 0
        if not dropped and len(valid) >= 5:
            self._last_leader = leader
        if snap.session_time > 0:
            self._last_clock = snap.session_time
        if self._last_session is None:
            return ""
        if snap.session != self._last_session:
            self._last_leader = leader
            return "session type changed"
        if self._drop_ticks >= 8:
            self._drop_ticks, self._last_leader = 0, leader
            return f"leader's lap count went from {last_leader} to {leader}"
        if last_clock and 0 < snap.session_time < last_clock - 30:
            return "race clock restarted"
        return ""

    def _gate_tick(self, snap, res):
        """Arm the pit gate while a race runs (green flag has flown, checkered not yet out); log the cars it held."""
        pm = self.game.pm
        if pm is None:
            return
        want = (bool(self.settings.get("pit_gate", True)) and self.game.write_enabled
                and snap.session == mm.SESSION_RACE and self.engine.race_started and res.laps_to_go > 0)
        if want:
            self.pitgate.install(pm)
            self.pitgate.set_allow(pm, self.engine.gate_allow(snap))
            if self.pitgate.arm(pm):
                if not self._gate_armed:
                    self.pitgate.status = "Pit gate: on (only AIMOD sends cars to pit road)"
                    log.info("Pit gate on (v2): AI cars turn for pit road only on AIMOD's call, a penalty / repair "
                             "/ tow stop or an empty tank - the game's own pit request no longer opens it")
                self._gate_armed = True
        elif self._gate_armed or self.pitgate.cave is not None:
            self.pitgate.disarm(pm)
            if self._gate_armed:
                self.pitgate.status = "Pit gate: ready (off outside a race)"
                log.info("Pit gate off (%s)", "turned off" if not self.settings.get("pit_gate", True)
                         else "race over" if snap.session == mm.SESSION_RACE else "not a race")
            self._gate_armed = False
        held = self.pitgate.held(pm)
        if not held:
            return
        now = self.engine.now
        by_slot = {c.idx: c for c in snap.cars if c.valid}
        for slot, n in held.items():
            self._gate_held_total += n
            c = by_slot.get(slot)
            last = self._gate_logged.get(slot)
            if c is None or (last is not None and 0 <= now - last < 60.0):
                continue
            self._gate_logged[slot] = now
            green = snap.flag == mm.FLAG_GREEN
            log.info("Pit gate: held #%s %s on track - the game turned it for pit road %s, AIMOD didn't call it",
                     c.number, c.name, "under green" if green else "under caution")
            self.engine.note_gate_hold(slot, green)

    def _start_pit_guard(self):
        """Under caution NR2003 sets a car's pit request on its own, and the car takes the pit lane within a few
        frames - often before AIMOD's next tick (150 ms; far more frames at time acceleration). This thread
        clears the request every 20 ms for the cars AIMOD keeps out, so they drive past pit road."""
        if getattr(self, "_guard_thread", None) is not None:
            return
        import threading

        def run():
            cleared = 0
            while not getattr(self, "_closing", False):
                time.sleep(0.02)
                ids = getattr(self, "_keep_out", frozenset())
                if ids and self.game.pm is not None:
                    n = self.game.hold_off(ids)
                    if n:
                        cleared += n
                        if cleared in (1, 10, 100) or cleared % 1000 == 0:
                            log.info("Pit guard: held off %d game pit request(s) for cars AIMOD keeps out", cleared)
        self._guard_thread = threading.Thread(target=run, name="pit-guard", daemon=True)
        self._guard_thread.start()

    def _apply_adjust_effect(self, ev):
        """A crew chief adjustment changes the car's handling: its chassis rating in game memory (car block
        +5998, read live by the AI's car control). The session's own value is kept and put back when the
        session ends or AIMOD closes."""
        adj = getattr(ev, "adjust", None)
        if adj is None or not getattr(adj, "effect", 0.0) or self.game.pm is None or not self.game.write_enabled:
            return
        if not self.settings.get("adjust_handling", True):
            return
        import struct
        addr = mm.CAR_BLOCK_0 + ev.idx * mm.CAR_STRIDE + CHASSIS_VALUE
        try:
            base = self._chassis_base.get(ev.idx)
            if base is None:
                base = struct.unpack("<f", self.game.pm.read_bytes(addr, 4))[0]
                if not 0.05 < base < 3.0:
                    return
                self._chassis_base[ev.idx] = base
            total = self.engine.crew_chief.state(ev.idx).chassis
            self.game.pm.write_bytes(addr, struct.pack("<f", max(0.05, base + total)), 4)
            log.info("#%s %s: %s -> chassis %.3f (%+.3f, %s)", ev.number, ev.name, adj.short, base + total,
                     adj.effect, "good call" if adj.good_call else "misread")
        except Exception as e:
            log.debug("adjust effect failed: %s", e)

    def _restore_chassis(self):
        import struct
        if self.game.pm is not None:
            for idx, base in self._chassis_base.items():
                try:
                    self.game.pm.write_bytes(mm.CAR_BLOCK_0 + idx * mm.CAR_STRIDE + CHASSIS_VALUE,
                                             struct.pack("<f", base), 4)
                except Exception:
                    pass
        self._chassis_base = {}

    def clear_session(self, why: str = "cleared by hand"):
        """Forget everything about the previous race: strategy state, pit log, pit board, monitor."""
        log.info("New session (%s) - AIMOD data cleared", why)
        self._restore_chassis()
        self._behavior_restore()

        self.engine.reset()                       # also hands the finished runs to the tire model
        self.tire_model.save()
        self.wavelog.reset()
        self._new_pit_session()
        self._last_rows = []
        self._flagged = set()
        self._render_monitor()
        self._render_pitlog(new_view=True)
        self.pitboard.render({})

    def _clear_clicked(self):
        if messagebox.askyesno("New session", "Clear AIMOD's data for this race (strategy state, pit log, "
                                              "pit board)? Saved CSV logs are kept.", parent=self.root):
            self.clear_session()

    def _new_pit_session(self):
        self.pitlog.new_session()
        self.pit_events = []
        self._pit_dirty = True

    def _pit_view_changed(self):
        self.settings["pit_view"] = self.var_pit_view.get()
        self.settings["pit_sort"] = self.var_pit_sort.get()
        self.cmb_pit_sort.state(["!disabled"] if self.var_pit_view.get() == "driver" else ["disabled"])
        self.pit_table.sortable = self.var_pit_view.get() != "caution"
        self.pit_table.sort_col = None
        self._render_pitlog(new_view=True)

    @staticmethod
    def _stop_time(ev: PitEvent):
        if ev.box_time and ev.box_time > 0.5:
            return ev.box_time
        return None

    def _stop_color(self, ev: PitEvent):
        if ev.pos_pending:
            return FG
        if ev.pos_out is None:
            return KIND_COLORS["pitting"]
        if ev.pos_in and ev.pos_out:
            d = ev.pos_in - ev.pos_out
            return GOOD_C if d > 0 else BAD_C if d < 0 else FG
        return FG

    def _render_pitlog(self, new_view: bool = False):
        self._pit_dirty = False
        if self.var_pit_view.get() == "stops":
            self._render_pit_stops(new_view)
        elif self.var_pit_view.get() == "caution":
            self._render_pit_cautions()
        else:
            self._render_pit_drivers()

    def _render_pit_drivers(self):
        by_car: dict[int, list[PitEvent]] = {}
        for ev in self.pit_events:
            by_car.setdefault(ev.idx, []).append(ev)
        n = max([3] + [len(v) for v in by_car.values()])
        cols = [Column("num", "Car", 44, "e"), Column("name", "Driver", 150, "w"),
                Column("stops", "Stops", 44, "e"), Column("best", "Best stop", 66, "e",
                                                          "Quickest time stopped in the box (game seconds)")]
        cols += [Column(f"s{k}", f"Stop {k}", 124, "w", STOP_TIP) for k in range(1, n + 1)]
        if [c.id for c in cols] != [c.id for c in self.pit_table.columns]:
            self.pit_table.set_columns(cols)
            self.pit_table.set_widths(self.settings.get("pit_widths", {}))
        order = {r.idx: r.pos or 999 for r in self._last_rows}
        rows = []
        for idx, evs in by_car.items():
            times = [t for t in (self._stop_time(e) for e in evs) if t is not None]
            best = min(times) if times else None
            cells = [str(evs[0].number), evs[0].name, str(len(evs)), f"{best:.1f}" if best else ""]
            colors = [None, None, None, None]
            for e in evs:
                t = self._stop_time(e)
                cells.append(f"{e.lap}  {e.code}" + (f"  {t:.1f}s" if t else ""))
                colors.append(self._stop_color(e))
            gained = sum((e.pos_in - e.pos_out) for e in evs if e.pos_in and e.pos_out)
            rows.append((idx, evs, best, gained, Row(("car", idx), cells, colors)))
        mode = self.var_pit_sort.get()
        keys = {"Running order": lambda t: order.get(t[0], 999),
                "Car #": lambda t: t[1][0].number,
                "Driver": lambda t: t[1][0].name.lower(),
                "Most stops": lambda t: -len(t[1]),
                "Fastest stop": lambda t: t[2] if t[2] is not None else 1e9,
                "Places gained": lambda t: -t[3]}
        rows.sort(key=keys.get(mode, keys["Running order"]))
        self.pit_table.sortable = False
        self.pit_table.set_rows([t[4] for t in rows])

    def _render_pit_cautions(self):
        """Stops grouped by caution (in the order cars came off pit road) and by green-flag stretch."""
        cols = [Column("lap", "Lap", 44, "e"), Column("num", "Car", 44, "e"), Column("name", "Driver", 230, "w"),
                Column("stop", "Stop", 40, "e"), Column("svc", "Service", 64, "w", STOP_TIP),
                Column("reason", "Reason", 74, "w"), Column("pin", "Pos in", 52, "e"),
                Column("pout", "Pos out", 58, "e"), Column("gain", "+/-", 40, "e"),
                Column("box", "Service (s)", 74, "e", "Time the car sat stationary in its stall, game seconds"),
                Column("dmg", "Damage", 96, "w", "Damage when the stop started -> when the car rejoined"),
                Column("note", "Why", 260, "w")]
        if [c.id for c in cols] != [c.id for c in self.pit_table.columns]:
            self.pit_table.set_columns(cols)
            self.pit_table.set_widths(self.settings.get("pit_widths", {}))
        self.pit_table.sortable = False
        groups: dict[tuple, list] = {}
        for k, e in enumerate(self.pit_events):
            groups.setdefault((not e.green, e.caution_no), []).append((k, e))
        rows = []
        for (caution, n), items in sorted(groups.items(), key=lambda kv: min(e.time for _, e in kv[1]),
                                          reverse=True):
            laps = [e.lap for _, e in items]
            gained = sum((e.pos_in - e.pos_out) for _, e in items if e.pos_in and e.pos_out)
            if caution:
                title = f"CAUTION {n}  -  lap {min(laps)}  -  {len(items)} stop{'s' if len(items) != 1 else ''}"
                items.sort(key=lambda ke: (ke[1].pos_out or 999, ke[1].time))   # order off pit road
                color = KIND_COLORS["pitting"]
            else:
                span = f"lap {min(laps)}" if min(laps) == max(laps) else f"laps {min(laps)}-{max(laps)}"
                title = f"GREEN FLAG  -  {span}  -  {len(items)} stop{'s' if len(items) != 1 else ''}"
                items.sort(key=lambda ke: ke[1].time)
                color = ACCENT
            rows.append(Row(("head", caution, n), [""] * len(cols), fg=color, banner=title))
            for k, e in items:
                g = (e.pos_in - e.pos_out) if e.pos_in and e.pos_out else None
                t = self._stop_time(e)
                cells = [str(e.lap), str(e.number), e.name, str(e.stop_no), e.code, e.reason,
                         str(e.pos_in or ""), str(e.pos_out or ""), f"{g:+d}" if g is not None else "",
                         f"{t:.1f}" if t else "", self._damage_text(e), e.note]
                colors = [None] * len(cells)
                colors[8] = self._stop_color(e)
                rows.append(Row(("stop", k), cells, colors))
        self.pit_table.set_rows(rows)

    def _render_pit_stops(self, new_view: bool):
        cols = [Column("lap", "Lap", 44, "e"), Column("num", "Car", 44, "e"), Column("name", "Driver", 150, "w"),
                Column("stop", "Stop", 40, "e"), Column("svc", "Service", 64, "w", STOP_TIP),
                Column("reason", "Reason", 74, "w"), Column("flag", "Flag", 64, "w"),
                Column("pin", "Pos in", 52, "e"), Column("pout", "Pos out", 58, "e"),
                Column("gain", "+/-", 40, "e"),
                Column("box", "Service (s)", 74, "e", "Time the car sat stationary in its stall, game seconds"),
                Column("total", "Pit road (s)", 80, "e", "Pit road entry to exit, game seconds"),
                Column("dmg", "Damage", 96, "w", "Damage when the stop started -> when the car rejoined"),
                Column("note", "Why", 260, "w")]
        if [c.id for c in cols] != [c.id for c in self.pit_table.columns]:
            self.pit_table.set_columns(cols)
            self.pit_table.set_widths(self.settings.get("pit_widths", {}))
        if new_view or not self.pit_table.sort_col:
            self.pit_table.sort_col, self.pit_table.sort_rev = "lap", True
        self.pit_table.sortable = True
        rows = []
        for k, e in enumerate(self.pit_events):
            g = (e.pos_in - e.pos_out) if e.pos_in and e.pos_out else None
            t = self._stop_time(e)
            cells = [str(e.lap), str(e.number), e.name, str(e.stop_no), e.code, e.reason,
                     "GREEN" if e.green else "CAUTION", str(e.pos_in or ""), str(e.pos_out or ""),
                     f"{g:+d}" if g is not None else "", f"{t:.1f}" if t else "",
                     f"{e.road_time:.1f}" if e.road_time > 0.5 else "", self._damage_text(e), e.note]
            colors = [None] * len(cells)
            colors[9] = self._stop_color(e)
            rows.append(Row(("stop", k), cells, colors, sort={"lap": e.lap + k / 10000}))
        self.pit_table.set_rows(rows)

    def _pit_hover(self, key, col):
        if key[0] == "car" and col.startswith("s") and col[1:].isdigit():
            evs = [e for e in self.pit_events if e.idx == key[1]]
            k = int(col[1:]) - 1
            return self._stop_details(evs[k]) if k < len(evs) else None
        if key[0] == "stop" and col in ("svc", "note", "gain", "dmg"):
            return self._stop_details(self.pit_events[key[1]])
        return None

    def _stop_details(self, e: PitEvent) -> str:
        names = {"L/R": "four tires", "L": "left side", "R": "right side"}
        tires = names.get(e.code.split("+")[-1], "no tires")
        lines = [f"#{e.number} {e.name} - stop {e.stop_no}, lap {e.lap}",
                 f"Service: {e.code}  ({'repairs + ' if e.damage else ''}"
                 f"{'fuel + ' if e.fuel else 'no fuel, '}{tires})",
                 f"Reason: {e.reason} under {'green' if e.green else 'caution'}" + (f" - {e.note}" if e.note else "")]
        if e.pos_pending:
            lines.append(f"Position: P{e.pos_in or '?'} -> known at the restart (caution pit cycle)")
        elif e.pos_out is None:
            lines.append(f"Position: P{e.pos_in or '?'} -> in the pits now")
        else:
            g = (e.pos_in - e.pos_out) if e.pos_in and e.pos_out else 0
            lines.append(f"Position: P{e.pos_in or '?'} -> P{e.pos_out}  ({g:+d})")
        t = self._stop_time(e)
        if t:
            lines.append(f"Service time (stationary in the stall): {t:.1f} s")
        if e.road_time > 0.5:
            lines.append(f"Pit road, entry to exit: {e.road_time:.1f} s")
        if e.dmg_in is not None and (e.damage or (e.dmg_out is not None and e.dmg_out != e.dmg_in)):
            lines.append(f"Damage: {self._damage_text(e)}")
        return "\n".join(lines)

    @staticmethod
    def _damage_text(e: PitEvent) -> str:
        if e.dmg_in is None or not (e.damage or (e.dmg_out is not None and abs(e.dmg_out - e.dmg_in) > 1e-4)):
            return ""
        out = f"{e.dmg_out:.3f}" if e.dmg_out is not None else "..."
        return f"{e.dmg_in:.3f} -> {out}"

    def _drain_log(self):
        lines = []
        while True:
            try:
                lines.append(self.log_q.get_nowait())
            except queue.Empty:
                break
        if lines:
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", "\n".join(lines) + "\n")
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")

    def on_close(self):
        self._closing = True                          # stops the pit guard thread
        self._keep_out = frozenset()
        self._restore_chassis()
        self._behavior_restore()
        if self.game.pm is not None and self._crew_applied is not None and self._crew_applied[1]:
            self.pitcrew.apply(self.game.pm, False)          # stock crew back (the change is memory only)
        if self.game.pm is not None:
            if self.var_restore.get():
                self.pitgate.remove(self.game.pm)            # stock routine back
            else:
                self.pitgate.disarm(self.game.pm)            # left in place, open (stock behavior)
        self.engine.reset()
        self.radio.stop()
        self.talk_ind.close()
        self.tire_model.save()
        self.board_server.stop()
        self.game.detach(restore_patches=self.var_restore.get())
        self.pitlog.flush()
        self.feed.stop()
        self._save_settings()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    # one AIMOD at a time: two would fight over the game (and closing one undoes the pit crew / patches)
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        main._mutex = k32.CreateMutexW(None, False, "Local\\AIMOD_single_instance")
        if k32.GetLastError() == 183:            # ERROR_ALREADY_EXISTS
            ctypes.windll.user32.MessageBoxW(None, "AIMOD is already running - use the open window.", "AIMOD",
                                             0x40)
            return
    App().run()
