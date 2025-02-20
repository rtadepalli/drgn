# Copyright (c) Meta Platforms, Inc. and affiliates.
# SPDX-License-Identifier: GPL-3.0-or-later

import contextlib
import ctypes
import errno
import os
from pathlib import Path
import platform
import re
import signal
import socket
import time
from typing import NamedTuple
import unittest

import drgn
from tests import TestCase


class LinuxKernelTestCase(TestCase):
    prog = None
    skip_reason = None

    @staticmethod
    def _load_debug_info(prog):
        paths = []
        try:
            paths.append(os.environ["DRGN_TEST_KMOD"])
        except KeyError:
            pass
        prog.load_debug_info(paths, True)

    @classmethod
    def setUpClass(cls):
        # We only want to create the Program once for all tests, so it's cached
        # as a class variable (in the base class). If we can't run these tests
        # for whatever reason, we also cache that.
        if LinuxKernelTestCase.prog is not None:
            return
        if LinuxKernelTestCase.skip_reason is None:
            try:
                run_tests = int(os.environ["DRGN_RUN_LINUX_KERNEL_TESTS"]) != 0
            except (KeyError, ValueError):
                run_tests = True
                force_run = False
            else:
                force_run = run_tests
            if run_tests:
                prog = drgn.Program()
                try:
                    prog.set_kernel()
                except PermissionError:
                    if force_run:
                        raise
                    LinuxKernelTestCase.skip_reason = (
                        "Linux kernel tests must be run as root "
                        "(run with env DRGN_RUN_LINUX_KERNEL_TESTS=1 to force)"
                    )
                except (FileNotFoundError, ValueError):
                    if force_run:
                        raise
                    LinuxKernelTestCase.skip_reason = (
                        "Linux kernel tests require /proc/kcore "
                        "(run with env DRGN_RUN_LINUX_KERNEL_TESTS=1 to force)"
                    )
                else:
                    # Some of the tests use the loop module. Open loop-control
                    # so that it is loaded.
                    try:
                        with open("/dev/loop-control", "r"):
                            pass
                    except FileNotFoundError:
                        pass
                    try:
                        cls._load_debug_info(prog)
                        LinuxKernelTestCase.prog = prog
                        return
                    except drgn.MissingDebugInfoError as e:
                        if force_run:
                            raise
                        LinuxKernelTestCase.skip_reason = str(e)
            else:
                LinuxKernelTestCase.skip_reason = "env DRGN_RUN_LINUX_KERNEL_TESTS=0"
        raise unittest.SkipTest(LinuxKernelTestCase.skip_reason)


skip_unless_have_test_kmod = unittest.skipUnless(
    "DRGN_TEST_KMOD" in os.environ, "test requires drgn_test Linux kernel module"
)

skip_unless_have_full_mm_support = unittest.skipUnless(
    platform.machine() == "x86_64",
    f"mm support is not implemented for {platform.machine()}",
)


def wait_until(fn, *args, **kwds):
    TIMEOUT = 5
    deadline = time.monotonic() + TIMEOUT
    sleep = 1e-6
    while True:
        if fn(*args, **kwds):
            break
        now = time.monotonic()
        if now >= deadline:
            raise Exception(f"condition was not met in {TIMEOUT} seconds")
        time.sleep(min(deadline - now, sleep))
        sleep *= 2


def fork_and_pause(fn=None):
    pid = os.fork()
    if pid == 0:
        if fn:
            fn()
        try:
            while True:
                signal.pause()
        finally:
            os._exit(1)
    return pid


def proc_state(pid):
    with open(f"/proc/{pid}/status", "r") as f:
        return re.search(r"State:\s*(\S)", f.read(), re.M).group(1)


# Return whether a process is blocked and fully scheduled out. The process
# state is updated while the process is still running, so use this instead of
# proc_state(pid) != "R" to avoid races. This is not accurate if pid is the
# calling thread.
def proc_blocked(pid):
    with open(f"/proc/{pid}/syscall", "r") as f:
        return f.read() != "running\n"


