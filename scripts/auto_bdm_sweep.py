#!/usr/bin/env python3
"""
Automated BDM BKGD-mode voltage + width + delay sweep for MC9S12D64.
Glitches each ReadMemory individually (~5ms/attempt) — no boot security
bypass needed.

Controls:
  - Keysight E3631A via GPIB (core voltage)
  - Teensy 4.1 via serial (glitch timing in BKGD edge mode)
  - USBDM via DLL (BDM communication)

Usage:
    python -u auto_bdm_sweep.py [--teensy COM11] [--gpib 5]
"""

import serial
import time
import csv
import argparse
from datetime import datetime
from pymeasure.instruments.keysight import KeysightE3631A
from usbdm import USBDM, BDM_RC_OK, BDM_RC_BDM_EN_FAILED

# Safety limits
MAX_VOLTAGE = 2.500
MIN_VOLTAGE = 1.500  # target board operates as low as 1.545V
CLOCK_VOLTAGE = 2.000


def setup_supply(gpib_addr=5):
    supply = KeysightE3631A(f"GPIB::{gpib_addr}")
    supply.ch_1.current_limit = 0.01
    supply.ch_2.current_limit = 0.1
    supply.ch_2.voltage_setpoint = CLOCK_VOLTAGE
    supply.output_enabled = True
    time.sleep(0.5)
    print(f"Supply connected. Ch2 (clock) = {supply.ch_2.voltage:.3f}V")
    return supply


def set_core_voltage(supply, voltage):
    if voltage > MAX_VOLTAGE:
        raise ValueError(f"Voltage {voltage}V exceeds max {MAX_VOLTAGE}V!")
    if voltage < MIN_VOLTAGE:
        raise ValueError(f"Voltage {voltage}V below min {MIN_VOLTAGE}V!")
    supply.ch_1.voltage_setpoint = voltage
    time.sleep(0.3)
    actual = supply.ch_1.voltage
    print(f"  Core voltage set: {voltage:.3f}V, measured: {actual:.3f}V")
    return actual


def teensy_cmd(ser, cmd):
    """Send command to Teensy, read response."""
    ser.reset_input_buffer()
    ser.write(f"{cmd}\n".encode())
    ser.flush()
    time.sleep(0.02)
    resp = ""
    while ser.in_waiting:
        resp += ser.read(ser.in_waiting).decode(errors='replace')
        time.sleep(0.01)
    return resp.strip()


def teensy_drain(ser):
    """Drain all pending Teensy output."""
    time.sleep(0.05)
    while ser.in_waiting:
        ser.read(ser.in_waiting)
        time.sleep(0.01)


def arm_bkgd_continuous(ser, width_ns, delay_ns):
    """Arm Teensy in continuous BKGD edge mode."""
    teensy_cmd(ser, "X")           # disarm first
    teensy_cmd(ser, f"W{width_ns}")
    teensy_cmd(ser, f"D{delay_ns}")
    teensy_cmd(ser, "E24")         # 24 edges = one READ_WORD
    teensy_cmd(ser, "C")           # continuous mode (re-arms after each glitch)


def reset_and_connect(bdm, teensy, supply=None, voltage=None):
    """Reset target and establish BDM connection."""
    teensy_cmd(teensy, "X")  # disarm during reset
    try:
        bdm.target_reset()
    except Exception:
        # Hard recovery on reset failure
        hard_recover(bdm, teensy, supply, voltage)
        return
    time.sleep(0.05)
    bdm.connect()


def check_hit(val):
    """Check if a read value indicates successful security bypass."""
    return val != 0x0000 and val != 0xFFFF


def hard_recover(bdm, teensy, supply=None, voltage=None):
    """Hard recovery: power cycle target via PSU, then close/reopen USBDM."""
    teensy_cmd(teensy, "X")
    print("  Hard recovery: power cycling target + reopening USBDM...")

    # Power cycle target via PSU if available
    if supply is not None:
        try:
            supply.output_enabled = False
            time.sleep(1)
            supply.output_enabled = True
            if voltage is not None:
                supply.ch_1.voltage_setpoint = voltage
            time.sleep(1)
            print("  PSU power cycled")
        except Exception as e:
            print(f"  PSU power cycle failed: {e}")

    try:
        bdm.close()
    except:
        pass
    time.sleep(2)
    try:
        bdm.open()
        time.sleep(0.5)
        bdm.target_reset()
        time.sleep(0.1)
        bdm.connect()
        print("  Hard recovery OK")
        return True
    except Exception as e:
        print(f"  Hard recovery failed: {e}")
        return False


