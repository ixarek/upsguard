#!/usr/bin/env python3
"""Small X11 warning window. JSON over pipes; no settings/state files.

Uses the desktop's libX11 and libcairo through Python's standard library.
The worker never executes power or service commands.
"""
import ctypes as C
from ctypes.util import find_library
import json
import math
import os
import select
import sys
import time

WIDTH, HEIGHT = 680, 430


def panel_text(data, now):
    phase = data.get('phase', 'outage')
    mode = data.get('mode', 'off')
    action = {'off': 'Выключение АРМ', 'no': 'Выключение АРМ',
              'disk': 'Гибернация АРМ', 'mem': 'Сон АРМ (mem)',
              'standby': 'Сон АРМ (standby)', 'freeze': 'Сон АРМ (freeze)',
              'monitor_only': 'Гашение монитора'}.get(mode, mode)
    due = data.get('deadline')
    seconds = None if due is None else max(0, math.ceil(due - now))
    timer = '—' if seconds is None else '%02d:%02d' % divmod(seconds, 60)
    charge = data.get('charge')
    charge = '—' if charge is None else '%g%%' % charge
    header = 'ИБП работает от батареи'
    label = 'ДО ДЕЙСТВИЯ'
    message = ('Заряд выше порога или ещё подтверждается снижение.' if due is None
               else 'Остановите службу, чтобы отменить действие.')
    if phase == 'ol':
        header, label = 'Питание от сети восстановлено', 'ПОДТВЕРЖДЕНИЕ OL'
        message = 'Ожидается устойчивое питание от сети.'
    elif phase == 'done':
        timer, message = '00:00', 'Пробный отсчёт завершён. Действие пропущено.'
    elif phase == 'error':
        header, label = 'Не удалось выполнить действие', 'ДО ПОВТОРА'
        message = 'Проверьте поддержку выбранного режима и журнал службы.'
    elif seconds == 0:
        message = 'Выполняется последняя проверка показаний ИБП.'
    if data.get('demo'):
        header, message = 'Проверка окна UPS Guard', 'Демонстрация: команды сна и выключения не выполняются.'
    return dict(header=header, timer=timer, charge=charge, label=label,
                action=action, message=message,
                detail='Порог: %g%%   ·   Статус: %s' %
                       (data.get('threshold', 50), data.get('status', 'OB')),
                note=('Проверка завершится автоматически через 30 секунд.'
                      if data.get('demo') else
                      'ПРОБНЫЙ РЕЖИМ: сон и выключение не выполняются.'
                      if data.get('dry_run') else
                      'Кнопка остановки отключает защиту только этого АРМ.'))


def bind(lib, name, args, result=None):
    function = getattr(lib, name)
    function.argtypes, function.restype = args, result
    return function


class Extents(C.Structure):
    _fields_ = [(key, C.c_double) for key in
                ('x_bearing', 'y_bearing', 'width', 'height', 'x_advance', 'y_advance')]


