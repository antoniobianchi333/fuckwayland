"""Pure-stdlib /dev/uinput virtual input devices (legacy uinput_user_dev setup).

Env overrides for testing:
  WDOTOOL_UINPUT_PATH  device node to open (default /dev/uinput)
  WDOTOOL_FAKE_UINPUT=1  skip ioctls so a regular file can stand in for uinput
"""

import fcntl
import os
import struct

EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03
SYN_REPORT = 0

REL_X = 0x00
REL_Y = 0x01
REL_HWHEEL = 0x06
REL_WHEEL = 0x08
ABS_X = 0x00
ABS_Y = 0x01

BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112
BTN_SIDE = 0x113
BTN_EXTRA = 0x114
BTN_FORWARD = 0x115
BTN_BACK = 0x116
BTN_TASK = 0x117

ABS_RANGE = 32767  # QEMU usb-tablet axis maximum

# ioctls (x86-64/aarch64: _IOC dir<<30 | size<<16 | 'U'<<8 | nr)
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_SET_ABSBIT = 0x40045567

BUS_USB = 0x03

_EVENT_FMT = "llHHi"  # struct input_event: timeval + type,code,value (24B on LP64)
_USER_DEV_FMT = "80sHHHHI64i64i64i64i"  # struct uinput_user_dev (1116 bytes)


class UinputDevice:
    """One virtual evdev device created through /dev/uinput."""

    def __init__(self, name, keys=(), rels=(), abs_axes=(), vendor=0x0627, product=0x0001):
        path = os.environ.get("WDOTOOL_UINPUT_PATH", "/dev/uinput")
        self.fake = os.environ.get("WDOTOOL_FAKE_UINPUT") == "1"
        self.fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        self._created = False
        try:
            if not self.fake:
                if keys:
                    fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
                    for k in keys:
                        fcntl.ioctl(self.fd, UI_SET_KEYBIT, k)
                if rels:
                    fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REL)
                    for r in rels:
                        fcntl.ioctl(self.fd, UI_SET_RELBIT, r)
                if abs_axes:
                    fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
                    for a in abs_axes:
                        fcntl.ioctl(self.fd, UI_SET_ABSBIT, a)
            absmin = [0] * 64
            absmax = [0] * 64
            for a in abs_axes:
                absmax[a] = ABS_RANGE
            os.write(
                self.fd,
                struct.pack(
                    _USER_DEV_FMT,
                    name.encode(),
                    BUS_USB, vendor, product, 1,  # input_id
                    0,  # ff_effects_max
                    *absmax, *absmin, *([0] * 64), *([0] * 64),
                ),
            )
            if not self.fake:
                fcntl.ioctl(self.fd, UI_DEV_CREATE)
            self._created = True
        except BaseException:
            os.close(self.fd)
            raise

    def emit(self, etype: int, code: int, value: int):
        os.write(self.fd, struct.pack(_EVENT_FMT, 0, 0, etype, code, value))

    def syn(self):
        self.emit(EV_SYN, SYN_REPORT, 0)

    def key(self, code: int, down: bool):
        self.emit(EV_KEY, code, 1 if down else 0)
        self.syn()

    def close(self):
        if self.fd < 0:
            return
        try:
            if self._created and not self.fake:
                fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self.fd)
        self.fd = -1


def keyboard() -> UinputDevice:
    return UinputDevice("wdotool virtual keyboard", keys=range(1, 249))


def rel_mouse() -> UinputDevice:
    return UinputDevice(
        "wdotool virtual mouse",
        keys=(BTN_LEFT, BTN_MIDDLE, BTN_RIGHT, BTN_SIDE, BTN_EXTRA,
              BTN_FORWARD, BTN_BACK, BTN_TASK),
        rels=(REL_X, REL_Y, REL_WHEEL, REL_HWHEEL),
        product=0x0002,
    )


def abs_pointer() -> UinputDevice:
    """Absolute pointer shaped like a QEMU usb-tablet (every compositor maps
    its 0..32767 axes across the whole output layout)."""
    return UinputDevice(
        "wdotool virtual tablet",
        keys=(BTN_LEFT, BTN_MIDDLE, BTN_RIGHT),
        rels=(REL_WHEEL,),
        abs_axes=(ABS_X, ABS_Y),
        product=0x0003,
    )