def recover(bdm, teensy, supply=None, voltage=None):
    """Recover from BDM errors: disarm, reset, reconnect."""
    teensy_cmd(teensy, "X")
    try:
        bdm.target_reset()
        time.sleep(0.1)
        bdm.connect()
        return True
    except Exception:
        # Soft reset failed, try hard recovery with PSU power cycle
        return hard_recover(bdm, teensy, supply, voltage)


def sweep_bkgd(bdm, teensy, writer, voltage_mv, width_ns,
               delay_start, delay_end, delay_step, tries, probe_addr,
               supply=None, voltage=None):
    """Sweep delay range in BKGD mode for a given width. Returns list of hits."""
    hits = []
    total_attempts = 0
    consecutive_errors = 0

    # Initial reset + connect
    reset_and_connect(bdm, teensy, supply, voltage)
    time.sleep(0.1)

    # Arm continuous BKGD mode
    arm_bkgd_continuous(teensy, width_ns, delay_start)

    num_steps = (delay_end - delay_start) // delay_step
    for step_idx, delay_ns in enumerate(range(delay_start, delay_end, delay_step)):
        # Update delay (Teensy accepts D command while armed in continuous mode)
        teensy_cmd(teensy, f"D{delay_ns}")

        for attempt in range(tries):
            total_attempts += 1

            try:
                data = bdm.read_memory(probe_addr, 2)
                val = (data[0] << 8) | data[1]
                consecutive_errors = 0

                if check_hit(val):
                    print(f"  *** HIT delay={delay_ns}ns width={width_ns}ns "
                          f"addr=0x{probe_addr:04X} val=0x{val:04X}")
                    hits.append((delay_ns, val))
                    writer.writerow([
                        datetime.now().isoformat(), voltage_mv, width_ns,
                        delay_ns, attempt, f"0x{probe_addr:04X}",
                        f"0x{val:04X}", "hit"
                    ])

            except Exception:
                consecutive_errors += 1
                if consecutive_errors >= 10:
                    print(f"  [{total_attempts}] {consecutive_errors} consecutive errors, recovering...")
                    if not recover(bdm, teensy, supply, voltage):
                        print("  Recovery failed, aborting this width")
                        return hits
                    consecutive_errors = 0
                    # Re-arm after recovery
                    arm_bkgd_continuous(teensy, width_ns, delay_ns)

        # Progress every 20 steps
        if step_idx % 20 == 0:
            print(f"  [{step_idx}/{num_steps}] delay={delay_ns}ns: "
                  f"{len(hits)} hits, {consecutive_errors} errs")

    # Drain teensy
    teensy_drain(teensy)
    return hits


