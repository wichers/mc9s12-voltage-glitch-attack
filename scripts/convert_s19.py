#!/usr/bin/env python3
"""Convert MC9S12D64 S19 dumps between address formats.

Handles conversion between physical addresses (page * 0x4000) and
USBDM/Freescale paged addresses (page << 16 | cpu_addr) for the
MC9S12D64's banked flash.

Usage:
    python convert_s19.py input.s19 output.s19 [--format usbdm|physical|flat]

Formats:
    usbdm    - Page in high byte: 0x3C8000-0x3CBFFF (default, for USBDM tools)
    physical - Page * 0x4000: 0xF0000-0xF3FFF (linear physical)
    flat     - All paged data at 0x8000-0xBFFF window (loses page distinction)
"""

import argparse
import sys


def parse_s19(filename):
    """Parse S19 file into list of (address, data_bytes) tuples."""
    records = []
    with open(filename, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec_type = line[0:2]
            if rec_type == 'S1':
                # S1: 16-bit address
                byte_count = int(line[2:4], 16)
                addr = int(line[4:8], 16)
                data_hex = line[8:8 + (byte_count - 3) * 2]
                data = bytes.fromhex(data_hex)
                records.append((addr, data))
            elif rec_type == 'S2':
                # S2: 24-bit address
                byte_count = int(line[2:4], 16)
                addr = int(line[4:10], 16)
                data_hex = line[10:10 + (byte_count - 4) * 2]
                data = bytes.fromhex(data_hex)
                records.append((addr, data))
            elif rec_type == 'S3':
                # S3: 32-bit address
                byte_count = int(line[2:4], 16)
                addr = int(line[4:12], 16)
                data_hex = line[12:12 + (byte_count - 5) * 2]
                data = bytes.fromhex(data_hex)
                records.append((addr, data))
            # Skip S0 (header), S5 (count), S7/S8/S9 (end)
    return records


def classify_record(addr):
    """Classify a record's address into region and page.

    Returns (region, page) where:
        region: 'eeprom', 'fixed_low', 'paged', 'fixed_high', 'unknown'
        page: None for non-paged, 0x3C/0x3D for paged
    """
    # USBDM format: page in high byte
    if addr >= 0x3C0000:
        page = (addr >> 16) & 0xFF
        cpu_addr = addr & 0xFFFF
        if 0x8000 <= cpu_addr <= 0xBFFF and page in (0x3C, 0x3D):
            return 'paged', page

    # Physical format: page * 0x4000
    if 0xF0000 <= addr <= 0xF3FFF:
        return 'paged', 0x3C
    if 0xF4000 <= addr <= 0xF7FFF:
        return 'paged', 0x3D

    # CPU addresses (16-bit)
    if addr <= 0xFFFF:
        if 0x0400 <= addr <= 0x07FF:
            return 'eeprom', None
        if 0x4000 <= addr <= 0x7FFF:
            return 'fixed_low', None  # page 0x3E
        if 0x8000 <= addr <= 0xBFFF:
            return 'paged', None  # unknown page (flat format)
        if 0xC000 <= addr <= 0xFFFF:
            return 'fixed_high', None  # page 0x3F

    return 'unknown', None


def to_cpu_addr(addr, region, page):
    """Convert any address format back to CPU address (0x8000-0xBFFF for paged)."""
    if region == 'paged':
        if addr >= 0x3C0000:  # USBDM format
            return addr & 0xFFFF
        if 0xF0000 <= addr <= 0xF7FFF:  # physical format
            return (addr & 0x3FFF) + 0x8000
        return addr  # already CPU address
    return addr


def convert_addr(cpu_addr, page, fmt):
    """Convert CPU address + page to target format."""
    if page is None:
        return cpu_addr  # non-paged, keep as-is

    if fmt == 'usbdm':
        return (page << 16) | cpu_addr
    elif fmt == 'physical':
        return page * 0x4000 + (cpu_addr - 0x8000)
    elif fmt == 'flat':
        return cpu_addr  # lose page info
    return cpu_addr


def s_record(addr, data):
    """Generate an S-record line for the given address and data."""
    if addr > 0xFFFF:
        # S2: 24-bit address
        byte_count = len(data) + 4  # addr(3) + data + checksum(1)
        header = f"S2{byte_count:02X}{addr:06X}"
    else:
        # S1: 16-bit address
        byte_count = len(data) + 3  # addr(2) + data + checksum(1)
        header = f"S1{byte_count:02X}{addr:04X}"

    data_hex = data.hex().upper()
    full = header + data_hex

    # Calculate checksum
    raw_bytes = bytes.fromhex(full[2:])
    checksum = (~sum(raw_bytes)) & 0xFF
    return full + f"{checksum:02X}"


def write_s19(filename, records, fmt, chunk_size=16, exclude_eeprom=False):
    """Write records to S19 file in the specified format."""
    # Sort by address
    records.sort(key=lambda r: r[0])

    with open(filename, 'w') as f:
        total_bytes = 0
        for addr, data in records:
            region, page = classify_record(addr)

            # Skip EEPROM if requested
            if exclude_eeprom and region == 'eeprom':
                continue

            cpu_addr = to_cpu_addr(addr, region, page)
            new_addr = convert_addr(cpu_addr, page, fmt)

            # Write in chunks (default 16 bytes to match USBDM format)
            offset = 0
            while offset < len(data):
                chunk = data[offset:offset + chunk_size]
                f.write(s_record(new_addr + offset, chunk) + "\n")
                total_bytes += len(chunk)
                offset += len(chunk)

    return total_bytes


def main():
    parser = argparse.ArgumentParser(
        description='Convert MC9S12D64 S19 dumps between address formats')
    parser.add_argument('input', help='Input S19 file')
    parser.add_argument('output', help='Output S19 file')
    parser.add_argument('--format', choices=['usbdm', 'physical', 'flat'],
                        default='usbdm',
                        help='Output address format (default: usbdm)')
    parser.add_argument('--info', action='store_true',
                        help='Show info about input file and exit')
    parser.add_argument('--no-eeprom', action='store_true',
                        help='Exclude EEPROM data from output')
    parser.add_argument('--chunk-size', type=int, default=16,
                        help='Bytes per S-record (default: 16, USBDM compatible)')
    args = parser.parse_args()

    records = parse_s19(args.input)

    if args.info or args.output == '-':
        print(f"Input: {args.input}")
        print(f"Records: {len(records)}")

        regions = {}
        for addr, data in records:
            region, page = classify_record(addr)
            key = f"{region}" + (f" (page 0x{page:02X})" if page else "")
            if key not in regions:
                regions[key] = {'min': addr, 'max': addr + len(data) - 1, 'bytes': 0}
            regions[key]['min'] = min(regions[key]['min'], addr)
            regions[key]['max'] = max(regions[key]['max'], addr + len(data) - 1)
            regions[key]['bytes'] += len(data)

        for key, info in sorted(regions.items(), key=lambda x: x[1]['min']):
            print(f"  {key}: 0x{info['min']:06X}-0x{info['max']:06X} ({info['bytes']} bytes)")
        return

    total = write_s19(args.output, records, args.format,
                      chunk_size=args.chunk_size,
                      exclude_eeprom=args.no_eeprom)
    print(f"Converted {len(records)} records ({total} bytes) to {args.format} format")
    print(f"Output: {args.output}")


if __name__ == '__main__':
    main()
