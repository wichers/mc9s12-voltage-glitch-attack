"""
Python ctypes wrapper for USBDM API (usbdm.4.dll).
Provides access to BDM communication with HC12/HCS12 targets.

Usage:
    from usbdm import USBDM

    with USBDM() as bdm:
        bdm.target_reset()
        bdm.connect()
        val = bdm.read_word(0xFFFE)
        print(f"0xFFFE = 0x{val:04X}")
"""

import ctypes
from ctypes import (
    c_int, c_uint, c_ubyte, c_ulong, c_bool, c_char_p,
    POINTER, byref, sizeof
)
import os
import sys

# ---------- Constants ----------

# TargetType_t
T_HCS12 = 0

# TargetVddSelect_t
BDM_TARGET_VDD_OFF = 0
BDM_TARGET_VDD_3V3 = 1
BDM_TARGET_VDD_5V = 2
BDM_TARGET_VDD_ENABLE = 0x10

# TargetMode_t
RESET_SPECIAL = 0
RESET_NORMAL = 1
RESET_HARDWARE = (1 << 2)
RESET_SOFTWARE = (2 << 2)
RESET_POWER = (3 << 2)

# AutoConnect_t
AUTOCONNECT_NEVER = 0
AUTOCONNECT_STATUS = 1
AUTOCONNECT_ALWAYS = 2

# USBDM_ErrorCode
BDM_RC_OK = 0
BDM_RC_FAIL = 2
BDM_RC_NO_CONNECTION = 5
BDM_RC_BDM_EN_FAILED = 18
BDM_RC_SYNC_TIMEOUT = 21

# BDMSTS register
BDMSTS_ADDR = 0xFF01
BDMSTS_ENBDM = 0x80
BDMSTS_UNSEC = 0x02


# ---------- Structures ----------

class USBDM_ExtendedOptions_t(ctypes.Structure):
    _fields_ = [
        ("size", c_uint),
        ("targetType", c_int),
        ("targetVdd", c_int),
        ("cycleVddOnReset", c_bool),
        ("cycleVddOnConnect", c_bool),
        ("leaveTargetPowered", c_bool),
        ("autoReconnect", c_int),
        ("guessSpeed", c_bool),
        ("bdmClockSource", c_int),
        ("useResetSignal", c_bool),
        ("maskInterrupts", c_bool),
        ("interfaceFrequency", c_uint),
        ("usePSTSignals", c_bool),
        ("powerOffDuration", c_uint),
        ("powerOnRecoveryInterval", c_uint),
        ("resetDuration", c_uint),
        ("resetReleaseInterval", c_uint),
        ("resetRecoveryInterval", c_uint),
    ]


class USBDM_bdmInformation_t(ctypes.Structure):
    _fields_ = [
        ("size", c_uint),
        ("BDMsoftwareVersion", c_int),
        ("BDMhardwareVersion", c_int),
        ("ICPsoftwareVersion", c_int),
        ("ICPhardwareVersion", c_int),
        ("capabilities", c_uint),
        ("commandBufferSize", c_uint),
        ("jtagBufferSize", c_uint),
    ]


# ---------- Exceptions ----------

class USBDMError(Exception):
    def __init__(self, func_name, rc, msg=""):
        self.func_name = func_name
        self.rc = rc
        self.msg = msg
        super().__init__(f"{func_name}() failed: rc={rc} ({msg})")


# ---------- USBDM Wrapper ----------

