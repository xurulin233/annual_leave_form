"""年假查询 Web 服务：首页查询、结果页、带 key 的 CSV 上传。

行为按 CONTRACT.md 第一、二节；唯一写入路径是 POST /upload。
"""

import argparse
import os
import signal
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from email.parser import BytesParser
from email.policy import default as EMAIL_POLICY
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import leave

BASE = Path(__file__).resolve().parent
KEY_FILE = BASE / "key.txt"

# 上传成功提示：只保留最近一条，被下一次 GET / 消费后清空（第 34 条）。
FLASH = {"msg": None}
FLASH_LOCK = threading.Lock()

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
except (ModuleNotFoundError, OSError, KeyError):  # 没有 tzdata 时退回固定 +08:00，和规则 19 是同一个时区
    TZ = timezone(timedelta(hours=8))


def today():
    # 第 19 条：查询日取后台服务器当日，Asia/Shanghai。
    return datetime.now(TZ).date()


def fmt_days(value):
    # 第 26 条：整数不带小数点，半天带一位。
    return f"{float(value):g}"


STYLE = """<style>
body{font-family:system-ui,sans-serif;margin:2rem;max-width:46rem}
#upload{position:fixed;top:12px;right:12px;border:1px solid #ccc;border-radius:6px;
        padding:10px 12px;background:#fafafa;font-size:14px}
#upload button{margin-left:6px}
#flash{background:#e7f6e7;border:1px solid #8c8;border-radius:4px;padding:8px 12px;margin-bottom:1rem}
h1{font-size:1.3rem}
.year{font-size:1.2rem;font-weight:600;margin:1rem 0 .5rem}
.row{margin:.2rem 0}
</style>"""

UPLOAD_BOX = """<div id="upload">
<form id="upform" method="post" action="/upload" enctype="multipart/form-data">
<input type="file" name="file" id="file" accept=".csv">
<input type="hidden" name="key" id="key">
<button type="button" onclick="doUpload()">上传</button>
</form>
</div>
<script>
function doUpload(){
  var f=document.getElementById('file');
  if(!f.files.length){alert('请选择 CSV 文件');return;}
  var k=prompt('请输入 key');
  if(k===null){return;}
  document.getElementById('key').value=k;
  document.getElementById('upform').submit();
}
</script>"""


def page(body):
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>年假查询</title>{STYLE}</head><body>{UPLOAD_BOX}{body}</body></html>"
    )


def home_body(msg=None):
    flash = ""
    if msg:
        # 第 34 条：显示一次即清；3 秒后自动隐藏。
        flash = (
            f'<div id="flash">{escape(msg)}</div>'
            "<script>setTimeout(function(){var e=document.getElementById('flash');"
            "if(e){e.style.display='none';}},3000);</script>"
        )
    form = (
        '<h1>年假查询</h1>'
        '<form method="get" action="/">'
        '员工号：<input name="employee_no" autofocus> '
        '<button type="submit">查询</button>'
        "</form>"
        '<p class="hint">例如：0001</p>'
    )
    return page(flash + form)


def result_body(employee_no, name, used, rem):
    # 第 20 条：第一行年份，其后只显示四项；不显示入职日期。
    return page(
        f'<div class="year">{today().year} 年</div>'
        f'<div class="row">员工号：{escape(employee_no)}</div>'
        f'<div class="row">姓名：{escape(name)}</div>'
        f'<div class="row">已休 {fmt_days(used)} 天</div>'
        f'<div class="row">剩余 {fmt_days(rem)} 天</div>'
    )


def notice_body(text):
    return page(f'<div class="row">{escape(text)}</div>')


class Handler(BaseHTTPRequestHandler):
    server_version = "AnnualLeave/1.0"
    data_path = None

    def log_message(self, *args):
        # 第 5 节：不写日志文件；控制台也不刷请求日志。
        pass

    def send_page(self, status, html, headers=None):
        payload = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def load(self):
        return leave.load_records(str(self.data_path))

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path != "/":
            return self.send_page(404, notice_body("404 页面不存在"))

        params = parse_qs(parts.query, keep_blank_values=True)
        if "employee_no" not in params:
            with FLASH_LOCK:
                message, FLASH["msg"] = FLASH["msg"], None
            return self.send_page(200, home_body(message))

        employee_no = params["employee_no"][0].strip()
        if not employee_no:
            return self.send_page(200, notice_body("请输入员工号"))

        row = leave.query(employee_no, today().isoformat(), self.load())
        if row is None:
            return self.send_page(200, notice_body(f"查无此人：{employee_no}"))
        return self.send_page(
            200,
            result_body(employee_no, row["name"], row["used_days"], row["remaining_days"]),
        )

    def do_POST(self):
        if urlsplit(self.path).path != "/upload":
            return self.send_page(405, notice_body("405 只支持 POST /upload"))
        return self.upload()

    def upload(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        fields = self.parse_multipart(self.headers.get("Content-Type", ""), body)

        key = fields.get("key", (None, b""))[1].decode("utf-8", "replace").strip()
        filename, payload = fields.get("file", (None, b""))

        # 第 31 条：每次上传重新读 key.txt；缺失/为空/不等一律 403。
        try:
            real_key = KEY_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            real_key = ""
        if not real_key or key != real_key:
            return self.send_page(403, notice_body("key 不正确"))

        if filename is None:
            return self.send_page(400, notice_body("第 1 行: 缺少上传文件"))

        data_dir = Path(self.data_path).parent
        handle, tmp_name = tempfile.mkstemp(dir=str(data_dir), suffix=".upload")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
            try:
                records = leave.load_records(str(tmp))
            except ValueError as exc:
                # 第 32 条：校验不过 400，原数据文件不动。
                return self.send_page(400, notice_body(str(exc)))

            # 第 34 条：N = 请假记录数（只登记员工的行不计）。
            count = sum(len(item["leaves"]) for item in records.values())
            try:
                os.replace(tmp, self.data_path)
            except OSError:
                # 第 33 条：替换失败 500，原文件不变。
                return self.send_page(500, notice_body("数据文件替换失败"))
        finally:
            tmp.unlink(missing_ok=True)

        with FLASH_LOCK:
            FLASH["msg"] = f"上传成功，共 {count} 条"
        # 第 34 条：302 回首页，URL 不带参数。
        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    @staticmethod
    def parse_multipart(content_type, body):
        head = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        message = BytesParser(policy=EMAIL_POLICY).parsebytes(head + body)
        fields = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name:
                fields[name] = (part.get_filename(), part.get_payload(decode=True) or b"")
        return fields

    def do_PUT(self):
        self.send_page(405, notice_body("405"))

    do_DELETE = do_PATCH = do_PUT


def main():
    parser = argparse.ArgumentParser(description="年假查询服务")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data", default="data/leave.csv")
    args = parser.parse_args()

    data_path = Path(args.data)
    if not data_path.is_absolute():
        data_path = BASE / data_path

    try:
        leave.load_records(str(data_path))
    except ValueError as exc:
        print(f"数据错误: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"数据错误: 读不了 {data_path}: {exc}", file=sys.stderr)
        return 2

    Handler.data_path = data_path
    httpd = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"LISTENING {httpd.server_address[1]}", flush=True)

    def stop(signum, frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
