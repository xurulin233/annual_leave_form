#!/usr/bin/env python3
"""验收：按 CONTRACT.md 第四段判第三节 16 条。

用法：python3 acceptance.py
退出码：0 全对 / 1 有不对 / 2 还没有实现

期望值全部来自 project/data/expected.csv，本文件不写死任何一个数字；
本文件也不从被测实现里反推期望值。
"""

import csv
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urljoin
from urllib.request import HTTPRedirectHandler, Request, build_opener

BASE = Path(__file__).resolve().parent
PROJECT = BASE / "project"
DATA_DIR = PROJECT / "data"
EXPECTED = DATA_DIR / "expected.csv"
SAMPLE = DATA_DIR / "leave.csv"
KEY_FILE = PROJECT / "key.txt"
APP = PROJECT / "app.py"
LEAVE_PY = PROJECT / "leave.py"

START_TIMEOUT = 5.0
HTTP_TIMEOUT = 5.0

try:
    from zoneinfo import ZoneInfo

    _TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001 - 没有 tzdata 时退回固定 +08:00，仍然是同一时区
    _TZ = timezone(timedelta(hours=8))

TODAY = datetime.now(_TZ).date()


class _NoRedirect(HTTPRedirectHandler):
    """关掉自动跟随：302 本身就是要判的结果。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirect)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fmt(n):
    """与页面口径一致：15 / 12.5。"""
    return f"{float(n):g}"


def http(method, url, body=None, headers=None, timeout=HTTP_TIMEOUT):
    req = Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8", "replace")
    except OSError as exc:
        return 0, {}, f"连接失败: {exc}"


def multipart(fields, filename, payload):
    boundary = "----accept" + os.urandom(8).hex()
    chunks = []
    for name, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    chunks.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: text/csv\r\n\r\n".encode()
        + payload
        + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def parse_leaves(text):
    leaves = []
    for item in (text or "").split("|"):
        item = item.strip()
        if not item:
            continue
        day, days = item.split(":", 1)
        leaves.append((date.fromisoformat(day.strip()), float(days.strip())))
    return leaves


def start_server(data_path):
    """起 app.py，返回 (proc, port)；失败返回 (None, 日志)。"""
    proc = subprocess.Popen(
        [sys.executable, str(APP), "--port", "0", "--data", str(data_path)],
        cwd=str(PROJECT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    box = {}
    lines = []

    def reader():
        for line in proc.stdout:
            lines.append(line.rstrip())
            if line.startswith("LISTENING"):
                box["port"] = line.split()[-1].strip()
                return

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(START_TIMEOUT)
    if "port" not in box:
        proc.kill()
        proc.wait(timeout=5)
        return None, "\n".join(lines[-10:])
    return proc, box["port"]


def placeholders(leave, records, employee_no):
    values = {"year": str(TODAY.year), "used": "?", "remaining": "?"}
    if employee_no and employee_no in records:
        row = leave.query(employee_no, TODAY.isoformat(), records)
        if row:
            values["used"] = fmt(row["used_days"])
            values["remaining"] = fmt(row["remaining_days"])
    return values


def render(fragment, values):
    for key, value in values.items():
        fragment = fragment.replace("{" + key + "}", value)
    return fragment


def contains_all(text, fragments, values):
    missing = []
    for raw in fragments:
        piece = render(raw.strip(), values)
        if piece and piece not in text:
            missing.append(piece)
    return missing


def contains_none(text, fragments, values):
    found = []
    for raw in fragments:
        piece = render(raw.strip(), values)
        if piece and piece in text:
            found.append(piece)
    return found


def report(no, desc, got, want):
    ok = got == want
    mark = "对" if ok else "错"
    print(f" {no}. {desc}\n    得到 {got}   期望 {want}   [{mark}]")
    return ok


def run_numeric(leave, records, row):
    no = row["编号"].strip()
    emp = row["员工号"].strip()
    as_of = date.fromisoformat(row["查询日"].strip())
    leaves = parse_leaves(row["请假记录"])
    hire = records[emp]["hire_date"]
    got_used = sum(days for start, days in leaves if start.year == as_of.year)
    got_rem = leave.remaining(
        hire.isoformat(), [(s.isoformat(), d) for s, d in leaves], as_of.isoformat()
    )
    want_used = float(row["期望已休"])
    want_rem = float(row["期望剩余"])
    ok = report(no, f"{emp} {as_of} 休 {fmt(got_used)} 天", fmt(got_used), fmt(want_used))
    ok = report(f"{no}b", f"{emp} {as_of} 剩余", fmt(got_rem), fmt(want_rem)) and ok
    return ok


def run_page(leave, records, row, port):
    no = row["编号"].strip()
    target = row["员工号"].strip()
    literal = target.startswith(("GET ", "POST "))
    values = placeholders(leave, records, "" if literal else target)
    if literal:
        method, path = target.split(None, 1)
        url = f"http://127.0.0.1:{port}{path}"
    else:
        method = "GET"
        url = f"http://127.0.0.1:{port}/?employee_no={quote(target)}"
    status, headers, text = http(method, url)
    if 300 <= status < 400 and "Location" in headers:
        status, _, text = http("GET", urljoin(url, headers["Location"]))

    ok = report(no, f"{method} {target or '(空员工号)'} 状态", status, int(row["期望状态"]))
    missing = contains_all(text, (row["必须包含"] or "").split("|"), values)
    if missing:
        ok = report(f"{no}b", "页面缺少片段", missing, []) and ok
    extra = contains_none(text, (row["必须不包含"] or "").split("|"), values)
    if extra:
        ok = report(f"{no}c", "页面不该出现", extra, []) and ok
    return ok


def run_upload(row, port, data_copy, test_key):
    no = row["编号"].strip()
    want_status = int(row["期望状态"])
    filename = row["上传文件"].strip()
    raw = (DATA_DIR / filename).read_bytes()
    key = f"wrong-{os.urandom(4).hex()}" if want_status == 403 else test_key
    body, ctype = multipart({"key": key}, filename, raw)
    before = sha256(data_copy)
    status, headers, text = http(
        "POST", f"http://127.0.0.1:{port}/upload", body, {"Content-Type": ctype}
    )
    page = text
    if 300 <= status < 400 and "Location" in headers:
        _, _, page = http("GET", urljoin(f"http://127.0.0.1:{port}/", headers["Location"]))
    after = sha256(data_copy)

    ok = report(no, f"上传 {filename} 状态", status, want_status)
    values = {"year": str(TODAY.year), "used": "?", "remaining": "?"}
    missing = contains_all(page, (row["必须包含"] or "").split("|"), values)
    if missing:
        ok = report(f"{no}b", "响应缺少片段", missing, []) and ok
    if (row["文件不变"] or "").strip() == "1":
        ok = report(f"{no}c", "数据文件哈希未变", after == before, True) and ok
    return ok


def main():
    absent = [p for p in (APP, LEAVE_PY, EXPECTED) if not p.exists()]
    if absent:
        for path in absent:
            print(f"缺 {path}")
        print("还没有实现：等 project/app.py、project/leave.py、data/expected.csv 就位。")
        return 2

    try:
        sys.path.insert(0, str(PROJECT))
        import leave  # noqa: PLC0415 - 必须先把 project/ 放进 sys.path
    except Exception:  # noqa: BLE001 - 实现方刚起步，import 失败按"未实现"处理
        traceback.print_exc()
        print("还没有实现：project/leave.py 导不进来。")
        return 2

    rows = list(csv.DictReader(EXPECTED.open(encoding="utf-8-sig")))
    numeric = [r for r in rows if r["类型"].strip() == "数值"]
    pages = [r for r in rows if r["类型"].strip() == "页面"]
    uploads = [r for r in rows if r["类型"].strip() == "上传"]

    print("=== 验收 ===")
    failed = 0
    try:
        records = leave.load_records(str(SAMPLE))
    except Exception:  # noqa: BLE001 - 样本都读不了，按"未实现"处理
        traceback.print_exc()
        print("还没有实现：leave.load_records 读不了 data/leave.csv。")
        return 2

    for row in numeric:
        try:
            if not run_numeric(leave, records, row):
                failed += 1
        except Exception:  # noqa: BLE001 - 单条异常记失败，不中断其余
            traceback.print_exc()
            print(f" {row.get('编号', '?')}. 抛异常，算失败")
            failed += 1

    tmpdir = Path(tempfile.mkdtemp(prefix="al_accept_"))
    data_copy = tmpdir / "leave.csv"
    shutil.copyfile(SAMPLE, data_copy)
    key_existed = KEY_FILE.exists()
    key_backup = KEY_FILE.read_bytes() if key_existed else None
    test_key = os.urandom(24).hex()
    KEY_FILE.write_text(test_key + "\n", encoding="utf-8")

    proc = None
    try:
        proc, port = start_server(data_copy)
        if proc is None:
            print("还没有实现：app.py 没有打印 LISTENING。")
            print(port)
            return 2

        for row in pages:
            try:
                if not run_page(leave, records, row, port):
                    failed += 1
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                print(f" {row.get('编号', '?')}. 抛异常，算失败")
                failed += 1

        for row in uploads:
            try:
                if not run_upload(row, port, data_copy, test_key):
                    failed += 1
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                print(f" {row.get('编号', '?')}. 抛异常，算失败")
                failed += 1
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if key_existed:
            KEY_FILE.write_bytes(key_backup)
        else:
            KEY_FILE.unlink(missing_ok=True)
        shutil.rmtree(tmpdir, ignore_errors=True)

    total = len(numeric) + len(pages) + len(uploads)
    print()
    if failed:
        print(f"=== {failed} / {total} 条不对 ===")
        return 1
    print(f"=== {total} 条全对 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
