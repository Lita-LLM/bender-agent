"""Исполнитель окон Windows (превью). Отдельный процесс: код ctypes не должен уронить Бендера.

Читает со стандартного ввода по одной JSON-команде в строке, отвечает JSON-строкой. Решений не принимает:
какое окно можно трогать, решает ядро (bender/tools/screen.py) перед каждой командой. Только Windows."""
import base64
import ctypes
import json
import struct
import sys
import time
import zlib
from ctypes import wintypes


def png(width, height, bgra):
    rows = bytearray()
    stride = width * 4
    for y in range(height):
        line = bgra[y * stride:(y + 1) * stride]
        rgb = bytearray(width * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = line[2::4], line[1::4], line[0::4]
        rows += b'\0' + rgb

    def chunk(kind, data):
        body = kind + data
        return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(bytes(rows), 6)) + chunk(b'IEND', b'')


def main():
    user32, gdi32, kernel32 = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.kernel32
    user32.SetProcessDPIAware()

    def info(hwnd):
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = ''
        handle = kernel32.OpenProcess(0x1000, False, pid.value)
        if handle:
            size = wintypes.DWORD(1024)
            path = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                exe = path.value
            kernel32.CloseHandle(handle)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return {'hwnd': int(hwnd), 'title': title.value, 'class': cls.value, 'pid': pid.value, 'exe': exe,
                'left': rect.left, 'top': rect.top, 'width': rect.right - rect.left, 'height': rect.bottom - rect.top}

    def windows():
        found = []
        callback = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def each(hwnd, _):
            if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
                found.append(info(hwnd))
            return True
        user32.EnumWindows(callback(each), 0)
        return found

    def shot(hwnd):
        data = info(hwnd)
        width, height = data['width'], data['height']
        if width <= 0 or height <= 0 or width * height > 8000 * 5000:
            raise ValueError('окно неподходящего размера')
        dc = user32.GetWindowDC(hwnd)
        mem = gdi32.CreateCompatibleDC(dc)
        bitmap = gdi32.CreateCompatibleBitmap(dc, width, height)
        gdi32.SelectObject(mem, bitmap)
        user32.PrintWindow(hwnd, mem, 2)  # PW_RENDERFULLCONTENT
        header = struct.pack('<IiiHHIIiiII', 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        buffer = ctypes.create_string_buffer(width * height * 4)
        gdi32.GetDIBits(mem, bitmap, 0, height, buffer, header, 0)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, dc)
        return {'png': base64.b64encode(png(width, height, buffer.raw)).decode(), 'width': width, 'height': height}

    def front(hwnd):
        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.15)
        if int(user32.GetForegroundWindow()) != int(hwnd):
            raise ValueError('окно не вышло на передний план: ввод не отправлен')

    def click(hwnd, x, y, button):
        front(hwnd)
        data = info(hwnd)
        if not (0 <= x < data['width'] and 0 <= y < data['height']):
            raise ValueError('точка вне окна')
        user32.SetCursorPos(data['left'] + int(x), data['top'] + int(y))
        down, up = (0x0002, 0x0004) if button == 'left' else (0x0008, 0x0010)
        user32.mouse_event(down, 0, 0, 0, 0)
        user32.mouse_event(up, 0, 0, 0, 0)

    class KeyInput(ctypes.Structure):
        _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD), ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD), ('extra', ctypes.c_void_p)]

    class Union(ctypes.Union):
        _fields_ = [('ki', KeyInput), ('pad', ctypes.c_byte * 32)]

    class Input(ctypes.Structure):
        _fields_ = [('type', wintypes.DWORD), ('u', Union)]

    def send(vk=0, scan=0, flags=0):
        item = Input(1, Union(ki=KeyInput(vk, scan, flags, 0, None)))
        user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(Input))

    KEYS = {'enter': 0x0D, 'tab': 0x09, 'escape': 0x1B, 'backspace': 0x08, 'delete': 0x2E, 'up': 0x26, 'down': 0x28, 'left': 0x25, 'right': 0x27}

    def write(hwnd, text):
        front(hwnd)
        for char in text[:500]:
            for unit in struct.unpack('<%dH' % (len(char.encode('utf-16-le')) // 2), char.encode('utf-16-le')):
                send(0, unit, 0x0004)
                send(0, unit, 0x0004 | 0x0002)

    def key(hwnd, name):
        front(hwnd)
        send(KEYS[name])
        send(KEYS[name], 0, 0x0002)

    for line in sys.stdin:
        try:
            cmd = json.loads(line)
            op = cmd['op']
            if op == 'list':
                out = {'windows': windows()}
            elif op == 'shot':
                out = shot(int(cmd['hwnd']))
            elif op == 'click':
                click(int(cmd['hwnd']), int(cmd['x']), int(cmd['y']), cmd.get('button', 'left'))
                out = {}
            elif op == 'type':
                write(int(cmd['hwnd']), str(cmd['text']))
                out = {}
            elif op == 'key':
                key(int(cmd['hwnd']), cmd['key'])
                out = {}
            else:
                raise ValueError('неизвестная команда')
            reply = {'ok': True, **out}
        except Exception as exc:  # noqa: BLE001 — исполнитель отвечает ошибкой, а не падает
            reply = {'ok': False, 'error': str(exc)[:200]}
        sys.stdout.write(json.dumps(reply) + '\n')
        sys.stdout.flush()


if __name__ == '__main__':
    main()
