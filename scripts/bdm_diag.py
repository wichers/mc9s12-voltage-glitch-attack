#!/usr/bin/env python3
"""
BDM pin diagnostic — verify Teensy can read RESET (pin 12) and BKGD (pin 6).

Run AFTER flashing teensy_bkgd_glitch.ino and rewiring pin 6 to BKGD.

Usage:
    python -u bdm_diag.py [--teensy COM11]

Tests:
  1. RESET pin reads HIGH when target is powered
  2. BKGD calibration detects edges when USBDM sends BDM commands
  3. Edge counting (E24 + arm) triggers on READ_WORD

The script talks to the Teensy only. You must trigger USBDM operations
externally (e.g. from USBDM's GUI or command-line tools) when prompted.
"""

import serial
import time
import argparse
import sys


def send_cmd(ser, cmd, delay=0.3):
    """Send command, wait, return response."""
    ser.reset_input_buffer()
    ser.write(f"{cmd}\n".encode())
    ser.flush()
    time.sleep(delay)
    return ser.read(ser.in_waiting or 1).decode(errors='replace')


def test_reset_pin(ser):
    """Test 1: Check RESET pin state."""
    print("=" * 50)
    print("TEST 1: RESET pin (pin 12)")
    print("=" * 50)

    resp = send_cmd(ser, "S", delay=0.5)
    lines = resp.strip().split('\n')
    reset_line = [l for l in lines if 'RESET pin' in l]

    if reset_line:
        state = reset_line[0].strip()
        print(f"  {state}")
        if "HIGH" in state:
            print("  PASS: RESET is HIGH (target powered / not held in reset)")
            return True
        else:
            print("  INFO: RESET is LOW (target in reset or not powered)")
            print("  -> If target should be running, check wiring to pin 12")
            return True  # Still readable, just low
    else:
        print("  FAIL: Could not read RESET pin state")
        print(f"  Raw response: {resp!r}")
        return False


def test_bkgd_calibrate(ser):
    """Test 2: Enter calibrate mode and wait for BDM edges on BKGD (pin 6)."""
    print()
    print("=" * 50)
    print("TEST 2: BKGD calibration (pin 6)")
    print("=" * 50)
    print("  Entering calibrate mode (T command)...")

    resp = send_cmd(ser, "T", delay=0.5)
    print(f"  Teensy says: {resp.strip()}")

    print()
    print("  >>> NOW trigger USBDM operations (connect, read, etc.) <<<")
    print("  >>> e.g. open USBDM GUI and click 'Connect'              <<<")
    print("  >>> Waiting up to 30 seconds for edges on BKGD...         <<<")
    print()

    start = time.time()
    buf = ""
    while time.time() - start < 30:
        data = ser.read(ser.in_waiting or 1)
        if data:
            text = data.decode(errors='replace')
            buf += text
            # Calibration report starts with T and contains timing info
            if 'T' in buf and ('ns' in buf or 'kHz' in buf or 'bit' in buf.lower()):
                print(f"  Calibration result: {buf.strip()}")
                print("  PASS: BKGD edges detected on pin 6!")
                # Disarm
                send_cmd(ser, "X", delay=0.2)
                return True
            # Check for any edge-related output
            if '\n' in buf:
                lines = buf.split('\n')
                for line in lines[:-1]:
                    line = line.strip()
                    if line and 'CALIBRATE' not in line:
                        print(f"  Received: {line}")
                buf = lines[-1]
        time.sleep(0.1)

    print("  FAIL: No BDM edges detected after 30 seconds")
    print("  -> Check that pin 6 is wired to BKGD (bypass level converter)")
    print("  -> Check that USBDM is connected to target and sending commands")
    # Disarm
    send_cmd(ser, "X", delay=0.2)
    return False


def test_edge_counting(ser):
    """Test 3: Arm edge counter (E24 + A) and wait for READ_WORD trigger."""
    print()
    print("=" * 50)
    print("TEST 3: Edge counting (24 edges = one READ_WORD)")
    print("=" * 50)

    # Set edge target to 24
    resp = send_cmd(ser, "E24", delay=0.3)
    print(f"  Set edge target: {resp.strip()}")

    # Set safe glitch width (0 = no actual glitch, just detection)
    resp = send_cmd(ser, "W0", delay=0.3)
    print(f"  Width set to 0 (no glitch): {resp.strip()}")

    # Arm single-shot
    resp = send_cmd(ser, "A", delay=0.3)
    print(f"  Armed: {resp.strip()}")

    print()
    print("  >>> NOW trigger a USBDM read operation <<<")
    print("  >>> e.g. read a memory address via USBDM GUI or CLI <<<")
    print("  >>> Waiting up to 30 seconds for 24 edges...         <<<")
    print()

    start = time.time()
    buf = ""
    while time.time() - start < 30:
        data = ser.read(ser.in_waiting or 1)
        if data:
            text = data.decode(errors='replace')
            buf += text
            # Glitch report starts with G
            if 'G' in buf and '\n' in buf:
                lines = buf.split('\n')
                for line in lines:
                    line = line.strip()
                    if line.startswith('G'):
                        print(f"  Trigger report: {line}")
                        print("  PASS: 24 edges counted, trigger fired!")
                        send_cmd(ser, "X", delay=0.2)
                        return True
                    elif line and 'ARMED' not in line:
                        print(f"  Received: {line}")
                buf = lines[-1] if not lines[-1].endswith('\n') else ""
        time.sleep(0.1)

    # Check status to see how many edges were counted
    resp = send_cmd(ser, "S", delay=0.5)
    edge_line = [l for l in resp.split('\n') if 'Edges:' in l]
    if edge_line:
        print(f"  {edge_line[0].strip()}")

    print("  FAIL: Did not reach 24 edges in 30 seconds")
    print("  -> If some edges counted: edge target may not match BDM command length")
    print("  -> If zero edges: BKGD signal not reaching pin 6")
    send_cmd(ser, "X", delay=0.2)
    return False


def main():
    parser = argparse.ArgumentParser(description="BDM pin diagnostics")
    parser.add_argument("--teensy", default="COM11")
    args = parser.parse_args()

    print(f"Connecting to Teensy on {args.teensy}...")
    try:
        ser = serial.Serial(args.teensy, 115200, timeout=0.5)
    except serial.SerialException as e:
        print(f"ERROR: Cannot open {args.teensy}: {e}")
        sys.exit(1)

    time.sleep(2)  # wait for Teensy USB init
    ser.reset_input_buffer()

    # Disarm any previous state
    send_cmd(ser, "X", delay=0.3)

    results = []

    # Test 1: RESET pin
    results.append(("RESET pin", test_reset_pin(ser)))

    # Test 2: BKGD calibration
    results.append(("BKGD calibration", test_bkgd_calibrate(ser)))

    # Test 3: Edge counting (only if test 2 passed)
    if results[-1][1]:
        results.append(("Edge counting", test_edge_counting(ser)))
    else:
        print("\n  Skipping edge counting test (BKGD calibration failed)")
        results.append(("Edge counting", None))

    # Summary
    print()
    print("=" * 50)
    print("SUMMARY")
    print("=" * 50)
    for name, passed in results:
        if passed is None:
            status = "SKIPPED"
        elif passed:
            status = "PASS"
        else:
            status = "FAIL"
        print(f"  {name}: {status}")

    all_pass = all(r[1] for r in results if r[1] is not None)
    if all_pass:
        print("\nAll tests passed! Ready for BDM glitch attack.")
    else:
        print("\nSome tests failed. Fix wiring before proceeding.")

    ser.close()


if __name__ == "__main__":
    main()
