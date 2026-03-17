# MC9S12 Voltage Glitch Flash Dump

Voltage glitching toolchain to bypass flash readout security on Freescale/NXP MC9S12D64 (and similar HCS12) microcontrollers. Uses a Teensy 4.1 as the glitch generator, a 74HC4053 analog MUX for voltage switching, a USBDM adapter for BDM protocol communication, and a Keysight E3631A power supply for automated voltage sweeping.

## How It Works

The MC9S12 evaluates flash security during the first 150 bus clocks after reset. A precisely timed voltage glitch on the core supply during this window can cause the security check to fail, leaving the BDM interface unsecured. Once unsecured, the full flash contents can be read out via BDM.

The attack has two phases:
1. **BKGD characterization**: Fast sweep (~5 min) to find the voltage/pulse-width sweet spot for each specific chip
2. **RESET-mode attack**: Targeted sweep at the sweet spot to glitch during boot security evaluation, detected via `Connect()` return code

## Hardware

### Components
| Component | Role | Notes |
|-----------|------|-------|
| Teensy 4.1 | Glitch timing generator | 600 MHz ARM, USB serial |
| 74HC4053 | Analog MUX | Switches between normal voltage and glitch (current sink) |
| USBDM | BDM adapter | BKGD/RESET/VDD/GND to target |
| Keysight E3631A | Programmable PSU | Ch1=core voltage, Ch2=clock voltage, NI GPIB |
| MC9S12D64 | Target MCU | Secured flash, 2 MHz bus clock |

### Wiring Diagram

```
                    Keysight E3631A (GPIB addr 5)
                    +---------------------------+
                    | Ch1(+) ---- VCORE_NORMAL  |  (1.55-1.70V, 10mA limit)
                    | Ch1(-) ---- GND           |
                    | Ch2(+) ---- VCLK          |  (2.0V)
                    | Ch2(-) ---- GND           |
                    +---------------------------+


    Teensy 4.1 (USB on COM11)              74HC4053 MUX
    +-------------------+                  +--------------------+
    | Pin 2  (OUTPUT) --|---- MUX_EN ----->| E   (pin 6)  ~Enable
    | Pin 3  (OUTPUT) --|---- MUX_CTRL --->| A,B,C (pin 11,10,9) Select
    | Pin 6  (INPUT)  --|---- BKGD         |
    | Pin 12 (INPUT)  --|---- RESET        |
    | GND             --|---- GND -------->| VEE,GND (pin 7,8)
    +-------------------+                  |
                                           | VCC  (pin 16) --- 5V
                                           |
                                           | Y0,Z0,X0 (pin 12,14,15) --- VCORE_NORMAL (from E3631A Ch1)
                                           | Y1,Z1,X1 (pin 13,2,3)   --- VCORE_GLITCH (current sink / GND)
                                           | Y,Z,X    (pin 1,4,5)    --- VCORE_TARGET (to MC9S12 core)
                                           +--------------------+

    MUX Logic:
      MUX_EN=HIGH, MUX_CTRL=HIGH  ->  Z0=Y0   (normal voltage to target)
      MUX_EN=HIGH, MUX_CTRL=LOW   ->  Z0=Y0b  (GND/current sink = glitch)
      MUX_EN=LOW                   ->  Z0=Hi-Z (disconnected)


    USBDM Adapter                          MC9S12D64 Target
    +-------------------+                  +------------------+
    | BKGD -------------|---- BKGD ------->| BKGD             |
    | RESET ------------|---- RESET ------>| RESET            |
    | VDD --------------|---- 5V --------->| VDD              |
    | GND --------------|---- GND -------->| GND              |
    +-------------------+                  +------------------+
                                           | VCORE <--------- Z0 (from MUX)
                                           | VCLK  <--------- Ch2 (from E3631A)
                                           | XTAL  <--------- 2 MHz crystal
                                           +------------------+

    Level Converter (unidirectional, 3.3V <-> 5V)
    +-------------------------------+
    | Teensy side (3.3V)    Target/USBDM side (5V)
    | Teensy Pin 6  ------->  BKGD line
    | Teensy Pin 12 ------->  RESET line
    +-------------------------------+
```

### Critical Notes

- **Level converter** between Teensy and USBDM/target is **unidirectional** (Teensy 3.3V → Target 5V). Both pin 6 (BKGD) and pin 12 (RESET) connect through it.
- **Core voltage** must be **separated** from PLL/clock voltage. The glitch only targets the core supply.
- **Crystal**: Must match the target's expected frequency (typically 2 MHz for MC9S12D64). Wrong crystal = scattered hits and very low success rate.
- **Clock caps**: Add stabilization capacitors on the crystal. Without them, the oscillator is unstable and glitch timing becomes unpredictable.
- **PSU current limit**: Set to **10mA** (0.01A) on the core voltage channel to prevent damage.
- **PSU recovery**: If the USBDM gets stuck (rc=33, "RESET pin timeout"), toggling the E3631A output off/on recovers it without physical unplug. Frequent rc=33 errors usually indicate an unstable clock — verify the crystal frequency matches the target spec and that stabilization caps are properly soldered.