class Painter:
    def __init__(self):
        self.lib = C.CDLL(find_library('cairo') or 'libcairo.so.2')
        p, d, i, s = C.c_void_p, C.c_double, C.c_int, C.c_char_p
        for name, args, result in (
                ('cairo_create', [p], p), ('cairo_destroy', [p], None),
                ('cairo_surface_destroy', [p], None), ('cairo_surface_flush', [p], None),
                ('cairo_set_source_rgb', [p, d, d, d], None),
                ('cairo_paint', [p], None), ('cairo_rectangle', [p, d, d, d, d], None),
                ('cairo_fill', [p], None), ('cairo_move_to', [p, d, d], None),
                ('cairo_select_font_face', [p, s, i, i], None),
                ('cairo_set_font_size', [p, d], None), ('cairo_show_text', [p, s], None),
                ('cairo_text_extents', [p, s, C.POINTER(Extents)], None),
                ('cairo_save', [p], None), ('cairo_restore', [p], None),
                ('cairo_scale', [p, d, d], None),
                ('cairo_xlib_surface_create', [p, C.c_ulong, p, i, i], p),
                ('cairo_xlib_surface_set_size', [p, i, i], None),
                ('cairo_image_surface_create', [i, i, i], p),
                ('cairo_surface_write_to_png', [p, s], i)):
            bind(self.lib, name, args, result)

    def draw(self, context, data, now, width=WIDTH, height=HEIGHT):
        c = self.lib
        text = panel_text(data, now)
        dark, grey, red = (0.12, 0.17, 0.23), (0.38, 0.43, 0.49), (0.72, 0.16, 0.12)

        def colour(rgb):
            c.cairo_set_source_rgb(context, *rgb)

        def box(x, y, w, h, rgb):
            colour(rgb)
            c.cairo_rectangle(context, x, y, w, h)
            c.cairo_fill(context)

        def write(value, x, y, size=18, rgb=dark, bold=False, max_width=None):
            c.cairo_select_font_face(context, b'DejaVu Sans', 0, 1 if bold else 0)
            c.cairo_set_font_size(context, size)
            ext = Extents()
            if max_width:
                while value:
                    c.cairo_text_extents(context, value.encode('utf-8'), C.byref(ext))
                    if ext.x_advance <= max_width:
                        break
                    value = value[:-2] + '…' if len(value) > 1 else ''
            colour(rgb)
            c.cairo_move_to(context, x, y)
            c.cairo_show_text(context, value.encode('utf-8'))

        c.cairo_save(context)
        c.cairo_scale(context, width / WIDTH, height / HEIGHT)
        colour((0.98, 0.98, 0.99))
        c.cairo_paint(context)
        box(0, 0, WIDTH, 7, red)
        write(text['header'], 28, 49, 25, bold=True, max_width=624)
        write('UPS Guard  ·  ' + text['action'], 28, 80, 16, grey, max_width=624)
        box(28, 102, 624, 105, (0.93, 0.94, 0.96))
        write('ЗАРЯД БАТАРЕИ', 48, 130, 13, grey, bold=True)
        write(text['charge'], 48, 182, 39, bold=True)
        write(text['label'], 350, 130, 13, grey, bold=True, max_width=280)
        write(text['timer'], 350, 185, 50, red, bold=True)
        write(text['detail'], 28, 245, 18, max_width=624)
        write(text['message'], 28, 278, 16, max_width=624)
        if data.get('error'):
            write(str(data['error']).replace('\n', ' '), 28, 305, 13, red, max_width=624)
        box(28, 329, 390, 52, red)
        box(438, 329, 214, 52, (0.87, 0.89, 0.92))
        write('Закрыть проверку' if data.get('demo') else 'Остановить службу',
              90, 362, 18, (1, 1, 1), bold=True)
        write('Скрыть окно', 482, 362, 17)
        write(text['note'], 28, 409, 13, grey, max_width=624)
        c.cairo_restore(context)


class ButtonEvent(C.Structure):
    _fields_ = [('type', C.c_int), ('serial', C.c_ulong), ('send_event', C.c_int),
                ('display', C.c_void_p), ('window', C.c_ulong), ('root', C.c_ulong),
                ('subwindow', C.c_ulong), ('time', C.c_ulong), ('x', C.c_int),
                ('y', C.c_int), ('x_root', C.c_int), ('y_root', C.c_int),
                ('state', C.c_uint), ('button', C.c_uint), ('same_screen', C.c_int)]


class ConfigureEvent(C.Structure):
    _fields_ = [('type', C.c_int), ('serial', C.c_ulong), ('send_event', C.c_int),
                ('display', C.c_void_p), ('event', C.c_ulong), ('window', C.c_ulong),
                ('x', C.c_int), ('y', C.c_int), ('width', C.c_int), ('height', C.c_int),
                ('border_width', C.c_int), ('above', C.c_ulong), ('override_redirect', C.c_int)]


class ClientEvent(C.Structure):
    _fields_ = [('type', C.c_int), ('serial', C.c_ulong), ('send_event', C.c_int),
                ('display', C.c_void_p), ('window', C.c_ulong),
                ('message_type', C.c_ulong), ('format', C.c_int), ('data', C.c_long * 5)]


class XEvent(C.Union):
    _fields_ = [('type', C.c_int), ('button', ButtonEvent), ('configure', ConfigureEvent),
                ('client', ClientEvent), ('pad', C.c_long * 24)]


