# AIMOD

AI pit strategy for **NASCAR Racing 2003 Season**. AIMOD takes over the AI cars' pit decisions while
you race or watch: when each car pits, what it takes (fuel amount, two or four tires, repairs), and how
teams react to each other, to cautions and to stage breaks. It also shows a broadcast-style pit board over
the game and can voice every car's crew chief and spotter as a race scanner.

AIMOD v2 is a rewrite of **AIMOD v1.0.4** by Adolfo Delgado. It keeps v1's memory map and reads the same
presets (v2 presets keep the v1 keys, so they still open in v1.0.4).

Download the latest release from the [Releases](../../releases) page. The README inside the zip is the
user guide (every setting is explained there and in the app's tooltips).

## What it does

- **Pit strategy per car.** Green-flag stops planned from each car's real fuel mileage and tire wear;
  short-pit / split / stretch stints; undercut and overcut reactions; team and manufacturer plans
  (including superspeedway blocks).
- **Cautions.** Every caution call is AIMOD's: fuel, tires against track position, laps to go, the free
  pass and wave around, stage breaks, overtime (green-white-checkered). The game's own stop requests
  are called off when the car doesn't need the stop; penalties, heavy damage and an empty tank still
  go ahead.
- **Every trip down pit road is accounted for.** Stops the game commits to on its own are taken over or
  sent through without service, and logged with their real cause.
- **Crew chief adjustments.** At a stop an AI crew chief may adjust wedge, track bar, air pressure or
  grille tape; a good call improves the car's handling, a misread makes it worse (crew rating from the
  `.car` file).
- **Pit crew and gearbox options** (memory only, game files are never changed): modern one-lug pit
  crews with faster fuel flow, and the game's own 5/6-speed gearbox for mods that want it.
- **Race scanner.** Spoken team radio for one car or the whole field, with an on-screen card for the car
  that's talking; custom lines; optional phone playback.
- **Pit board and logs.** In-game pit board (windowed/borderless NR2003), a web/OBS widget, a CSV pit log
  per session and `aimod.log`.
- **Works alone.** RaceControl (cautions, wave arounds, stages) and a companion overlay app are optional;
  AIMOD reads their files when they are there.

## Requirements

- Windows 10/11 and NASCAR Racing 2003 Season (the NR2003.exe build v1.0.4 supported).
- AIMOD runs as administrator, because NR2003 normally does and AIMOD reads and writes its memory.
- Before patching any code, AIMOD checks the original bytes; an unknown exe build switches it to read-only.

## Building from source

Python 3.12 on Windows:

```
pip install -r requirements.txt
build.cmd
```

`build.cmd` runs the tests, then builds `build\dist\AIMOD.exe` with `build\release\AIMOD.spec` (the spec
carries the hidden imports the scanner needs, so build from it rather than from the PyInstaller command
line). To run from source: `python main.py` from an administrator prompt.

Car number art and voices are not in this repository; they come with the release zip (or your own files).

## Tests

```
python -m unittest discover -s tests
python tools/sim_scenarios.py all
```

About 250 unit tests cover the strategy engine, the game-memory layer (against fake processes), presets,
the radio and the setup readers. `tests/racesim.py` is a whole-race simulator (fuel, tire falloff, pit
road, cautions, stages) used to check full-race outcomes: stops per car, laps down, fuel left at the
finish. Tests that read an installed NR2003 skip themselves when it isn't there.

## Layout

| Path | What's in it |
|---|---|
| `aimod/strategy.py` | The strategy engine: one tick per game update, decisions and memory writes per car |
| `aimod/game.py`, `aimod/memory_map.py` | Reading and writing NR2003's memory; every address with how it was found |
| `aimod/app.py` | The Tk app: settings, monitor, pit log, patches |
| `aimod/radio*.py`, `aimod/helmet.py` | Race scanner, voices and the on-screen card |
| `aimod/adjust.py`, `aimod/aisetup.py` | Crew chief chassis adjustments from the track's AI setup |
| `aimod/pitcrew.py`, `aimod/gearbox.py` | Pit crew and gearbox options (memory only) |
| `aimod/tiremodel.py`, `aimod/pace.py` | Learned tire falloff and pace per track |
| `aimod/carfiles.py`, `aimod/carno.py` | `.car` files (teams, crew ratings) and car numbers ("01" is stored as 2001) |
| `tests/` | Unit tests and the race simulator |

## How it was built

AIMOD v2 was written with AI assistance (Claude), working from Adolfo Delgado's v1.0.4 and from live
NR2003 sessions. Memory addresses were found by probing the running game and are documented where they
are defined. Every feature was then checked in live races and by testers, and bug reports were turned into
regression tests (the test names say which race or report they come from).

## Credits

- **Adolfo Delgado**: original AIMOD (v1.0.4): memory map, pit takeover and strategy logic.
- **Jonathon Thomas**: AIMOD v2.

## License

MIT, see [LICENSE](LICENSE).