## Software

### Prerequisites

- Python 3.10+
- [PlatformIO](https://platformio.org/) (for Teensy firmware upload)
- NI-VISA / NI-488.2 drivers (for GPIB communication)
- USBDM drivers + `usbdm.4.dll` (place in `scripts/` or system PATH)

```bash
pip install -r requirements.txt
```

### Teensy Firmware

Flash the Teensy with the BDM glitch firmware:

```bash
cd firmware
pio run -t upload
```

The firmware supports these serial commands:
- `S` — Status (show current settings, timing measurements)
- `D<ns>` — Set delay in nanoseconds (e.g., `D5000`)
- `W<ns>` — Set glitch width in nanoseconds (e.g., `W100`)
- `E<n>` — Set edge count target (default 24 for one BDM READ_WORD)
- `R` — Arm for RESET-mode glitch (fires on RESET rising edge)
- `B` — Arm for BKGD-mode glitch (fires after N BKGD edges)
- `X` — Disarm / stop
- `T` — Trigger timing measurement (calibrate bus clock)

### Step 1: Verify Wiring

```bash
cd scripts
python bdm_diag.py --teensy COM11
```

Checks that pin 6 can see BKGD edges and pin 12 can read the RESET line.

### Step 2: BKGD Voltage Characterization (~5 min)

```bash
python auto_bdm_sweep.py \
  --teensy COM11 --gpib 5 \
  --v-start 1.550 --v-end 1.700 --v-step 0.005 \
  --width-list 60,80,100,120,150 \
  --delay-start 0 --delay-end 80000 --delay-step 500 \
  --tries 3
```

This maps the chip's voltage sensitivity by corrupting BDM bus data during reads. It does **not** bypass security — it only identifies which voltage/width combinations affect the chip. Look for a voltage with a large cluster of hits.

### Step 3: RESET-mode Security Bypass + Dump

```bash
python auto_bdm_reset_sweep.py \
  --teensy COM11 --gpib 5 \
  --v-start 1.595 --v-end 1.605 --v-step 0.002 \
  --width-list 100 \
  --delay-start 2000 --delay-end 20000 --delay-step 200 \
  --tries 20
```

Focuses on the sweet spot found in Step 2. On a successful `Connect()` (rc=0 instead of rc=18), the script automatically:
1. Validates by reading multiple addresses
2. Dumps all flash pages including banked pages via PPAGE register
3. Saves to `firmware_YYYYMMDD_HHMMSS.s19`

### Flash Memory Map (MC9S12D64)

The MC9S12D64 has 64KB flash across 4 pages. Two pages are fixed-mapped, two require PPAGE register writes:

| Page | Address Window | PPAGE Value | Notes |
|------|---------------|-------------|-------|
| 0x3E | $4000-$7FFF | Fixed | Bootloader area |
| 0x3F | $C000-$FFFF | Fixed | Application + vectors |
| 0x3C | $8000-$BFFF | Write 0x3C to $0030 | Banked |
| 0x3D | $8000-$BFFF | Write 0x3D to $0030 | Banked |

Additionally, EEPROM is at $0400-$07FF (1KB).

## Chip-to-Chip Variation

**Every chip has a different optimal voltage.** Even chips from the same production batch can vary by 40-50mV. Always run Step 2 (BKGD characterization) on each new chip.

Known examples:
| Chip | Die Marking | Core Voltage | Width | Delay |
|------|-------------|-------------|-------|-------|
| Motorola MC9S12D64 | Motorola logo | 1.645V | 150ns | 12,000ns |
| NXP MC9S12D64 #1 | NXP logo | 1.598V | 100ns | 5,000ns |
| NXP MC9S12D64 #2 | NXP logo | 1.640V | 80ns | 11,600ns |

## USBDM Timing Options

These are critical for stability. Without them, the USBDM can lock up and require physical unplug:

```python
resetDuration         = 500   # ms — how long RESET is held low
resetReleaseInterval  = 300   # ms — wait after RESET release
resetRecoveryInterval = 800   # ms — total recovery before Connect()
autoReconnect         = NEVER # prevent unexpected BDM traffic
```

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| Zero BKGD hits at any voltage | Wrong crystal frequency | Match crystal to target spec (usually 2 MHz) |
| Scattered hits, no clear sweet spot | Unstable oscillator | Add clock stabilization capacitors |
| USBDM rc=33 (RESET timeout) | Target crashed hard | Toggle PSU output off/on, or unplug USBDM |
| USBDM rc=18 after glitch | Security not bypassed | Adjust voltage/width/delay, increase tries |
| Inconsistent dump checksums | Re-glitch during read failed | Run more dumps, compare checksums |

## License

This project is provided for educational and authorized security research purposes only.
