"""年假计算与 CSV 读写。

规则全部来自 CONTRACT.md 第二节 B（第 10–18 条），这里不额外加规则。
所有天数内部以"半天"为整数单位累计，避免 0.5 反复相加产生浮点误差，
只在最终返回时除以 2。
"""

import csv
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

FULL_YEARS = 5
YEARLY_DAYS = 15
CARRY_MAX_DAYS = 5
CARRY_DEADLINE = (3, 31)
HEADER = ["employee_no", "name", "hire_date", "leave_start_date", "days"]


def _half(days):
    """把天数换成整数半天，按 0.5 天为最小单位四舍五入。"""
    return int((Decimal(str(days)) * 2).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _full_years(hire, at):
    """按入职周年算已满年数；2/29 入职在平年按 3/1 才满周年。"""
    years = at.year - hire.year
    if (at.month, at.day) < (hire.month, hire.day):
        years -= 1
    return years


def remaining(hire_date, leaves, as_of):
    """剩余年假天数；hire_date / as_of 是 "YYYY-MM-DD"，leaves 是 [(开始日, 天数)]。"""
    hire = date.fromisoformat(hire_date)
    query = date.fromisoformat(as_of)

    # 第 10 条：司龄按查询日算，不满 5 年一律 0 天。
    if _full_years(hire, query) < FULL_YEARS:
        return 0.0

    # 第 11 条：查询日满 5 年即给满当年 15 天，不按月折算。
    quota = _half(YEARLY_DAYS)

    prev = query.year - 1
    used_now = 0
    used_prev = 0
    for start, days in leaves:
        start_date = date.fromisoformat(start)
        half = _half(days)
        # 第 16 条：按请假开始日所在年度整条计入。
        if start_date.year == query.year:
            used_now += half
        elif start_date.year == prev:
            used_prev += half

    # 第 12 条：去年额度按去年 12/31 的司龄算。
    prev_quota = quota if _full_years(hire, date(prev, 12, 31)) >= FULL_YEARS else 0

    # 第 13、14 条：去年超休不倒扣今年，最多结转 5 天。
    carry = min(max(0, prev_quota - used_prev), _half(CARRY_MAX_DAYS))

    # 第 15 条：结转含 3/31 当天，4/1 起作废。
    if (query.month, query.day) > CARRY_DEADLINE:
        carry = 0

    # 第 17 条：超休输出 0，不返回负数。
    return max(0, quota + carry - used_now) / 2


def load_records(path):
    """读 CSV 返回 {员工号: {"name", "hire_date", "leaves"}}。

    格式错误抛 ValueError，消息里带 "第 N 行"（表头是第 1 行）。
    """
    records = {}
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header != HEADER:
            raise ValueError("第 1 行: 表头必须是 " + ",".join(HEADER))

        line = 1
        for row in reader:
            line += 1
            if not any(cell.strip() for cell in row):
                continue
            if len(row) != len(HEADER):
                raise ValueError(f"第 {line} 行: 列数不是 {len(HEADER)}")

            emp, name, hire, leave_day, days = [cell.strip() for cell in row]
            if not emp:
                raise ValueError(f"第 {line} 行: employee_no 为空")
            if not name:
                raise ValueError(f"第 {line} 行: name 为空")
            try:
                hire_date = date.fromisoformat(hire)
            except ValueError:
                raise ValueError(f"第 {line} 行: hire_date 非法") from None

            if not leave_day and not days:
                record = records.setdefault(
                    emp, {"name": name, "hire_date": hire_date, "leaves": []}
                )
                _check_same(record, name, hire_date, line)
                continue
            if not leave_day or not days:
                raise ValueError(f"第 {line} 行: leave_start_date 与 days 必须同时为空或同时有值")

            try:
                start = date.fromisoformat(leave_day)
            except ValueError:
                raise ValueError(f"第 {line} 行: leave_start_date 非法") from None
            try:
                value = float(days)
            except ValueError:
                raise ValueError(f"第 {line} 行: days 不是数字") from None
            # 第 5 条：0.5 的正整数倍。
            if value <= 0 or abs(value * 2 - round(value * 2)) > 1e-9:
                raise ValueError(f"第 {line} 行: days 必须是 0.5 的正整数倍")

            record = records.setdefault(
                emp, {"name": name, "hire_date": hire_date, "leaves": []}
            )
            _check_same(record, name, hire_date, line)
            record["leaves"].append((start, value))
    return records


def _check_same(record, name, hire_date, line):
    # 第 7 条：同一员工号的 name 与 hire_date 必须一致。
    if record["name"] != name:
        raise ValueError(f"第 {line} 行: 与同一员工号前面的 name 不一致")
    if record["hire_date"] != hire_date:
        raise ValueError(f"第 {line} 行: 与同一员工号前面的 hire_date 不一致")


def query(employee_no, as_of, records):
    """查一个人；查无此人返回 None。"""
    record = records.get(employee_no.strip())
    if record is None:
        return None
    year = date.fromisoformat(as_of).year
    # 第 16 条：已休 = 查询日年度内开始的请假之和。
    used = sum(days for start, days in record["leaves"] if start.year == year)
    rem = remaining(
        record["hire_date"].isoformat(),
        [(start.isoformat(), days) for start, days in record["leaves"]],
        as_of,
    )
    return {
        "employee_no": employee_no.strip(),
        "name": record["name"],
        "used_days": used,
        "remaining_days": rem,
    }
