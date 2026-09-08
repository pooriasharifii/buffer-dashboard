"""
داشبورد موجودی بافر گارانتی ۳ نماینده
---------------------------------------
هر بار که صفحه رفرش میشه، مستقیم و زنده (Live) از گوگل شیت هر نماینده خونده میشه
(بدون نیاز به Google API/احراز هویت، چون شیت‌ها Public + Editor هستن).

هر شیت باید سلول A102 رو داشته باشه که با یه اسکریپت onEdit، زمان آخرین تغییر
هر ردیف از ردیف ۳ به بعد رو توش ثبت می‌کنه (طبق چیزی که خودت تنظیم کردی).

اجرا:
    pip install streamlit pandas altair
    streamlit run buffer_inventory_dashboard.py
"""

import html
import io
import re
from datetime import datetime

import altair as alt
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="داشبورد بافر گارانتی", page_icon="📦", layout="wide")

# اسم نماینده -> Sheet ID
SHEETS = {
    "Dragonshop": "1PcM6_fmIyTbzdS-BNTCj9nC8ZHRWD3O7V-VuGyJh6co",
    "Kianpour": "1Hy0LSBQ1k8D8U-Hs_7XMiG4kxSB0o3wh7NlZ-8ZjFYY",
    "Mirzaee": "1aS3GjDRCtWewSBq2deJxvSfFEWD5XUqYnBwOXJalhVE",
}
REP_COLORS = {"Dragonshop": "#3b82f6", "Kianpour": "#8b5cf6", "Mirzaee": "#10b981"}
REP_ABBR = {"Dragonshop": "DS", "Kianpour": "KP", "Mirzaee": "MZ"}

DATE_COL_PATTERN = re.compile(r"موجود[یي]\s*[\d./\-]+")
DATE_KEY_PATTERN = re.compile(r"(\d{4})[./\-](\d{2})[./\-](\d{2})")
LAST_EDIT_ROW_INDEX = 101  # raw.iloc[101] == سطر ۱۰۲ در گوگل‌شیت (چون ردیف اول هدر است)

