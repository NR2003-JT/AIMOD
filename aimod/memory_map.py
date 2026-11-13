"""NR2003.exe memory map, recovered from AIMOD v1.0.4 (estrategyaiV1.0.4).

All absolute addresses assume the image base 0x400000 used by every NR2003.exe
build checked so far. Per-car data lives in a block of CAR_STRIDE bytes; the
offsets below are relative to the start of that block.
"""

# Race-wide values
FLAG_STATE = 0x76F184          # int   - 7 = green
SESSION_STATE = 0x977A00       # byte  - 7 = race
# The game's own clock (use this): int ticks at 0xBEBDF5 (+1 per tick) / tick rate (36 if [0x755A18] != 0, else
# [0x7559B4]). The line crossings are stamped from it (0x53B5D0 -> 0x509A20); checked live 2026-10-21 (within one tick).
# It follows time acceleration and stops when paused; SESSION_TIME below froze for whole practice sessions.
GAME_TICKS = 0xBEBDF5          # int
TICK_RATE_FLAG = 0x755A18      # int: != 0 -> 36 ticks per second
TICK_RATE_VALUE = 0x7559B4     # int: ticks per second otherwise
SESSION_TIME = 0x977A04        # double - session clock, game seconds. CAN FREEZE for minutes in a race (seen
                               # 2026-10-14); strategy.advance_clock falls back to real time
TANK_CAPACITY = 0xBE0E38       # double - tank size in laps (v1 had this disabled)
LAPS_CURRENT = 0xBE0E48        # double
LAPS_TOTAL_RVA = 0x9ACEE8      # int, relative to NR2003.exe module base

# Loaded session: track folder name (char[32]) and, 0x20 later, the mod's series folder name.
# Same text also sits at 0xBEBD38 and 0x8C6080 (fallbacks).
TRACK_NAME_ADDRS = (0x977938, 0xBEBD38, 0x8C6080)
SERIES_NAME_OFFSET = 0x20

# Penalty rules (found while building bc_wave): manager at [0xBDB9D8]; +4 array of rule objects,
# +8 count. Each rule keeps a dword per car at rule+4+car*4; bit 16 = black flag shown.
PENALTY_MGR = 0xBDB9D8
PENALTY_BLACK_BIT = 1 << 16

# Each rule's "wants this car to pit" check (rule vtable +0x30), as NR2003's green pit check 0x534F40 calls it:
# function -> 0 = the car's state & 0xF is non-zero, n = it equals n. Rules whose check always says no are left out.
RULE_CHECKS = {0x422880: 0, 0x4228E0: 0, 0x422930: 0, 0x422980: 0, 0x4229D0: 0, 0x422B00: 0, 0x422B40: 0,
               0x422B80: 0, 0x422BC0: 0, 0x422D30: 0, 0x422A20: 3, 0x422C70: 3, 0x422C00: 5}

FLAG_GREEN = 7
SESSION_RACE = 7

# Per-car blocks: 44 slots (player, pace car, 42 more cars). v1 scanned 43, so
# whoever sat in slot 43 was never controlled.
NUM_CARS = 44
PLAYER_SLOT = 0
PACE_SLOT = 1
CAR_STRIDE = 8724
CAR_BLOCK_0 = 11348818 - CAR_STRIDE * 2 - 7418   # = 0xACCA30

