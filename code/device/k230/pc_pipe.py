"""Byte-stream access to the CanMV extension's Windows named pipe."""

import ctypes
from ctypes import wintypes
import io
import math
import time


class _Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)]


class _PipeReader(io.RawIOBase):
    def __init__(self, channel):
        super().__init__()
        self.channel = channel

    def readable(self):
        return True

    def readinto(self, target):
        buffer = ctypes.create_string_buffer(len(target))
        count = self.channel._transfer(buffer, 0, len(target), writing=False)
        target[:count] = buffer.raw[:count]
        return count


class NamedPipeChannel:
    def __init__(self):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateFileW": ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
            "CreateEventW": ([ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            "ReadFile": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_Overlapped)], wintypes.BOOL),
            "WriteFile": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_Overlapped)], wintypes.BOOL),
            "GetOverlappedResult": ([wintypes.HANDLE, ctypes.POINTER(_Overlapped),
                                     ctypes.POINTER(wintypes.DWORD), wintypes.BOOL], wintypes.BOOL),
            "CancelIoEx": ([wintypes.HANDLE, ctypes.POINTER(_Overlapped)], wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "WaitNamedPipeW": ([wintypes.LPCWSTR, wintypes.DWORD], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result
        self.handle = None
        self.timeout = None

    def settimeout(self, timeout):
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Invalid pipe timeout")
        self.timeout = timeout

    def connect(self, endpoint):
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        while True:
            handle = self.api.CreateFileW(endpoint, 0xC0000000, 0, None, 3, 0x40000000, None)
            if handle != ctypes.c_void_p(-1).value:
                self.handle = handle
                return
            error = ctypes.get_last_error()
            if error != 231:  # All instances busy; wait without taking over the board's USB port.
                raise ctypes.WinError(error)
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("CanMV named pipe connection timed out")
            wait_ms = 1000 if remaining is None else max(1, min(1000, math.ceil(remaining * 1000)))
            if not self.api.WaitNamedPipeW(endpoint, wait_ms):
                error = ctypes.get_last_error()
                if error != 121:
                    raise ctypes.WinError(error)

    def makefile(self, mode):
        if mode != "rb":
            raise ValueError("Only binary pipe reads are supported")
        return io.BufferedReader(_PipeReader(self), buffer_size=64 * 1024)

    def sendall(self, data):
        buffer = ctypes.create_string_buffer(data)
        offset = 0
        while offset < len(data):
            count = self._transfer(buffer, offset, len(data) - offset, writing=True)
            if not count:
                raise BrokenPipeError("CanMV named pipe closed during write")
            offset += count

    def _transfer(self, buffer, offset, size, *, writing):
        if self.handle is None:
            raise OSError("CanMV named pipe is closed")
        event = self.api.CreateEventW(None, True, False, None)
        if not event:
            raise ctypes.WinError(ctypes.get_last_error())
        operation = _Overlapped(hEvent=event)
        count = wintypes.DWORD()
        function = self.api.WriteFile if writing else self.api.ReadFile
        try:
            if not function(self.handle, ctypes.byref(buffer, offset), size, None, ctypes.byref(operation)):
                error = ctypes.get_last_error()
                if not writing and error in (109, 232, 233):
                    return 0
                if error != 997:  # ERROR_IO_PENDING
                    raise ctypes.WinError(error)
                wait_ms = 0xFFFFFFFF if self.timeout is None else min(0xFFFFFFFE, math.ceil(self.timeout * 1000))
                status = self.api.WaitForSingleObject(event, wait_ms)
                if status != 0:
                    error = ctypes.get_last_error()
                    # Keep the buffer/event alive until cancellation has completed.
                    self.api.CancelIoEx(self.handle, ctypes.byref(operation))
                    self.api.GetOverlappedResult(self.handle, ctypes.byref(operation), ctypes.byref(count), True)
                    if status == 258:
                        raise TimeoutError("CanMV named pipe request timed out")
                    raise ctypes.WinError(error)
            if not self.api.GetOverlappedResult(self.handle, ctypes.byref(operation), ctypes.byref(count), False):
                error = ctypes.get_last_error()
                if not writing and error in (109, 232, 233):
                    return 0
                raise ctypes.WinError(error)
            return count.value
        finally:
            self.api.CloseHandle(event)

    def close(self):
        if self.handle is not None:
            self.api.CloseHandle(self.handle)
            self.handle = None
