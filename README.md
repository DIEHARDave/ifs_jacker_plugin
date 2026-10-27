# KILN_IFSJacker

[IFS Jacker](https://github.com/ninjamida/ifs-jacker) support for the
Flashforge AD5X on [Kiln OS](https://github.com/djvalic/kiln-os): two to four
IFS units on the printer's one IFS port, as channels 1-16.

The IFS Jacker is ninjamida's RP2040 board (build guide and case on
[Printables](https://www.printables.com/model/1644745-ifs-jacker)). It sits
between the printer and the IFS units and looks to the printer like one IFS
with 8, 12 or 16 channels. On Z-Mod it comes with
[ifs_jacker_plugin](https://github.com/ninjamida/ifs_jacker_plugin); this is
the Kiln OS counterpart, written from the protocol (no code from either).
It lives on the `kiln` branch of a fork of that repository; `master` is
ninjamida's Z-Mod plugin, unchanged.

> **Status: tested against an emulator only, not on a real Jacker.** It also
> needs a Kiln OS whose `ifs.py` drives more than 4 channels, which is not in
> a Kiln release yet (see [Kiln OS part](#kiln-os-part)).

## What it does

Kiln's own IFS module drives the channels: with `[ifs] channels: auto` (the
default in that change) it reads the `channel_count` the Jacker adds to its
status and offers slots 1-16, slicer tools `T0`-`T15`, materials, tool map,
auto-refill and pre-feed for all of them. This plugin adds the Jacker's own
parts:

- **Detection**: `Z2` once the IFS link reports a `channel_count` (a plain
  IFS never does, so it is never sent `Z2`; the Z-Mod plugin's "plain IFS
  disconnects three times at start-up" doesn't happen here). Firmware
  version, unit and channel count in `printer.kiln_ifsjacker`.
- **Peripherals** (Jacker firmware 3.0+): names (`Z3`), values from every
  status reply (`p<n>_<field>`) and `Z4`, commands (`Z5`).
- **`sensor_type: ifs_jacker`**: a Jacker temperature peripheral as a Klipper
  `temperature_sensor` (Mainsail, Fluidd, HelixScreen show it).
- **`[kiln_ifsjacker_fan name]`**: a Jacker PWM output as a fan,
  `SET_FAN_SPEED FAN=name SPEED=0.5`.

| Command | |
|---|---|
| `IFS_JACKER_CHECK` | Detect the Jacker again |
| `IFS_JACKER_STATUS` | Its `Z2` reply (version, units, channels, request and timeout counters) |
| `IFS_JACKER_PERIPHERALS` | Each peripheral's name and values |
| `IFS_JACKER_PERIPHERAL PERIPHERAL= COMMAND= [PARAM1=] [PARAM2=]` | Raw `Z5` to one peripheral |

`printer.kiln_ifsjacker`: `present`, `version`, `units`, `channel_count`,
`peripherals` (per index: `name` and its fields), `message`.

## Install

```
cd /data/ad5x/plugins
git clone -b kiln https://github.com/DIEHARDave/ifs_jacker_plugin ifsjacker
```

then `ENABLE_PLUGIN NAME=ifsjacker`. Peripherals go in `local.cfg`
(`plugin.cfg` has examples).

The Jacker side is set up as its README says (one RS485 converter per IFS;
`include_channel_count_in_status=true` in its `config.ini`, the default). If
that is off, set `[ifs] channels: 8` (or 12, 16) in `local.cfg`.

## Kiln OS part

The channel handling lives in Kiln's `klipper/extras/ifs.py`, branch
[`ifs-n-channels`](https://github.com/DIEHARDave/kiln-os/tree/ifs-n-channels)
of my fork (based on v0.0.26), meant as a pull request to Kiln OS:

- `[ifs] channels: auto | 1-16`; per-slot state for 16 slots; status lists,
  `tool_map`, `ifs_materials` and the `IFS_*` range checks follow the count.
- `T4`-`T15` registered by the module (unless a macro of that name exists);
  a tool beyond the count is refused.
- With a Jacker, `L`/`S` of IFS moves go as whole numbers: Jacker 3.2 reads
  them with `int()` and silently drops both when one has a decimal point
  (tuned lengths such as `152.3` would otherwise become the unit's default).
- `activity_channel` put on the right unit: the Jacker shifts only some
  states (7, 11, 12, 15) by 44 per unit, so a load on unit 2 channel 2
  arrives as 21 ("channel 2 loading"), which is decoded with the unit of
  the selected channel.
- F21's four counters stored at the active unit's slots.
- A stock IFS behaves as before: 4 channels, decimal lengths, `T4` refused.

## Known limits

- **HelixScreen** shows 4 IFS lanes: its AD5X IFS backend has a fixed
  `kNumPorts` of 4. Slots 5-16 work from the slicer (`T4`+), the console,
  Mainsail and Fluidd, but not on the touch screen's lane view.
- **KILN_AFCLite** and **KILN_AutoColor** assume 4 slots too.
- Commands without a channel (`F15` reset, `F112` stop, `F42` current) reach
  only the unit used last (Jacker behaviour): `IFS_SET_CURRENT` sets one
  unit's motors.
- No `IFS Jacker LED` support yet (the Z-Mod plugin's `ifs_jacker_led`).

## Emulator

`emulator/` runs the real IFS Jacker firmware core on a PC with simulated
IFS units, so Kiln can be tested against it without the hardware:

- `fake_ifs.py`: one simulated IFS (commands and states from Kiln's
  `docs/ifs-protocol.md`), on a pty by itself or behind the Jacker.
- `jacker_emu.py`: loads `core.py` and the peripheral code from a checkout
  of [ifs-jacker](https://github.com/ninjamida/ifs-jacker) (not copied here:
  it is under the PolyForm Noncommercial license) and swaps only the
  hardware: the printer UART becomes a pty, each IFS UART a `FakeIfs`. A
  control port puts spools in or takes them out during a run.
- `p_emu_temp.py`, `p_emu_pwm.py`: stand-in peripherals with the reply
  formats of the Jacker's thermistor and PWM output.
- `klipper_test/`: Klipper (Kiln's pinned commit, batch mode against a
  linux-process MCU dictionary) with Kiln's `ifs.py` and this plugin, driven
  through `kiln_test_probe.py`, a test-only module that stands in for the
  API socket batch mode doesn't have.

```
git clone -b kiln https://github.com/DIEHARDave/ifs_jacker_plugin ~/ifs_jacker_plugin
git clone https://github.com/ninjamida/ifs-jacker ~/ifs-jacker
git clone https://github.com/Klipper3d/klipper ~/kiln-klipper
cd ~/kiln-klipper && git checkout ee2c6fb6b00b2b5d097ef70d0868df7d74943b51
echo CONFIG_MACH_LINUX=y > .config && make olddefconfig && make
cd ~/ifs_jacker_plugin/emulator/klipper_test
python3 test_kiln_jacker.py --klipper ~/kiln-klipper --kiln ~/kiln-os \
    --jacker-src ~/ifs-jacker/src --python ~/klippy-env/bin/python
```

Linux (or WSL) only. Last run, 2026-10-26, Jacker firmware 3.2.1: 39 checks,
0 failed:

| Setup | Checked |
|---|---|
| 2 units, 8 channels | channel count from F13; T0-T7; `T5` loads channel 6 as unit 2 channel 2 on the wire (F24, F10, F112, push, F39, F23) and the Jacker parks unit 1 (F18); a 6 -> 7 swap reports `unloading` 6 and `loading` 7; tool map across units; `T8`, `TOOL=8`, `SLOT=9` refused; `L152.3 S600.5` arrives as `L152 S600`; 8 slot materials saved; a new spool on channel 8 is pre-fed; `IFS_EJECT SLOT=8`; temperature sensor and fan peripherals; auto-refill in a print from slot 2 (unit 1) to slot 6 (unit 2) |
| 4 units, 16 channels | 16 channels; `T15` loads channel 16 as unit 4 channel 4; `loading` 16; no `T16` |
| One plain IFS | 4 channels, `T4` refused, decimal lengths unchanged, no `Z2` ever sent, plugin not present, swaps work |
| Jacker without `channel_count` | `channels: auto` gives 4; `channels: 8` gives 8 with whole-number lengths |

The emulator has no timing faults, noise or real filament: it shows the
protocol and Kiln's logic are right, not that a real Jacker build feeds
reliably.

## License

GPLv3 (`LICENSE`). The IFS Jacker firmware the emulator loads is not part of
this repository and keeps its own license.