def main():
    parser = argparse.ArgumentParser(description="BDM BKGD-mode voltage sweep")
    parser.add_argument("--teensy", default="COM11")
    parser.add_argument("--gpib", type=int, default=5)
    parser.add_argument("--v-start", type=float, default=1.635)
    parser.add_argument("--v-end", type=float, default=1.660)
    parser.add_argument("--v-step", type=float, default=0.005)
    parser.add_argument("--width-list", default="100,150,200,250",
                        help="Comma-separated glitch widths in ns")
    parser.add_argument("--delay-start", type=int, default=0,
                        help="Delay sweep start (ns)")
    parser.add_argument("--delay-end", type=int, default=100000,
                        help="Delay sweep end (ns)")
    parser.add_argument("--delay-step", type=int, default=500,
                        help="Delay step (ns)")
    parser.add_argument("--tries", type=int, default=3,
                        help="Attempts per delay step")
    parser.add_argument("--probe", type=lambda x: int(x, 0), default=0xFFFE,
                        help="Probe address (default 0xFFFE)")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    if args.output is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output = f"bdm_sweep_{ts}.csv"

    width_list = [int(w) for w in args.width_list.split(",")]

    # Safety
    if args.v_end > MAX_VOLTAGE:
        print(f"ERROR: v-end {args.v_end}V exceeds {MAX_VOLTAGE}V")
        return
    if args.v_start < MIN_VOLTAGE:
        print(f"ERROR: v-start {args.v_start}V below {MIN_VOLTAGE}V")
        return

    # Build voltage list
    voltages = []
    v = args.v_start
    while v <= args.v_end + 0.0001:
        voltages.append(round(v, 3))
        v += args.v_step

    print("Connecting to power supply...")
    supply = setup_supply(args.gpib)

    print(f"Opening Teensy on {args.teensy}...")
    teensy = serial.Serial(args.teensy, 115200, timeout=0.5)
    time.sleep(2)
    teensy.reset_input_buffer()
    resp = teensy_cmd(teensy, "S")
    print(f"Teensy status: {resp}")

    print("Connecting to USBDM...")
    bdm = USBDM()
    bdm.open()

    logfile = open(args.output, "w", newline="")
    writer = csv.writer(logfile)
    writer.writerow(["timestamp", "voltage_mv", "width_ns", "delay_ns",
                     "attempt", "probe_addr", "probe_val", "status"])

    # Summary: voltage_mv -> width_ns -> hit_count
    all_results = {}

    num_steps = (args.delay_end - args.delay_start) // args.delay_step
    total_per_width = num_steps * args.tries
    print(f"\nSweep: {len(voltages)} voltages x {len(width_list)} widths x "
          f"{num_steps} delay steps x {args.tries} tries = "
          f"{len(voltages) * len(width_list) * total_per_width} total attempts")
    print(f"Probe address: 0x{args.probe:04X}")
    print(f"Delay range: {args.delay_start}-{args.delay_end}ns, step {args.delay_step}ns")
    print(f"Widths: {width_list}ns\n")

    try:
        for voltage in voltages:
            voltage_mv = int(round(voltage * 1000))
            print(f"\n{'='*60}")
            print(f"VOLTAGE: {voltage:.3f}V ({voltage_mv}mV)")
            print(f"{'='*60}")

            actual = set_core_voltage(supply, voltage)
            time.sleep(1)

            all_results[voltage_mv] = {}

            for width_ns in width_list:
                print(f"\n  --- Width: {width_ns}ns ---")

                hits = sweep_bkgd(
                    bdm, teensy, writer, voltage_mv, width_ns,
                    args.delay_start, args.delay_end, args.delay_step,
                    args.tries, args.probe, supply, voltage)
                logfile.flush()

                all_results[voltage_mv][width_ns] = len(hits)
                print(f"  Width {width_ns}ns: {len(hits)} hits")

            # Per-voltage summary
            total = sum(all_results[voltage_mv].values())
            print(f"\n  {voltage_mv}mV total: {total} hits")
            for w in width_list:
                print(f"    W={w:>4}ns: {all_results[voltage_mv][w]} hits")

        # Final comparison
        print(f"\n{'='*60}")
        print("FINAL COMPARISON")
        print(f"{'='*60}")
        header = f"{'mV':>6}"
        for w in width_list:
            header += f" {'W'+str(w):>8}"
        header += f" {'Total':>8}"
        print(header)
        print("-" * (6 + 9 * (len(width_list) + 1)))
        for voltage_mv in sorted(all_results.keys()):
            row = f"{voltage_mv:>6}"
            total = 0
            for w in width_list:
                h = all_results[voltage_mv].get(w, 0)
                total += h
                row += f" {h:>8}"
            row += f" {total:>8}"
            print(row)

    except KeyboardInterrupt:
        print("\nSweep interrupted.")
    finally:
        teensy_cmd(teensy, "X")  # disarm
        print("\nReturning to 1.650V...")
        supply.ch_1.voltage_setpoint = 1.650
        logfile.close()
        teensy.close()
        bdm.close()
        print(f"Results saved to {args.output}")


if __name__ == "__main__":
    main()
