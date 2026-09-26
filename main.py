# main.py
# Ứng dụng Kivy: chụp toàn bộ màn hình Android (kể cả web, app khác) bằng
# MediaProjection API (gọi qua Pyjnius) và phát dạng MJPEG qua HTTP để
# TV mở bằng trình duyệt (Samsung Internet) xem trực tiếp.
#
# LƯU Ý QUAN TRỌNG - đọc trước khi build:
# 1. Đây vẫn phải gọi API hệ thống Android (MediaProjection) - không có
#    cách nào chụp màn hình toàn hệ thống mà bỏ qua API này, dù ngôn ngữ nào.
# 2. Android BẮT BUỘC hiện dialog "Bắt đầu ghi màn hình?" mỗi lần - không
#    thể tự động hoá hay ẩn đi.
# 3. GIỚI HẠN LỚN NHẤT của bản thuần Python/Kivy này: nếu không chạy trong
#    một Foreground Service riêng, việc chụp màn hình có thể NGỪNG khi bạn
#    thoát khỏi app này để mở web/app khác trên điện thoại - tức là đúng lúc
#    bạn cần xem nhất thì mất hình. Bản demo dưới đây chạy trực tiếp trong
#    Activity để đơn giản hoá; nếu gặp vấn đề này, cần thêm một Foreground
#    Service viết riêng (phức tạp hơn nữa trong python-for-android) - hỏi lại
#    tôi nếu bạn cần bản đó.

import io
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kivy.app import App
from kivy.clock import Clock
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label

from jnius import autoclass, cast, PythonJavaClass, java_method
from android import activity  # cung cấp bởi python-for-android

PORT = 8080
REQUEST_CODE = 1001
RESULT_OK = -1  # Activity.RESULT_OK

# --- Các lớp Java cần dùng, load qua Pyjnius ---
PythonActivity = autoclass('org.kivy.android.PythonActivity')
Context = autoclass('android.content.Context')
MediaProjectionManager = autoclass('android.media.projection.MediaProjectionManager')
ImageReaderClass = autoclass('android.media.ImageReader')
PixelFormat = autoclass('android.graphics.PixelFormat')
DisplayManager = autoclass('android.hardware.display.DisplayManager')
Handler = autoclass('android.os.Handler')
Looper = autoclass('android.os.Looper')

activity_instance = PythonActivity.mActivity

# Trạng thái dùng chung giữa luồng chụp ảnh và luồng HTTP server
_state = {
    'width': 720,
    'height': 1280,
    'frame': None,
    'lock': threading.Lock(),
    'projection': None,
    'reader': None,
    'virtual_display': None,
}

status_label = None  # gán trong build()


class ImageAvailableListener(PythonJavaClass):
    """Cầu nối để Java gọi ngược vào Python mỗi khi có khung hình mới."""
    __javainterfaces__ = ['android/media/ImageReader$OnImageAvailableListener']
    __javacontext__ = 'app'

    @java_method('(Landroid/media/ImageReader;)V')
    def onImageAvailable(self, reader):
        image = reader.acquireLatestImage()
        if image is None:
            return
        try:
            width = _state['width']
            height = _state['height']

            plane = image.getPlanes()[0]
            buffer = plane.getBuffer()
            pixel_stride = plane.getPixelStride()
            row_stride = plane.getRowStride()
            row_padding = row_stride - pixel_stride * width

            raw = bytearray(buffer.remaining())
            buffer.get(raw)  # đọc dữ liệu thô từ ByteBuffer sang bytearray Python

            from PIL import Image as PILImage
            padded_width = width + row_padding // pixel_stride
            img = PILImage.frombuffer(
                'RGBA', (padded_width, height), bytes(raw), 'raw', 'RGBA', 0, 1
            )
            if row_padding:
                img = img.crop((0, 0, width, height))

            out = io.BytesIO()
            img.convert('RGB').save(out, format='JPEG', quality=50)

            with _state['lock']:
                _state['frame'] = out.getvalue()
        except Exception as e:
            print('[ScreenMirror] Loi encode frame:', e)
        finally:
            image.close()