def run_window():
    x = C.CDLL(find_library('X11') or 'libX11.so.6')
    p, i, u, s = C.c_void_p, C.c_int, C.c_ulong, C.c_char_p
    for name, args, result in (
            ('XOpenDisplay', [s], p), ('XDefaultScreen', [p], i),
            ('XRootWindow', [p, i], u), ('XDefaultVisual', [p, i], p),
            ('XDisplayWidth', [p, i], i), ('XDisplayHeight', [p, i], i),
            ('XCreateSimpleWindow', [p, u, i, i, C.c_uint, C.c_uint, C.c_uint, u, u], u),
            ('XSelectInput', [p, u, C.c_long], i), ('XMapRaised', [p, u], i),
            ('XStoreName', [p, u, s], i), ('XInternAtom', [p, s, i], u),
            ('XChangeProperty', [p, u, u, u, i, i, p, i], i),
            ('XSetWMProtocols', [p, u, C.POINTER(u), i], i),
            ('XPending', [p], i), ('XNextEvent', [p, C.POINTER(XEvent)], i),
            ('XConnectionNumber', [p], i), ('XFlush', [p], i),
            ('XDestroyWindow', [p, u], i), ('XCloseDisplay', [p], i)):
        bind(x, name, args, result)
    display = x.XOpenDisplay(None)
    if not display:
        raise RuntimeError('Cannot open X11 display; check display.name and xauthority')
    painter = None
    surface = context = window = None
    try:
        painter = Painter()
        screen = x.XDefaultScreen(display)
        width, height = WIDTH, HEIGHT
        window = x.XCreateSimpleWindow(display, x.XRootWindow(display, screen),
            max(0, (x.XDisplayWidth(display, screen) - width) // 2),
            max(0, (x.XDisplayHeight(display, screen) - height) // 2), width, height, 0, 0, 0xffffff)
        x.XSelectInput(display, window, (1 << 15) | (1 << 17) | (1 << 2))
        x.XStoreName(display, window, b'UPS Guard - battery warning')
        atom = lambda name: x.XInternAtom(display, name.encode('ascii'), 0)
        close_atom = u(atom('WM_DELETE_WINDOW'))
        x.XSetWMProtocols(display, window, C.byref(close_atom), 1)
        above = u(atom('_NET_WM_STATE_ABOVE'))
        x.XChangeProperty(display, window, atom('_NET_WM_STATE'), 4, 32, 0, C.byref(above), 1)
        surface = painter.lib.cairo_xlib_surface_create(display, window,
                   x.XDefaultVisual(display, screen), width, height)
        context = painter.lib.cairo_create(surface)
        x.XMapRaised(display, window)
        x.XFlush(display)
        print(json.dumps({'event': 'ready'}), flush=True)
        os.set_blocking(sys.stdin.fileno(), False)
        buffer, data, last_frame = b'', None, None
        event = XEvent()
        while True:
            readable, _, _ = select.select([sys.stdin.fileno(), x.XConnectionNumber(display)], [], [], 0.1)
            if sys.stdin.fileno() in readable:
                chunk = os.read(sys.stdin.fileno(), 65536)
                if not chunk:
                    return 0
                buffer += chunk
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    data = json.loads(line.decode('utf-8'))
                    last_frame = None
            while x.XPending(display):
                x.XNextEvent(display, C.byref(event))
                if event.type == 12:
                    last_frame = None
                elif event.type == 22:
                    width, height = max(1, event.configure.width), max(1, event.configure.height)
                    painter.lib.cairo_xlib_surface_set_size(surface, width, height)
                    last_frame = None
                elif event.type == 33 and event.client.data[0] == close_atom.value:
                    print(json.dumps({'event': 'dismiss'}), flush=True)
                    return 0
                elif event.type == 4 and event.button.button == 1:
                    bx, by = event.button.x * WIDTH / width, event.button.y * HEIGHT / height
                    if 329 <= by <= 381:
                        if 28 <= bx <= 418:
                            print(json.dumps({'event': 'stop'}), flush=True)
                            return 0
                        if 438 <= bx <= 652:
                            print(json.dumps({'event': 'dismiss'}), flush=True)
                            return 0
            if data is not None:
                now = time.monotonic()
                frame = panel_text(data, now)
                if frame != last_frame:
                    painter.draw(context, data, now, width, height)
                    painter.lib.cairo_surface_flush(surface)
                    x.XFlush(display)
                    last_frame = frame
    finally:
        if context:
            painter.lib.cairo_destroy(context)
        if surface:
            painter.lib.cairo_surface_destroy(surface)
        if window:
            x.XDestroyWindow(display, window)
        x.XCloseDisplay(display)


if __name__ == '__main__':
    try:
        sys.exit(run_window())
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({'event': 'error', 'message': str(exc)}), flush=True)
        sys.exit(1)