def smp_enabled():
    return bool(re.search(r"\bSMP\b", os.uname().version))


def parse_range_list(s):
    values = set()
    s = s.strip()
    if s:
        for range_str in s.split(","):
            first, sep, last = range_str.partition("-")
            if sep:
                values.update(range(int(first), int(last) + 1))
            else:
                values.add(int(first))
    return values


_c = ctypes.CDLL(None, use_errno=True)

_mount = _c.mount
_mount.restype = ctypes.c_int
_mount.argtypes = [
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_ulong,
    ctypes.c_void_p,
]
MS_RDONLY = 1
MS_NOSUID = 2
MS_NODEV = 4
MS_NOEXEC = 8
MS_SYNCHRONOUS = 16
MS_REMOUNT = 32
MS_MANDLOCK = 64
MS_DIRSYNC = 128
MS_NOSYMFOLLOW = 256
MS_NOATIME = 1024
MS_NODIRATIME = 2048
MS_BIND = 4096
MS_MOVE = 8192
MS_REC = 16384
MS_SILENT = 32768
MS_POSIXACL = 1 << 16
MS_UNBINDABLE = 1 << 17
MS_PRIVATE = 1 << 18
MS_SLAVE = 1 << 19
MS_SHARED = 1 << 20
MS_RELATIME = 1 << 21
MS_KERNMOUNT = 1 << 22
MS_I_VERSION = 1 << 23
MS_STRICTATIME = 1 << 24
MS_LAZYTIME = 1 << 25


def _check_ctypes_syscall(ret, *args):
    if ret == -1:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno), *args)
    return ret


def mount(source, target, fstype, flags=0, data=None):
    _check_ctypes_syscall(
        _mount(
            os.fsencode(source),
            os.fsencode(target),
            fstype.encode(),
            flags,
            None if data is None else data.encode(),
        ),
        source,
        None,
        target,
    )


_umount2 = _c.umount2
_umount2.restype = ctypes.c_int
_umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]


def umount(target, flags=0):
    _check_ctypes_syscall(_umount2(os.fsencode(target), flags), target)


_MOUNTS_RE = re.compile(
    rb"(?P<source>[^ ]+) (?P<mount_point>[^ ]+) (?P<fstype>[^ ]+) "
    rb"(?P<mount_options>[^ ]+) [0-9]+ [0-9]+"
)


class Mount(NamedTuple):
    source: str
    mount_point: Path
    fstype: str
    mount_options: str


def iter_mounts(pid="self"):
    with open(f"/proc/{pid}/mounts", "rb") as f:
        for line in f:
            match = _MOUNTS_RE.match(line)
            assert match
            yield Mount(
                source=match["source"].decode("unicode-escape"),
                mount_point=Path(match["mount_point"].decode("unicode-escape")),
                fstype=match["fstype"].decode("unicode-escape"),
                mount_options=match["mount_options"].decode("unicode-escape"),
            )


_mlock = _c.mlock
_mlock.restype = ctypes.c_int
_mlock.argtypes = [ctypes.c_void_p, ctypes.c_size_t]


def mlock(addr, len):
    _check_ctypes_syscall(_mlock(addr, len))


_syscall = _c.syscall
_syscall.restype = ctypes.c_long


def create_socket(*args, **kwds):
    try:
        return socket.socket(*args, **kwds)
    except OSError as e:
        if e.errno in (errno.ENOSYS, errno.EAFNOSUPPORT, errno.ESOCKTNOSUPPORT):
            raise unittest.SkipTest("kernel does not support TCP")
        else:
            raise


@contextlib.contextmanager
def setenv(key, value):
    old_value = os.environ.get(key)
    try:
        if value is not None:
            os.environ[key] = value
        elif old_value is not None:
            del os.environ[key]
        yield
    finally:
        if old_value is None:
            del os.environ[key]
        else:
            os.environ[key] = old_value
