"""Windows equivalents for the exclusive advisory locks used by BLE leases.

Byte-range locks are released by Windows when the owning handle/process closes.
No lock bypass or best-effort success is permitted.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import errno
import msvcrt
import os
import time

LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8


def flock(fd: int, operation: int) -> None:
    if operation not in (LOCK_EX, LOCK_EX | LOCK_NB, LOCK_UN):
        raise ValueError("Only exclusive and unlock operations are supported")
    offset = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if operation == LOCK_UN:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if operation & LOCK_NB:
                    raise BlockingIOError(error.errno, str(error)) from error
                time.sleep(0.05)
    finally:
        os.lseek(fd, offset, os.SEEK_SET)


def pid_alive(pid: int) -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # invalid process id
            return False
        if error == 5:  # access denied is not proof that the owner has exited
            return True
        raise ctypes.WinError(error)
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)