_listener_holder = []  # giữ tham chiếu listener để không bị garbage-collected


def start_projection(_instance):
    mgr = cast(
        MediaProjectionManager,
        activity_instance.getSystemService(Context.MEDIA_PROJECTION_SERVICE),
    )
    intent = mgr.createScreenCaptureIntent()
    activity.bind(on_activity_result=on_activity_result)
    activity_instance.startActivityForResult(intent, REQUEST_CODE)


def on_activity_result(request_code, result_code, data):
    if request_code != REQUEST_CODE:
        return
    if result_code != RESULT_OK:
        _set_status('Ban da tu choi quyen chia se man hinh')
        return

    mgr = cast(
        MediaProjectionManager,
        activity_instance.getSystemService(Context.MEDIA_PROJECTION_SERVICE),
    )
    projection = mgr.getMediaProjection(result_code, data)
    _state['projection'] = projection

    setup_capture(projection)
    start_http_server()
    _set_status(f'Dang phat tai:\nhttp://{get_local_ip()}:{PORT}\n\nMo dia chi nay bang Samsung Internet tren TV')


def setup_capture(projection):
    metrics = activity_instance.getResources().getDisplayMetrics()
    density = metrics.densityDpi
    width = 720
    height = int(720 * metrics.heightPixels / metrics.widthPixels)
    _state['width'] = width
    _state['height'] = height

    reader = ImageReaderClass.newInstance(width, height, PixelFormat.RGBA_8888, 2)
    listener = ImageAvailableListener()
    _listener_holder.append(listener)
    main_handler = Handler(Looper.getMainLooper())
    reader.setOnImageAvailableListener(listener, main_handler)
    _state['reader'] = reader

    virtual_display = projection.createVirtualDisplay(
        'PyScreenMirror', width, height, density,
        DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
        reader.getSurface(), None, None,
    )
    _state['virtual_display'] = virtual_display


class StreamHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/stream'):
            self.send_response(200)
            self.send_header(
                'Content-Type', 'multipart/x-mixed-replace; boundary=frame'
            )
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            try:
                while True:
                    with _state['lock']:
                        frame = _state['frame']
                    if frame:
                        self.wfile.write(b'--frame\r\n')
                        self.wfile.write(b'Content-Type: image/jpeg\r\n')
                        self.wfile.write(
                            f'Content-Length: {len(frame)}\r\n\r\n'.encode()
                        )
                        self.wfile.write(frame)
                        self.wfile.write(b'\r\n')
                    threading.Event().wait(0.07)  # ~14 fps
            except Exception:
                pass  # client (TV) ngat ket noi
        else:
            html = (
                '<!DOCTYPE html><html><head><meta charset="utf-8">'
                '<style>html,body{margin:0;background:#000;height:100%;overflow:hidden;}'
                'img{width:100vw;height:100vh;object-fit:contain;display:block;}</style>'
                '</head><body><img src="/stream"></body></html>'
            ).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(html)))
            self.end_headers()
            self.wfile.write(html)

    def log_message(self, format, *args):
        pass  # tat log mac dinh cho do roi


def start_http_server():
    server = ThreadingHTTPServer(('0.0.0.0', PORT), StreamHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return 'khong xac dinh'


def _set_status(text):
    def _update(_dt):
        if status_label:
            status_label.text = text
    Clock.schedule_once(_update, 0)


class ScreenMirrorApp(App):
    def build(self):
        global status_label
        layout = BoxLayout(orientation='vertical', padding=24, spacing=16)
        status_label = Label(
            text=f'IP: {get_local_ip()}\nNhan nut de bat dau', halign='center'
        )
        btn = Button(
            text='Bat dau chia se man hinh', size_hint_y=None, height=100
        )
        btn.bind(on_release=start_projection)
        layout.add_widget(status_label)
        layout.add_widget(btn)
        return layout


if __name__ == '__main__':
    ScreenMirrorApp().run()