# Found by AIMOD v2 (Oct 2026) by matching memory against the companion app's feed.
# Slot index == NR2003 car index (slot 0 = player, slot 1 = pace car).
OFF_LAPS_DONE = 1360           # int   - laps completed by this car (also at +7394)
OFF_LAP_NUM = 6778             # int   - lap the car is on (= laps completed + 1)
OFF_LAP_DIST = 8668            # float - metres from the start/finish line on this lap
OFF_POS_X = 6622               # float - world position, metres (Y at +6626)
OFF_VEL_X = 6634               # float - velocity, m/s (Y at +6638, Z at +6642)
# Fuel: NR2003 keeps it obfuscated - two pointers at car+0x518 / +0x520 to 8-byte blocks; the fuel
# (a double, game units ~ kg; x0.3524 = gallons) is block A XOR block B (from the AIPIT debug print,
# 0x404517 -> 0x409290). Capacity: float at [car+0x58A]+0x24.
# Scoring: NR2003's crossing table. Header at SCORING_HDR: +4 laps per row, +8 row shift (row = 1 << shift
# dwords), +0xC pointer to the table. Entry [slot][n] = game ms (low 28 bits) when the car completed lap n.
SCORING_HDR = 0x77F952
CROSS_MASK = 0x0FFFFFFF
OFF_FUEL_A = 0x518
OFF_FUEL_B = 0x520
OFF_FUEL_CAP_PTR = 0x58A
FUEL_CAP_OFF = 0x24
OFF_PIT_TIMER = 1734           # float - NR2003's own pit stop timer: resets and counts while the car is serviced
# Race control block (found while building bc_wave): one byte per car, 1 from pit road entry to pit exit.
ON_PIT_ROAD = 0x76B230 + 0x8DC
# Pit road state word (game's getter checks in this order): 0x10 closed, 0x8 open to all, 0x100 lead lap only.
# Don't write it: changing it delays the yellow lights and the pace car's lights.
PIT_STATE = 0x76B2F9
OFF_MAKE = 1426                # byte  - manufacturer index from the series (also at +4630, +4841)
# Index order in the 2012 Cup mod checked (alphabetical). The companion app labels these
# differently (1 = Chevrolet, 3 = Dodge, 0 = Unknown), which doesn't match the cars.
MAKE_NAMES = ["Chevrolet", "Dodge", "Ford", "Toyota"]

OFF_DAMAGE_BASE = 4903
OFF_DAMAGE_REAR = OFF_DAMAGE_BASE + 16
OFF_DAMAGE_FRONT = OFF_DAMAGE_BASE + 20
OFF_DAMAGE_RIGHT = OFF_DAMAGE_BASE + 48
OFF_DAMAGE_LEFT = OFF_DAMAGE_BASE + 52
OFF_REASON_OUT = 4916          # byte  - >0 means the car is out of the race
OFF_STRATEGY = 6046            # float - driver strategy attribute, rating = x*50
OFF_WEAR_LF = 7342             # float
OFF_WEAR_RF = 7346
OFF_WEAR_LR = 7350
OFF_WEAR_RR = 7354
OFF_GRIP = 7418                # float - valid range (0.01, 1.05]
# Each car's driver object (car + 0x16BE). 0x4F6E90 turns the driver's and car's ratings into the driver's own
# multipliers: aero drag +0x12C, engine power +0x130, traction circle (cornering grip) +0x134, chassis grip +0x138.
# The game rebuilds them when the car starts a new lap (0x52CC86 -> 0x52D340 -> 0x4F71B0, also with ASC off) and on
# ASC updates; writes hold until then. Live 2026-10-25: traction x0.97 = 0.2-0.5 s a lap slower on a 28 s lap.
OFF_DRIVER = 0x16BE
DRV_DRAG = 0x12C
DRV_POWER = 0x130
DRV_TRACTION = 0x134
DRIVER_MULT_CHECK = (0x4F6FDD, bytes([0xD9, 0x99, 0x34, 0x01, 0x00, 0x00]))   # fstp [ecx+0x134] in 0x4F6E90
# Driver aggression (.car rating k = 0 of the triplets at driver +0x30: mean, deviation, working value). The working
# value is read only by the car-next-to-car code (0x530110 / classifier 0x53244A); the game rolls it once at the session
# start (mean + a draw from the deviation) and never writes it in the race (live 2026-11-03/05, 42/42 cars, writes held).
# Side padding: the car-versus-car code (0x52BCFC) rebuilds car+0x1BEE on every AI update as car+0x1BEA + a term for the
# cars around; car+0x1BEA is a per-car base (1.0 normally; 0.7 / 1.0 / 1.5 by the game's mode, which resets it). Scaling
# it per car changes only that car's side room (live 2026-11-13: x0.7 on a pair = close side by side, no wrecks).
OFF_SIDE_BASE = 0x1BEA
OFF_AGG_MEAN = OFF_DRIVER + 0x30           # car + 0x16EE
OFF_AGG_WORK = OFF_DRIVER + 0x38           # car + 0x16F6
AGG_CODE_CHECK = (0x5302A2, bytes([0xD9, 0x86, 0xF6, 0x16, 0x00, 0x00]))   # fld [esi+0x16F6] in 0x530110
OFF_PITTING = 8118             # byte  - 1 = pit requested / in pit sequence
# The game's own reasons for a stop (its "pit now?" check, 0x40B5D0): +8119 = stop to serve a penalty;
# +4943 = a car condition the game pits for when it reaches its limit (damage / mechanical).
OFF_GAME_PENALTY_STOP = 8119
OFF_GAME_CONDITION = 4943
# The game's own repair need (int, 0x1B33): 0x52FAD0 rates the car's damage model each frame - 2 = must pit,
# 1 = should pit. With it above 0 the AI switches itself into its repair mode (object 0x760D78) and drives in
# WITHOUT a pit request (+8118 stays 0): the "came in without a request" stops. Seen with PitProbe, 2026-10-20.
OFF_GAME_REPAIR = 0x1B33
# The AI's current mode (pointer to one of the game's mode objects). Once a car is in a pit mode the stop goes ahead
# whatever the request says (PitProbe: request cleared at one to go, the car still came in).
OFF_AI_MODE = 0x13A7
PIT_MODES = frozenset((0x760F48, 0x760D94, 0x760F78, 0x760DF0, 0x760F3C,     # pit approach (set the request)
                       0x760D78,                                             # the game's repair stop
                       0x760F54, 0x760F90))                                  # pit lane (caution / green)