# ---------------------------------------------------------------------------
# استایل — نکته مهم: direction:rtl فقط روی متن/جدول‌های خودمون اعمال میشه،
# نه روی کل صفحه، چون RTL کردن کل اپ باعث می‌شد نمودار Altair کج و ریز بشه.
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;600;700;800&display=swap');

    html, body, [class*="css"] {
        font-family: 'Vazirmatn', Tahoma, sans-serif !important;
        font-size: 16px;
    }
    .block-container { padding-top: 1.5rem; }

    .rtl-text { direction: rtl; text-align: right; }
    h1, h2, h3, .stMarkdown p, label, .stCaption { direction: rtl; text-align: right; }

    [data-testid="stSidebar"] { direction: rtl; }

    .card {
        background: #ffffff;
        border: 1px solid #e5e7eb;
        border-radius: 16px;
        padding: 18px 20px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05);
        direction: rtl;
        height: 100%;
    }
    .mini-card {
        background: #ffffff;
        border: 1px solid #e5e7eb;
        border-radius: 12px;
        padding: 10px 14px;
        margin-bottom: 10px;
        direction: rtl;
    }
    .alert-card {
        background: #fef2f2;
        border: 1px solid #fecaca;
        border-radius: 12px;
        padding: 14px;
        direction: rtl;
        text-align: center;
    }
    .total-card {
        background: #eff6ff;
        border: 1px solid #bfdbfe;
        border-radius: 12px;
        padding: 14px;
        direction: rtl;
        text-align: center;
        margin-bottom: 10px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# توابع کمکی
# ---------------------------------------------------------------------------
def sheet_csv_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"


def normalize_title(title) -> str:
    if not isinstance(title, str):
        return ""
    t = title.strip()
    t = t.replace("ك", "ک").replace("ي", "ی")
    return re.sub(r"\s+", " ", t)


def parse_qty(x):
    """'-' یعنی موجودی صفر گزارش‌شده. خالی یعنی هنوز برای این تاریخ گزارش نشده."""
    if pd.isna(x):
        return pd.NA
    s = str(x).strip()
    if s == "":
        return pd.NA
    if s == "-":
        return 0
    try:
        return int(float(s))
    except ValueError:
        return pd.NA


def extract_capacity(title: str) -> str:
    m = re.search(r"(\d+)\s*(GB|TB)", title, re.IGNORECASE)
    return f"{m.group(1)}{m.group(2).upper()}" if m else ""


def date_sort_key(date_col: str):
    m = DATE_KEY_PATTERN.search(date_col)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def date_label(date_col: str) -> str:
    m = DATE_KEY_PATTERN.search(date_col)
    return m.group(0) if m else date_col


@st.cache_data(ttl=30, show_spinner=False)
def load_rep(sheet_id: str):
    """برمی‌گردونه: (dict تاریخ->DataFrame(عنوان کالا، تعداد), last_edit_time یا None, پیام خطا یا None).
    کش ۳۰ ثانیه‌ای: هنوز عملاً زنده‌ست، ولی هر تایپ/کلیک باعث فچ دوباره‌ی هر ۳ شیت نمیشه.
    """
    resp = requests.get(sheet_csv_url(sheet_id), timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8-sig"  # جلوگیری از حدس اشتباه انکودینگ (Mojibake) روی متن فارسی
    raw = pd.read_csv(io.StringIO(resp.text), header=None, dtype=str)
    if raw.empty:
        return {}, None, "شیت خالی است."

    header = raw.iloc[0].fillna("").astype(str).str.strip()
    date_idx = [i for i, c in enumerate(header) if DATE_COL_PATTERN.search(c)]
    if not date_idx:
        raw_headers = [h for h in header.tolist() if h][:8]  # چند ستون اول هدر واقعی، برای عیب‌یابی
        return {}, None, f"هیچ ستون موجودی با الگوی تاریخ‌دار پیدا نشد. هدر واقعی خونده‌شده: {raw_headers}"

    last_edit = None
    if raw.shape[0] > LAST_EDIT_ROW_INDEX:
        val = raw.iat[LAST_EDIT_ROW_INDEX, 0]
        if isinstance(val, str) and val.strip():
            last_edit = val.strip()

    body = raw.iloc[1:].copy()
    if LAST_EDIT_ROW_INDEX in body.index:
        body = body.drop(index=LAST_EDIT_ROW_INDEX)

    result = {}
    for i in date_idx:
        dc = header[i]
        sub = body[[0, i]].copy()
        sub.columns = ["عنوان کالا", "تعداد"]
        sub["عنوان کالا"] = sub["عنوان کالا"].apply(normalize_title)
        sub = sub[(sub["عنوان کالا"] != "") & (sub["عنوان کالا"] != normalize_title("جمع"))]
        sub["تعداد"] = sub["تعداد"].apply(parse_qty)
        if sub["تعداد"].notna().any():
            result[dc] = sub.reset_index(drop=True)
    return result, last_edit, None


def dot(color: str) -> str:
    return f'<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:{color};margin-left:6px;"></span>'


def build_comparison_html(df: pd.DataFrame, rep_cols: list, max_height: int = 480) -> str:
    header_cells = "<th style='padding:10px;text-align:center;color:#6b7280;font-size:0.85rem;'>ردیف</th>"
    header_cells += "<th style='padding:10px;text-align:right;color:#111827;'>عنوان کالا</th>"
    header_cells += "<th style='padding:10px;text-align:center;color:#6b7280;font-size:0.85rem;'>ظرفیت</th>"
    for rep in rep_cols:
        header_cells += (
            f"<th style='padding:10px;text-align:center;color:#111827;'>{dot(REP_COLORS.get(rep, '#999'))}{html.escape(rep)}</th>"
        )
    header_cells += "<th style='padding:10px;text-align:center;color:#111827;'>جمع کل</th>"

    rows_html = []
    for i, (_, row) in enumerate(df.iterrows(), start=1):
        bg = "#fafafa" if i % 2 == 0 else "#ffffff"
        cells = f"<td style='padding:9px 10px;text-align:center;color:#9ca3af;font-size:0.85rem;'>{i}</td>"
        cells += f"<td style='padding:9px 10px;text-align:right;font-weight:600;color:#111827;'>{html.escape(str(row['عنوان کالا']))}</td>"
        cells += f"<td style='padding:9px 10px;text-align:center;color:#6b7280;font-size:0.85rem;'>{html.escape(str(row['ظرفیت']))}</td>"
        for rep in rep_cols:
            val = row[rep]
            color = "#111827" if val > 0 else "#ef4444"
            cells += f"<td style='padding:9px 10px;text-align:center;color:{color};font-weight:600;'>{val}</td>"
        cells += f"<td style='padding:9px 10px;text-align:center;font-weight:800;color:#111827;'>{row['جمع کل']}</td>"
        rows_html.append(f"<tr style='background:{bg};'>{cells}</tr>")

    return f"""
    <div style="direction:rtl;max-height:{max_height}px;overflow-y:auto;border:1px solid #e5e7eb;border-radius:12px;">
    <table style="width:100%;border-collapse:collapse;font-size:0.95rem;">
    <thead style="position:sticky;top:0;background:#f9fafb;box-shadow:0 1px 0 #e5e7eb;">
    <tr>{header_cells}</tr>
    </thead>
    <tbody>{''.join(rows_html)}</tbody>
    </table>
    </div>
    """


def build_low_stock_html(df: pd.DataFrame) -> str:
    if df.empty:
        return "<div class='rtl-text' style='color:#6b7280;padding:10px;'>موردی زیر آستانه موجودی نیست 🎉</div>"
    rows_html = []
    for i, (_, row) in enumerate(df.iterrows(), start=1):
        bg = "#fafafa" if i % 2 == 0 else "#ffffff"
        rows_html.append(
            f"<tr style='background:{bg};'>"
            f"<td style='padding:8px 10px;text-align:center;font-weight:700;color:#dc2626;'>{row['موجودی']}</td>"
            f"<td style='padding:8px 10px;text-align:right;color:#111827;'>{html.escape(str(row['عنوان کالا']))}</td>"
            f"<td style='padding:8px 10px;text-align:center;color:#111827;'>{dot(REP_COLORS.get(row['نماینده'], '#999'))}{html.escape(row['نماینده'])}</td>"
            f"</tr>"
        )
    return f"""
    <div style="direction:rtl;max-height:360px;overflow-y:auto;border:1px solid #e5e7eb;border-radius:12px;background:#ffffff;">
    <table style="width:100%;border-collapse:collapse;font-size:0.9rem;">
    <thead style="position:sticky;top:0;background:#f9fafb;">
    <tr>
      <th style="padding:9px 10px;text-align:center;color:#374151;">موجودی</th>
      <th style="padding:9px 10px;text-align:right;color:#374151;">عنوان کالا</th>
      <th style="padding:9px 10px;text-align:center;color:#374151;">نماینده</th>
    </tr>
    </thead>
    <tbody>{''.join(rows_html)}</tbody>
    </table>
    </div>
    """


def build_changes_html(rows: list, limit: int = 12) -> str:
    if not rows:
        return "<div class='rtl-text' style='color:#6b7280;padding:10px;'>بین دو گزارش اخیر، تغییری ثبت نشده.</div>"
    rows_sorted = sorted(rows, key=lambda r: abs(r["تغییر"]), reverse=True)
    shown = rows_sorted[:limit]
    rows_html = []
    for i, r in enumerate(shown):
        bg = "#fafafa" if i % 2 == 0 else "#ffffff"
        sign_color = "#16a34a" if r["تغییر"] > 0 else "#dc2626"
        sign = f"+{r['تغییر']}" if r["تغییر"] > 0 else str(r["تغییر"])
        rows_html.append(
            f"<tr style='background:{bg};'>"
            f"<td style='padding:8px 10px;text-align:center;font-weight:700;color:{sign_color};'>{sign}</td>"
            f"<td style='padding:8px 10px;text-align:right;color:#111827;'>{html.escape(r['کالا'])}</td>"
            f"<td style='padding:8px 10px;text-align:center;color:#111827;'>{dot(REP_COLORS.get(r['نماینده'], '#999'))}{html.escape(r['نماینده'])}</td>"
            f"<td style='padding:8px 10px;text-align:center;color:#6b7280;font-size:0.8rem;'>{html.escape(str(r['زمان']))}</td>"
            f"</tr>"
        )
    note = ""
    if len(rows_sorted) > limit:
        note = f"<div style='direction:rtl;text-align:left;padding:6px 10px;color:#6b7280;font-size:0.8rem;'>+{len(rows_sorted) - limit} تغییر دیگر</div>"
    return f"""
    <div style="direction:rtl;max-height:360px;overflow-y:auto;border:1px solid #e5e7eb;border-radius:12px;background:#ffffff;">
    <table style="width:100%;border-collapse:collapse;font-size:0.9rem;">
    <thead style="position:sticky;top:0;background:#f9fafb;">
    <tr>
      <th style="padding:9px 10px;text-align:center;color:#374151;">تغییر</th>
      <th style="padding:9px 10px;text-align:right;color:#374151;">کالا</th>
      <th style="padding:9px 10px;text-align:center;color:#374151;">نماینده</th>
      <th style="padding:9px 10px;text-align:center;color:#374151;">زمان ثبت</th>
    </tr>
    </thead>
    <tbody>{''.join(rows_html)}</tbody>
    </table>
    </div>
    """ + note


# ---------------------------------------------------------------------------
# بارگذاری داده
# ---------------------------------------------------------------------------
rep_data, rep_last_edit, warnings = {}, {}, []
with st.spinner("در حال دریافت اطلاعات از گوگل‌شیت..."):
    for name, sid in SHEETS.items():
        try:
            data, last_edit, err = load_rep(sid)
            rep_data[name] = data
            rep_last_edit[name] = last_edit
            if err:
                warnings.append(f"⚠️ **{name}**: {err}")
        except requests.exceptions.RequestException as e:
            rep_data[name] = {}
            rep_last_edit[name] = None
            warnings.append(f"⚠️ **{name}**: اتصال به گوگل‌شیت برقرار نشد (تایم‌اوت یا خطای شبکه) — {e}")
        except Exception as e:
            rep_data[name] = {}
            rep_last_edit[name] = None
            warnings.append(f"⚠️ **{name}**: خطا در خواندن شیت — {e}")

if not any(rep_data.values()):
    st.error("هیچ داده‌ای از هیچ نماینده‌ای خوانده نشد.")
    st.stop()

all_date_cols = sorted({dc for d in rep_data.values() for dc in d.keys()}, key=date_sort_key, reverse=True)
date_options = {date_label(dc): dc for dc in all_date_cols}

# ---------------------------------------------------------------------------
# سایدبار — خلاصه هر نماینده + جمع کل + هشدار کمبود
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### نماینده‌ها")
    latest_dc_overall = all_date_cols[0] if all_date_cols else None
    threshold = st.slider("آستانه‌ی موجودی کم", min_value=1, max_value=10, value=3)

    rep_latest_totals, rep_low_counts = {}, {}
    for name, data in rep_data.items():
        dcs = sorted(data.keys(), key=date_sort_key, reverse=True)
        if dcs:
            latest_df = data[dcs[0]]
            total = int(latest_df["تعداد"].sum(skipna=True))
            low = int((latest_df["تعداد"] < threshold).sum())
        else:
            total, low = None, 0
        rep_latest_totals[name] = total
        rep_low_counts[name] = low

        last_edit = rep_last_edit.get(name)
        st.markdown(
            f"""
            <div class="mini-card">
              <div style="display:flex;align-items:center;gap:8px;">
                <div style="width:30px;height:30px;border-radius:8px;background:{REP_COLORS[name]};
                            color:#fff;display:flex;align-items:center;justify-content:center;
                            font-weight:700;font-size:0.8rem;">{REP_ABBR[name]}</div>
                <div>
                  <div style="font-weight:700;color:#111827;">{name}</div>
                  <div style="font-size:0.75rem;color:#6b7280;">آخرین تغییر: {last_edit or "ثبت نشده"}</div>
                </div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    overall_total = sum(v for v in rep_latest_totals.values() if v is not None)
    overall_low = sum(rep_low_counts.values())

    st.markdown(
        f"""
        <div class="total-card">
          <div style="font-size:0.85rem;color:#1d4ed8;">مجموع کل موجودی</div>
          <div style="font-size:1.7rem;font-weight:800;color:#1e3a8a;">{overall_total:,}</div>
        </div>
        <div class="alert-card">
          <div style="font-size:0.85rem;color:#b91c1c;">کالاهای زیر آستانه موجودی</div>
          <div style="font-size:1.7rem;font-weight:800;color:#991b1b;">{overall_low}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------------------
# هدر
# ---------------------------------------------------------------------------
top_l, top_r = st.columns([3, 1])
with top_l:
    st.markdown("## 📦 داشبورد موجودی بافر گارانتی")
    st.caption("نمای کلی موجودی محصولات در سه نماینده — داده‌ها به‌صورت زنده از گوگل‌شیت خونده می‌شن.")
with top_r:
    st.markdown(
        f"<div class='rtl-text' style='color:#6b7280;font-size:0.85rem;padding-top:10px;'>"
        f"دریافت داده: {datetime.now().strftime('%H:%M:%S')}</div>",
        unsafe_allow_html=True,
    )
    if st.button("🔄 بروزرسانی"):
        st.cache_data.clear()
        st.rerun()

# ---------------------------------------------------------------------------
# کارت‌های تفصیلی هر نماینده
# ---------------------------------------------------------------------------
cols = st.columns(3)
for col, name in zip(cols, SHEETS.keys()):
    total = rep_latest_totals.get(name)
    low = rep_low_counts.get(name)
    last_edit = rep_last_edit.get(name)
    total_str = f"{total:,}" if total is not None else "—"
    col.markdown(
        f"""
        <div class="card">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;">
            <div style="width:38px;height:38px;border-radius:10px;background:{REP_COLORS[name]};color:#fff;
                        display:flex;align-items:center;justify-content:center;font-weight:700;">{REP_ABBR[name]}</div>
            <div>
              <div style="font-weight:700;font-size:1.05rem;color:#111827;">{name}</div>
              <div style="font-size:0.78rem;color:#6b7280;">آخرین تغییر: {last_edit or "ثبت نشده"}</div>
            </div>
          </div>
          <div style="display:flex;justify-content:space-between;">
            <div>
              <div style="font-size:0.8rem;color:#6b7280;">موجودی کل</div>
              <div style="font-size:1.6rem;font-weight:800;color:#111827;">{total_str}</div>
            </div>
            <div style="text-align:left;">
              <div style="font-size:0.8rem;color:#6b7280;">کالاهای کم‌موجود</div>
              <div style="font-size:1.6rem;font-weight:800;color:#dc2626;">{low}</div>
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

st.write("")

# ---------------------------------------------------------------------------
# نمودار روند (در یک بستهٔ LTR جداگانه، تا کج نشه)
# ---------------------------------------------------------------------------
st.markdown("<div class='rtl-text'><h3>📈 روند موجودی در طول زمان</h3></div>", unsafe_allow_html=True)

trend_rows = [
    {"نماینده": name, "تاریخ": date_label(dc), "تعداد": int(sub["تعداد"].sum(skipna=True))}
    for name, data in rep_data.items()
    for dc, sub in data.items()
]
if trend_rows:
    trend_df = pd.DataFrame(trend_rows)
    date_order = [date_label(dc) for dc in sorted(all_date_cols, key=date_sort_key)]
    chart = (
        alt.Chart(trend_df)
        .mark_line(point=True, strokeWidth=3)
        .encode(
            x=alt.X("تاریخ:N", sort=date_order, title="تاریخ"),
            y=alt.Y("تعداد:Q", title="جمع موجودی"),
            color=alt.Color(
                "نماینده:N",
                title="نماینده",
                scale=alt.Scale(domain=list(REP_COLORS.keys()), range=list(REP_COLORS.values())),
            ),
            tooltip=["نماینده", "تاریخ", "تعداد"],
        )
        .properties(height=320)
        .configure_axis(labelFontSize=12, titleFontSize=13)
        .configure_legend(labelFontSize=12, titleFontSize=13)
    )
    st.altair_chart(chart, use_container_width=True)

st.divider()

# ---------------------------------------------------------------------------
# انتخاب تاریخ + جدول مقایسه
# ---------------------------------------------------------------------------
if not date_options:
    st.error("هیچ تاریخ گزارش‌شده‌ای پیدا نشد.")
    st.stop()

sel_col1, sel_col2, sel_col3 = st.columns([1.2, 2, 1])
with sel_col1:
    selected_label = st.selectbox("📅 تاریخ", list(date_options.keys()))
with sel_col2:
    search = st.text_input("🔍 جستجوی کالا")
with sel_col3:
    only_available = st.checkbox("فقط موجود در حداقل یک نماینده")

selected_col = date_options[selected_label]

merged = None
for name, data in rep_data.items():
    sub = data.get(selected_col)
    if sub is None:
        continue
    sub = sub.rename(columns={"تعداد": name})
    merged = sub if merged is None else pd.merge(merged, sub, on="عنوان کالا", how="outer")

if merged is None:
    st.warning("برای این تاریخ هیچ نماینده‌ای گزارشی ثبت نکرده.")
    st.stop()

rep_cols = [c for c in merged.columns if c != "عنوان کالا"]
merged[rep_cols] = merged[rep_cols].apply(lambda s: s.fillna(0).astype(int))
merged["جمع کل"] = merged[rep_cols].sum(axis=1)
merged["ظرفیت"] = merged["عنوان کالا"].apply(extract_capacity)
merged = merged.sort_values("عنوان کالا").reset_index(drop=True)

show = merged.copy()
if search:
    show = show[show["عنوان کالا"].str.contains(search, case=False, na=False)]
if only_available:
    show = show[show["جمع کل"] > 0]

st.markdown(f"<div class='rtl-text'><h3>مقایسه موجودی کالاها — {selected_label}</h3></div>", unsafe_allow_html=True)
st.markdown(build_comparison_html(show, rep_cols), unsafe_allow_html=True)

csv_bytes = show.to_csv(index=False).encode("utf-8-sig")
st.download_button("⬇️ دانلود CSV", csv_bytes, f"buffer_inventory_{selected_label}.csv", "text/csv")

st.divider()

# ---------------------------------------------------------------------------
# ردیف پایین: کالاهای کم‌موجود / نمودار میله‌ای / آخرین تغییرات
# ---------------------------------------------------------------------------
bottom1, bottom2, bottom3 = st.columns(3)

with bottom1:
    st.markdown(f"<div class='rtl-text'><h4>کالاهای زیر آستانه ({threshold} عدد)</h4></div>", unsafe_allow_html=True)
    low_rows = []
    for name, data in rep_data.items():
        dcs = sorted(data.keys(), key=date_sort_key, reverse=True)
        if not dcs:
            continue
        latest = data[dcs[0]]
        low = latest[latest["تعداد"] < threshold]
        for _, r in low.iterrows():
            low_rows.append({"نماینده": name, "عنوان کالا": r["عنوان کالا"], "موجودی": int(r["تعداد"])})
    low_df = pd.DataFrame(low_rows).sort_values("موجودی") if low_rows else pd.DataFrame(columns=["نماینده", "عنوان کالا", "موجودی"])
    st.markdown(build_low_stock_html(low_df), unsafe_allow_html=True)

with bottom2:
    st.markdown("<div class='rtl-text'><h4>موجودی کل در هر نماینده</h4></div>", unsafe_allow_html=True)
    bar_df = pd.DataFrame(
        [{"نماینده": n, "موجودی": t or 0} for n, t in rep_latest_totals.items()]
    )
    bar_chart = (
        alt.Chart(bar_df)
        .mark_bar(size=45, cornerRadiusTopLeft=6, cornerRadiusTopRight=6)
        .encode(
            x=alt.X("نماینده:N", title=None, sort=list(REP_COLORS.keys())),
            y=alt.Y("موجودی:Q", title=None),
            color=alt.Color(
                "نماینده:N",
                legend=None,
                scale=alt.Scale(domain=list(REP_COLORS.keys()), range=list(REP_COLORS.values())),
            ),
            tooltip=["نماینده", "موجودی"],
        )
        .properties(height=300)
    )
    st.altair_chart(bar_chart, use_container_width=True)

with bottom3:
    st.markdown("<div class='rtl-text'><h4>آخرین تغییرات ثبت‌شده</h4></div>", unsafe_allow_html=True)
    change_rows = []
    for name, data in rep_data.items():
        dcs = sorted(data.keys(), key=date_sort_key)
        if len(dcs) < 2:
            continue
        prev_df = data[dcs[-2]].rename(columns={"تعداد": "قبلی"})
        latest_df = data[dcs[-1]].rename(columns={"تعداد": "جدید"})
        cmp = pd.merge(prev_df, latest_df, on="عنوان کالا", how="inner").dropna(subset=["قبلی", "جدید"])
        cmp["تغییر"] = cmp["جدید"].astype(int) - cmp["قبلی"].astype(int)
        cmp = cmp[cmp["تغییر"] != 0]
        for _, r in cmp.iterrows():
            change_rows.append(
                {
                    "نماینده": name,
                    "کالا": r["عنوان کالا"],
                    "تغییر": int(r["تغییر"]),
                    "زمان": rep_last_edit.get(name) or date_label(dcs[-1]),
                }
            )
    st.markdown(build_changes_html(change_rows), unsafe_allow_html=True)

if warnings:
    st.divider()
    for w in warnings:
        st.warning(w)
