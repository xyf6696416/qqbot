import os
import sys

# Windows GBK 终端兼容
if sys.platform == 'win32' and sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

# ================= 配置区 =================
PORT = 7777  # 服务端口
IMAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tc")  # 图片根目录
ALLOWED_EXT = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')  # 允许的文件类型


# ==========================================

class AdvancedImageHandler(SimpleHTTPRequestHandler):
    """增强型图片请求处理器"""

    def do_GET(self):
        try:
            # 解码URL路径
            decoded_path = unquote(self.path)
            print(f"\n🔧 收到请求: {decoded_path}")

            # 处理根路径请求
            if decoded_path == '/':
                self.show_welcome_page()
                return

            # 验证并获取文件路径
            full_path = self.get_validated_path(decoded_path)
            if not full_path:
                return  # 错误已处理

            # 发送文件
            self.send_image_file(full_path)

        except Exception as e:
            # 修复错误信息编码问题
            error_msg = f"服务器内部错误: {str(e)}".encode('utf-8', 'replace').decode('latin-1')
            self.send_error(500, error_msg)

    def send_error(self, code, message=None, explain=None):
        """重写错误处理方法以支持UTF-8"""
        try:
            # 将错误信息编码为UTF-8字节后解码为latin-1字符串
            safe_message = message.encode('utf-8', 'replace').decode('latin-1') if message else ""
            super().send_error(code, message=safe_message, explain=explain)
        except UnicodeEncodeError:
            # 二次保险：纯ASCII错误信息
            super().send_error(code, message="Internal Server Error", explain="Check server logs")

    def get_validated_path(self, decoded_path):
        """安全验证文件路径"""
        try:
            # 去掉前导 /，直接拼接到图片根目录
            rel_path = decoded_path.lstrip('/')
            full_path = os.path.abspath(os.path.join(IMAGE_DIR, rel_path))

            # 防止路径穿越
            if not full_path.startswith(IMAGE_DIR):
                self.send_error(403, "禁止访问上级目录")
                return None

            # 检查文件存在性
            if not os.path.exists(full_path):
                self.send_error(404, f"文件不存在: {rel_path}")
                return None

            # 验证是否为文件
            if not os.path.isfile(full_path):
                self.send_error(400, "仅支持文件访问")
                return None

            # 验证文件类型
            file_ext = os.path.splitext(full_path)[1].lower()
            if file_ext not in ALLOWED_EXT:
                self.send_error(403, f"不支持的文件类型: {file_ext}")
                return None

            return full_path

        except ValueError:
            self.send_error(400, "非法路径格式")
            return None

    def send_image_file(self, full_path):
        """安全发送图片文件"""
        try:
            file_size = os.path.getsize(full_path)
            print(f"📦 准备发送文件: {full_path} ({file_size} bytes)")

            self.send_response(200)
            self.send_header('Content-Type', self.guess_type(full_path))
            self.send_header('Content-Length', str(file_size))
            self.send_header('Connection', 'close')
            self.end_headers()

            # 分块发送文件内容
            with open(full_path, 'rb') as f:
                while chunk := f.read(8192):  # 8KB分块
                    self.wfile.write(chunk)

            print("✅ 文件发送完成")

        except ConnectionAbortedError:
            print("⚠️ 客户端提前终止连接")
        except Exception as e:
            print(f"❌ 文件发送失败: {str(e)}")
            self.send_error(500, "文件传输中断")

    def show_welcome_page(self):
        """显示欢迎页面"""
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()

        welcome_html = f"""
        <html>
            <head>
                <title>本地图床服务</title>
                <style>
                    body {{ 
                        font-family: Arial, sans-serif; 
                        margin: 40px;
                        background-color: #f0f0f0;
                    }}
                    .container {{ 
                        max-width: 800px;
                        margin: 0 auto;
                        padding: 20px;
                        background: white;
                        border-radius: 8px;
                        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                    }}
                    code {{ 
                        background: #f8f8f8;
                        padding: 2px 5px;
                        border-radius: 3px;
                    }}
                </style>
            </head>
            <body>
                <div class="container">
                    <h1>🖼️ 本地图床服务已运行</h1>
                    <p>访问示例：<br>
                    <code>http://localhost:{PORT}/图片路径/文件名.jpg</code></p>

                    <h2>基本信息</h2>
                    <ul>
                        <li>服务端口：{PORT}</li>
                        <li>文件根目录：<code>{IMAGE_DIR}</code></li>
                        <li>支持格式：{', '.join(ALLOWED_EXT)}</li>
                    </ul>
                </div>
            </body>
        </html>
        """
        self.wfile.write(welcome_html.encode('utf-8'))


def run_server():
    """启动HTTP服务"""
    print(f"""
    🚀 本地图床服务启动成功！
    📂 文件根目录：{IMAGE_DIR}
    🌐 访问地址：http://localhost:{PORT}

    按 Ctrl+C 停止服务
    """)

    # 创建图片目录（如果不存在）
    os.makedirs(IMAGE_DIR, exist_ok=True)

    # 启动服务器
    server = ThreadingHTTPServer(('', PORT), AdvancedImageHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 服务已停止")
        server.server_close()


if __name__ == '__main__':
    # Windows系统端口占用检查
    if sys.platform == 'win32':
        try:
            import socket

            test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_socket.bind(('', PORT))
            test_socket.close()
        except OSError:
            print(f"❌ 端口 {PORT} 被占用，请执行：")
            print(f"taskkill /f /im python.exe")
            sys.exit(1)

    run_server()