# Pit request, a sub-struct at +1567. Layout read from NR2003.exe's own code (the
# "AIPIT IN" debug print and the setters at 0x5234B0 / 0x523520 / 0x523590):
#   +210 left side wanted, +211 right side wanted (absolute sides)
#   +212/+213 the same two flags as the pit crew uses them, swapped by the
#   per-car flag at +254 (1: +212 = left, +213 = right; 0: the other way round).
#   The crew copies +212..+214 when the car enters pit road.
#   +214 (int) fuel: 0 none, 1 splash, 2 'h', 3 one can, 4 'H', 5 two cans (full).
# v1 wrote +212 as "right" and +213 as "left" regardless of +254, so its sides
# were reversed wherever +254 = 1 (e.g. Charlotte).
OFF_PIT = 1567
OFF_REQ_LEFT = OFF_PIT + 210
OFF_REQ_RIGHT = OFF_PIT + 211
OFF_CREW_A = OFF_PIT + 212     # left when the side flag is 1, else right
OFF_CREW_B = OFF_PIT + 213     # right when the side flag is 1, else left
OFF_FUEL_REQ = OFF_PIT + 214
OFF_SIDE_FLAG = OFF_PIT + 254
# +286 (car +0x73D) byte: the crew repairs the damage this stop - the 'D' in the game's own "AIPIT IN %d: %d, %s%s%s"
# debug line (tires, fuel, D). The game sets it as a damaged car enters pit road under green and CLEARS it as it enters
# under caution (probe 2026-11-02, NR2003_clean.exe too), so caution stops never repaired anything and the game kept
# calling damaged cars back in. Set again after pit entry, a caution stop repairs (#31: 0.865 -> 0.451 with 4 tires
# and fuel). The crew clears it when the repair is done.
OFF_REPAIR_FLAG = OFF_PIT + 286
# The AI's pit service planner 0x40B740 (reached through a pointer table) rewrites the whole request - tires, fuel and
# the repair flag - from the car's stop plan: the repair flag from the byte at car +0x1DD3 (read nowhere else in the
# exe). With it 0 the planner wiped AIMOD's flag again within a frame (Bristol 11-02, new18: 14 of 20 damaged caution
# stops not repaired, the flag flipping 1 -> 0 every 30 ms on pit road). AIMOD sets both.
OFF_PLAN_REPAIR = 0x1DD3
FUEL_FULL = 5

