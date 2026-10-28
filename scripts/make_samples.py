"""生成内置示例数据集（固定随机种子，评测黄金答案依赖确定性）。

产出：
- samples/sales_orders.xlsx        销售台账（订单明细 + 商品主表两个 sheet，含缺失/重复/全空列/文本型数值）
- samples/employee_attendance.xlsx 考勤表（日期文本混杂、工时文本型数字）
- samples/survey.csv               问卷（GBK 编码，验证编码自动识别）
- samples/dirty/merged_cells.xlsx  脏表①合并单元格
- samples/dirty/multi_header.xlsx  脏表②两行表头
- samples/dirty/chinese_cols.csv   脏表③中文列名（GB2312）

运行：.venv/Scripts/python.exe scripts/make_samples.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
DIRTY = SAMPLES / "dirty"

RNG = np.random.default_rng(42)

REGIONS = {
    "华东": ["上海", "杭州", "南京", "苏州"],
    "华南": ["广州", "深圳", "厦门"],
    "华北": ["北京", "天津", "济南"],
    "西南": ["成都", "重庆", "昆明"],
}
PRODUCTS = {
    "智能手表Pro": ("穿戴设备", 899.0),
    "无线耳机Air": ("音频设备", 399.0),
    "蓝牙音箱Mini": ("音频设备", 199.0),
    "充电宝2万mAh": ("配件", 129.0),
    "智能台灯X1": ("智能家居", 249.0),
}
OWNERS = ["张伟", "李娜", "王强", "刘敏", "陈杰", "赵芳", "杨帆", "周琳"]
CHANNELS = ["线上商城", "线下门店", "企业采购"]


def make_sales() -> pd.DataFrame:
    n = 1200
    dates = pd.to_datetime("2024-01-01") + pd.to_timedelta(RNG.integers(0, 365, n), unit="D")
    regions = RNG.choice(list(REGIONS.keys()), n, p=[0.38, 0.27, 0.22, 0.13])
    rows = []
    for i in range(n):
        region = regions[i]
        city = RNG.choice(REGIONS[region])
        product = RNG.choice(list(PRODUCTS.keys()), p=[0.28, 0.26, 0.18, 0.16, 0.12])
        qty = int(RNG.integers(1, 20))
        unit_price = PRODUCTS[product][1]
        month = dates[i].month
        # 销售额：数量×单价 × 季节系数（Q2 旺季）× 区域系数
        season = 1.0 + 0.25 * np.sin((month - 3) / 12 * 2 * np.pi)
        region_factor = {"华东": 1.15, "华南": 1.05, "华北": 0.95, "西南": 0.85}[region]
        # 剧情设计：2024年3月华东骤降（诊断层演示点）
        dip = 0.55 if (region == "华东" and month == 3) else 1.0
        sales = round(qty * unit_price * season * region_factor * dip
                      * float(RNG.uniform(0.85, 1.15)), 2)
        rows.append({
            "订单ID": f"SO2024{i + 1:05d}",
            "日期": dates[i].normalize(),
            "大区": region,
            "城市": city,
            "产品": product,
            "数量": qty,
            "单价": unit_price,
            "销售额": sales,
            "负责人": RNG.choice(OWNERS),
            "渠道": RNG.choice(CHANNELS, p=[0.5, 0.3, 0.2]),
            "折扣率": str(round(float(RNG.choice([0.0, 0.05, 0.1, 0.15])), 2)),  # 文本型数值
            "备注": None,   # 全空列
        })
    df = pd.DataFrame(rows)
    # 埋 3% 销售额缺失 + 12 条重复行
    miss_idx = RNG.choice(n, int(n * 0.03), replace=False)
    df.loc[miss_idx, "销售额"] = np.nan
    dup_rows = df.sample(12, random_state=7)
    df = pd.concat([df, dup_rows], ignore_index=True)
    return df.sample(frac=1.0, random_state=42).reset_index(drop=True)  # 打乱让重复不相邻


def make_products() -> pd.DataFrame:
    rows = [{"产品": p, "品类": c, "成本": round(cost * 0.55, 2),
             "供应商": RNG.choice(["宏达电子", "蓝湾科技", "星辰制造"])}
            for p, (c, cost) in PRODUCTS.items()]
    return pd.DataFrame(rows)


def make_attendance() -> pd.DataFrame:
    n = 560
    employees = [f"E{ i % 28 + 1:03d}" for i in range(n)]
    names = RNG.choice(["王一", "赵二", "钱三", "孙四", "李五", "吴六", "郑七", "王八",
                        "冯九", "陈十", "楚留", "诸葛"], n)
    depts = RNG.choice(["销售部", "技术部", "人事部", "财务部"], n, p=[0.4, 0.3, 0.15, 0.15])
    dates = pd.to_datetime("2024-04-01") + pd.to_timedelta(RNG.integers(0, 120, n), unit="D")
    late = RNG.choice([0, 0, 0, 5, 10, 15, 23, 31], n).astype(int)
    hours = np.round(RNG.normal(8.2, 0.9, n), 1).clip(4, 11)
    df = pd.DataFrame({
        "工号": employees,
        "姓名": names,
        "部门": depts,
        "日期": dates.strftime("%Y/%m/%d"),          # 文本日期（schema 标记"疑似日期文本"）
        "上班打卡": [f"0{RNG.integers(8, 10)}:{RNG.integers(0, 59):02d}" for _ in range(n)],
        "下班打卡": [f"{RNG.integers(17, 21)}:{RNG.integers(0, 59):02d}" for _ in range(n)],
        "工时": [str(h) for h in hours],              # 文本型数字
        "迟到分钟": late,
    })
    miss = RNG.choice(n, 18, replace=False)
    df.loc[miss, "下班打卡"] = None                    # 漏打卡缺失
    return df.sample(frac=1.0, random_state=3).reset_index(drop=True)


def make_survey() -> pd.DataFrame:
    n = 300
    df = pd.DataFrame({
        "问卷ID": [f"Q{i + 1:04d}" for i in range(n)],
        "提交时间": (pd.to_datetime("2024-06-01")
                     + pd.to_timedelta(RNG.integers(0, 30, n), unit="D")).strftime("%Y-%m-%d %H:%M"),
        "年龄段": RNG.choice(["18-25", "26-35", "36-45", "46岁及以上"], n, p=[0.25, 0.4, 0.22, 0.13]),
        "城市": RNG.choice(["上海", "北京", "广州", "成都", "杭州"], n),
        "渠道": RNG.choice(["小程序", "公众号", "线下扫码"], n),
        "满意度": RNG.choice([1, 2, 3, 4, 5], n, p=[0.05, 0.1, 0.25, 0.35, 0.25]),
        "是否愿意推荐": RNG.choice(["愿意", "不愿意", "不确定"], n, p=[0.55, 0.2, 0.25]),
        "建议": RNG.choice(
            ["希望增加更多配色", "物流挺快", "", "客服态度好", "价格略贵", "", "功能齐全", "会推荐给朋友"],
            n),
    })
    return df.sample(frac=1.0, random_state=5).reset_index(drop=True)


def make_dirty_merged() -> pd.DataFrame:
    """两列合并单元格：大区列纵向合并。"""
    from openpyxl import Workbook
    df = pd.DataFrame({
        "大区": ["华东"] * 8 + ["华南"] * 6,
        "城市": ["上海", "杭州", "南京", "苏州", "上海", "杭州", "南京", "苏州",
                 "广州", "深圳", "厦门", "广州", "深圳", "厦门"],
        "销售额": [120, 95, 88, 132, 77, 143, 101, 90, 155, 167, 88, 143, 129, 97],
        "月份": ["1月"] * 14,
    })
    wb = Workbook()
    ws = wb.active
    ws.title = "销售"
    ws.append(list(df.columns))
    for _, row in df.iterrows():
        ws.append(list(row))
    # 合并 A2:A9（华东）、A10:A15（华南）
    ws.merge_cells("A2:A9")
    ws.merge_cells("A10:A15")
    # 把合并区左上之外的值清掉，模拟真实合并单元格
    for r in range(3, 16):
        ws.cell(row=r, column=1, value=None)
    path = DIRTY / "merged_cells.xlsx"
    wb.save(path)
    return path


def make_dirty_multi_header() -> pd.DataFrame:
    """两行表头：第一行是大类，第二行是列名。"""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "指标"
    ws.append(["", "销量", "销量", "销售额", "销售额"])
    ws.append(["地区", "2024Q1", "2024Q2", "2024Q1", "2024Q2"])
    rows = [
        ["华东", 300, 340, 45000, 51000],
        ["华南", 260, 310, 39000, 46500],
        ["华北", 210, 205, 31500, 30750],
        ["西南", 150, 180, 22500, 27000],
    ]
    for r in rows:
        ws.append(r)
    path = DIRTY / "multi_header.xlsx"
    wb.save(path)
    return path


def main() -> None:
    SAMPLES.mkdir(exist_ok=True)
    DIRTY.mkdir(exist_ok=True)

    sales = make_sales()
    products = make_products()
    with pd.ExcelWriter(SAMPLES / "sales_orders.xlsx", engine="openpyxl") as w:
        sales.to_excel(w, sheet_name="订单明细", index=False)
        products.to_excel(w, sheet_name="商品主表", index=False)
    print(f"sales_orders.xlsx：订单明细 {len(sales)} 行，商品主表 {len(products)} 行")

    att = make_attendance()
    att.to_excel(SAMPLES / "employee_attendance.xlsx", sheet_name="考勤记录", index=False)
    print(f"employee_attendance.xlsx：{len(att)} 行")

    survey = make_survey()
    survey.to_csv(SAMPLES / "survey.csv", index=False, encoding="gbk")
    print(f"survey.csv（GBK）：{len(survey)} 行")

    p1 = make_dirty_merged()
    print(f"dirty/merged_cells.xlsx 已生成（{p1.stat().st_size}B）")
    make_dirty_multi_header()
    print("dirty/multi_header.xlsx 已生成")

    cn = pd.DataFrame({
        "商品名称": ["苹果", "香蕉", "橙子", "葡萄", "西瓜"],
        "单价（元）": [8.5, 3.2, 6.0, 12.0, 2.5],
        "库存数量": [120, 300, 88, 45, 26],
        "供应商": ["宏达", "蓝湾", "星辰", "宏达", "蓝湾"],
    })
    cn.to_csv(DIRTY / "chinese_cols.csv", index=False, encoding="gb2312")
    print("dirty/chinese_cols.csv（GB2312，中文列名/括号）已生成")

    # 打印黄金答案参考值（供评测集核对）
    clean = sales["销售额"].sum()
    print("\n── 黄金答案参考（写入 evalset 时核对）──")
    print(f"全年总销售额: {clean:.2f}")
    g = sales.groupby("大区")["销售额"].sum().sort_values(ascending=False)
    print(f"各大区销售额总和:\n{g.round(2)}")
    print(f"订单总数: {len(sales)}")
    top = sales.groupby("产品")["销售额"].sum().sort_values(ascending=False).head(1)
    print(f"销售额最高产品: {top.index[0]} = {top.iloc[0]:.2f}")


if __name__ == "__main__":
    main()