class USBDM:
    def __init__(self, dll_dir=None):
        if dll_dir is None:
            dll_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "usbdm_dump")

        # Add DLL directory for dependent DLLs
        if hasattr(os, 'add_dll_directory'):
            os.add_dll_directory(dll_dir)
        # Also prepend PATH as fallback
        os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")

        dll_path = os.path.join(dll_dir, "usbdm.4.dll")
        self._dll = ctypes.CDLL(dll_path)
        self._setup_functions()
        self._opened = False

    def _setup_functions(self):
        d = self._dll

        d.USBDM_Init.argtypes = []
        d.USBDM_Init.restype = c_int

        d.USBDM_Exit.argtypes = []
        d.USBDM_Exit.restype = c_int

        d.USBDM_FindDevices.argtypes = [POINTER(c_uint)]
        d.USBDM_FindDevices.restype = c_int

        d.USBDM_Open.argtypes = [c_ubyte]
        d.USBDM_Open.restype = c_int

        d.USBDM_Close.argtypes = []
        d.USBDM_Close.restype = c_int

        d.USBDM_SetTargetType.argtypes = [c_int]
        d.USBDM_SetTargetType.restype = c_int

        d.USBDM_SetTargetVdd.argtypes = [c_int]
        d.USBDM_SetTargetVdd.restype = c_int

        d.USBDM_GetDefaultExtendedOptions.argtypes = [
            POINTER(USBDM_ExtendedOptions_t)]
        d.USBDM_GetDefaultExtendedOptions.restype = c_int

        d.USBDM_SetExtendedOptions.argtypes = [
            POINTER(USBDM_ExtendedOptions_t)]
        d.USBDM_SetExtendedOptions.restype = c_int

        d.USBDM_GetBdmInformation.argtypes = [
            POINTER(USBDM_bdmInformation_t)]
        d.USBDM_GetBdmInformation.restype = c_int

        d.USBDM_Connect.argtypes = []
        d.USBDM_Connect.restype = c_int

        d.USBDM_TargetReset.argtypes = [c_int]
        d.USBDM_TargetReset.restype = c_int

        d.USBDM_ReadMemory.argtypes = [c_uint, c_uint, c_uint,
                                        POINTER(c_ubyte)]
        d.USBDM_ReadMemory.restype = c_int

        d.USBDM_WriteMemory.argtypes = [c_uint, c_uint, c_uint,
                                         POINTER(c_ubyte)]
        d.USBDM_WriteMemory.restype = c_int

        d.USBDM_ReadDReg.argtypes = [c_uint, POINTER(c_ulong)]
        d.USBDM_ReadDReg.restype = c_int

        d.USBDM_WriteCReg.argtypes = [c_uint, c_ulong]
        d.USBDM_WriteCReg.restype = c_int

        d.USBDM_GetErrorString.argtypes = [c_int]
        d.USBDM_GetErrorString.restype = c_char_p

    def _check(self, func_name, rc, ignore=None):
        """Check return code, raise on error unless in ignore set."""
        if rc == BDM_RC_OK:
            return rc
        if ignore and rc in ignore:
            return rc
        msg = self.get_error_string(rc)
        raise USBDMError(func_name, rc, msg)

    def get_error_string(self, rc):
        result = self._dll.USBDM_GetErrorString(rc)
        return result.decode('ascii', errors='replace') if result else f"error {rc}"

    # --- Lifecycle ---

    def open(self, device_no=0):
        """Initialize USBDM, find devices, open first device, configure for HCS12."""
        rc = self._dll.USBDM_Init()
        self._check("USBDM_Init", rc)

        count = c_uint(0)
        rc = self._dll.USBDM_FindDevices(byref(count))
        self._check("USBDM_FindDevices", rc)
        if count.value == 0:
            raise USBDMError("USBDM_FindDevices", -1, "No USBDM devices found")
        print(f"USBDM: found {count.value} device(s)")

        rc = self._dll.USBDM_Open(c_ubyte(device_no))
        self._check("USBDM_Open", rc)

        rc = self._dll.USBDM_SetTargetType(T_HCS12)
        self._check("USBDM_SetTargetType", rc)

        # Configure options
        opts = USBDM_ExtendedOptions_t()
        opts.size = sizeof(USBDM_ExtendedOptions_t)
        opts.targetType = T_HCS12
        rc = self._dll.USBDM_GetDefaultExtendedOptions(byref(opts))
        self._check("USBDM_GetDefaultExtendedOptions", rc)

        # Match settings from dump_mc9s12d64.exe
        opts.resetDuration = 500
        opts.resetReleaseInterval = 300
        opts.resetRecoveryInterval = 800
        opts.targetVdd = BDM_TARGET_VDD_5V
        opts.autoReconnect = AUTOCONNECT_NEVER
        rc = self._dll.USBDM_SetExtendedOptions(byref(opts))
        self._check("USBDM_SetExtendedOptions", rc)

        # Enable target power
        rc = self._dll.USBDM_SetTargetVdd(BDM_TARGET_VDD_5V | BDM_TARGET_VDD_ENABLE)
        self._check("USBDM_SetTargetVdd", rc)

        self._opened = True
        print("USBDM: opened and configured for HCS12")
        return self

    def close(self):
        if self._opened:
            self._dll.USBDM_Close()
            self._dll.USBDM_Exit()
            self._opened = False

    def __enter__(self):
        return self.open()

    def __exit__(self, *args):
        self.close()

    # --- BDM commands ---

    def target_reset(self, mode=RESET_HARDWARE | RESET_SPECIAL):
        """Assert and release target RESET."""
        rc = self._dll.USBDM_TargetReset(mode)
        self._check("USBDM_TargetReset", rc)

    def connect(self):
        """Send BDM SYNC + timing calibration. Returns rc.
        On secured chips, various errors (BDM_EN_FAILED, NO_RX_ROUTINE, etc.)
        are expected — timing calibration still succeeds and BDM commands work.
        The C++ code ignores the return code entirely."""
        rc = self._dll.USBDM_Connect()
        # Don't raise on any connect error — secured chips always fail here
        # but BDM commands still work after timing calibration
        return rc

    def read_memory(self, addr, count=2):
        """Read count bytes from target memory. Returns bytes."""
        buf = (c_ubyte * count)()
        rc = self._dll.USBDM_ReadMemory(1, count, addr, buf)
        self._check("USBDM_ReadMemory", rc)
        return bytes(buf)

    def read_word(self, addr):
        """Read 16-bit word from target. Returns int."""
        data = self.read_memory(addr, 2)
        return (data[0] << 8) | data[1]

    def write_memory(self, addr, data):
        """Write bytes to target memory."""
        count = len(data)
        buf = (c_ubyte * count)(*data)
        rc = self._dll.USBDM_WriteMemory(1, count, addr, buf)
        self._check("USBDM_WriteMemory", rc)

    def read_bdmsts(self):
        """Read BDMSTS register. Returns (enbdm, unsec, raw)."""
        val = c_ulong(0)
        rc = self._dll.USBDM_ReadDReg(BDMSTS_ADDR, byref(val))
        self._check("USBDM_ReadDReg", rc)
        raw = val.value
        enbdm = bool(raw & BDMSTS_ENBDM)
        unsec = bool(raw & BDMSTS_UNSEC)
        return enbdm, unsec, raw

    def get_bdm_info(self):
        """Get BDM adapter information."""
        info = USBDM_bdmInformation_t()
        info.size = sizeof(USBDM_bdmInformation_t)
        rc = self._dll.USBDM_GetBdmInformation(byref(info))
        self._check("USBDM_GetBdmInformation", rc)
        return info