# Driver entries (names / car numbers)
DRIVER_ENTRY_0 = 14711636
DRIVER_ENTRY_SIZE = 175
DE_OFF_FIRST = 0               # 16-byte string
DE_OFF_LAST = 16               # 20-byte string
DE_OFF_CARNUM = 156            # int

# Code patches v1 applies to take over AI pit decisions: 2-byte conditional
# jumps replaced with NOPs. `original` is checked before patching so an unknown
# NR2003.exe build is never written to.
PATCHES = {
    # NR2003's per-car "pit now?" check (0x40B5D0) sets the pit request (+8118) when any of:
    #   1. the game's own fuel/tire stop plan (built from papy_ai.ini [ strategy ]) is due within a lap,
    #   2. damage >= 0.5, 3. fuel under 10%, 4. a penalty to serve.
    # Calling the request off doesn't stick against (1): the check sets it again on the next frame and
    # the car comes in anyway. This makes (1) always skip (jne -> jmp, the same path as "no plan"),
    # leaving the damage, fuel-emergency and penalty stops to the game and every planned stop to AIMOD.
    "no_game_plan_stops": {"addr": 0x40B63F, "original": bytes([0x75, 0x2B]), "patched": bytes([0xEB, 0x2B]),
                           "optional": True,
                           "label": "Stop the game's own planned pit stops (0x40B63F): only AIMOD calls fuel/tire "
                                    "stops; the game still pits for heavy damage, an empty tank and penalties"},
    # Reason 2 above: the call at 0x40B670 (0x52E740) asks a car in while its damage block +0x28 (car +4943, AIMOD's
    # "car condition") is between 0.5 and 1.0. A stall stop never changes that value, so the game asked again every
    # lap for the rest of the race (#33 at Bristol 2026-11-02: 126 stops, ~1.5 s each, "car condition 0.99").
    # je -> jmp: that reason never sets the request. Damage the game really repairs (repair need +0x1B33) and
    # AIMOD's own damage threshold still bring cars in.
    "no_game_condition_stops": {"addr": 0x40B677, "original": bytes([0x74, 0x12]), "patched": bytes([0xEB, 0x12]),
                                "optional": True,
                                "label": "Stop the game's 'car condition' pit stops (0x40B677): the game asked a car "
                                         "in every lap for a condition its pit stops never fix; repair damage "
                                         "and AIMOD's damage stops are unchanged"},
    # The AI's pit-entry routine 0x404A70 cancels the crew's damage repair under caution: if the race state isn't green
    # (0x417FC0 != 7) and the repair flag (car +0x73D) is set, it works out the time to the pace car and calls
    # 0x5237B0(0) - "AIPIT HALT %d: Halting damage repair due to pace car" in its debug text. Live it fired on every
    # caution stop (0 of ~115 repaired, same in the untouched 2003 exe), and the game's own pit check then called the
    # still-damaged car back in, stop after stop. test al,al -> xor al,al at 0x404AE5: the je after it always skips
    # the halt, so a caution stop repairs like a green one.
    "caution_repairs": {"addr": 0x404AE5, "original": bytes([0x84, 0xC0]), "patched": bytes([0x32, 0xC0]),
                        "optional": True,
                        "label": "Repair damage on caution stops (0x404AE5): NR2003 cancelled the crew's repair "
                                 "under yellow ('halting damage repair due to pace car'), so damaged cars kept "
                                 "coming back in"},
}
NOP2 = bytes([0x90, 0x90])