# ---------- Quick test ----------

if __name__ == "__main__":
    print("USBDM Python wrapper test")
    print(f"ExtendedOptions size: {sizeof(USBDM_ExtendedOptions_t)} bytes")
    print(f"BdmInformation size: {sizeof(USBDM_bdmInformation_t)} bytes")

    with USBDM() as bdm:
        info = bdm.get_bdm_info()
        ver = info.BDMsoftwareVersion
        print(f"BDM firmware: {(ver>>16)&0xFF}.{(ver>>8)&0xFF}.{ver&0xFF}")
        print(f"Buffer size: {info.commandBufferSize}")

        print("\nResetting target...")
        bdm.target_reset()

        import time
        time.sleep(0.1)

        print("Connecting...")
        rc = bdm.connect()
        if rc == BDM_RC_BDM_EN_FAILED:
            print("  BDM_EN_FAILED (expected on secured chip)")
        else:
            print(f"  Connect OK (rc={rc})")

        try:
            enbdm, unsec, raw = bdm.read_bdmsts()
            print(f"BDMSTS = 0x{raw:02X} (ENBDM={int(enbdm)}, UNSEC={int(unsec)})")
        except USBDMError as e:
            print(f"  BDMSTS read failed: {e}")

        try:
            val = bdm.read_word(0xFFFE)
            print(f"Read 0xFFFE = 0x{val:04X}")
            if val == 0x0000 or val == 0xFFFF:
                print("  -> Secured (blocked read)")
            else:
                print(f"  -> UNSECURED! Reset vector = 0x{val:04X}")
        except USBDMError as e:
            print(f"  Read failed: {e}")
