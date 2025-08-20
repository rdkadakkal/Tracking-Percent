import io
import pandas as pd
import numpy as np
import streamlit as st
from datetime import datetime, date, time, timezone

st.set_page_config(page_title="Shipments & Tracking %", layout="wide")

st.title("Shipments Created & Tracking % (Post-Fix)")

st.markdown(
    "Upload your weekly shipments file and choose a start date (e.g., the date the carrier-mapping fix went live). "
    "Then select tenant and carrier to see **counts and tracking %** since that date."
)

# -------------------------------
# Helpers
# -------------------------------
@st.cache_data(show_spinner=False)
def load_df(file_bytes: bytes, filename: str) -> pd.DataFrame:
    if filename.lower().endswith(".csv"):
        df = pd.read_csv(io.BytesIO(file_bytes))
    else:
        # Default to first sheet; change sheet_name if you need a specific sheet
        df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=0)
    return df

def coerce_bool(series: pd.Series) -> pd.Series:
    s = series.astype(str).str.strip().str.lower()
    true_vals = {"true", "1", "yes", "y"}
    return s.isin(true_vals)

def coerce_datetime_utc(series: pd.Series) -> pd.Series:
    # robust parse; treat as UTC if tz-naive
    dt = pd.to_datetime(series, errors="coerce", utc=True)
    # If parsed as naive, localize to UTC
    if not getattr(dt.dt, "tz", None):
        dt = dt.dt.tz_localize("UTC")
    return dt

def pick_first_present_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    lower_map = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand in df.columns:
            return cand
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    # try contains/partial match
    for c in df.columns:
        if any(k in c.lower() for k in [x.lower() for x in candidates]):
            return c
    return None

def percent(n, d) -> float:
    if d == 0:
        return 0.0
    return round(100.0 * n / d, 2)

# -------------------------------
# UI: Uploader
# -------------------------------
uploaded = st.file_uploader("Upload data file (.xlsx or .csv)", type=["xlsx", "xls", "csv"])

if not uploaded:
    st.info("Upload an Excel/CSV file to begin.")
    st.stop()

df_raw = load_df(uploaded.getvalue(), uploaded.name)

# -------------------------------
# Column detection
# -------------------------------
col_date = pick_first_present_column(df_raw, ["Shipment Created (UTC)", "Shipment Created UTC", "Created At (UTC)", "Created Date (UTC)"])
col_tracked = pick_first_present_column(df_raw, ["Tracked", "Is Tracked", "Tracking Status"])
col_carrier = pick_first_present_column(df_raw, ["Carrier Name", "Carrier", "Carrier_Name"])
col_tenant = pick_first_present_column(df_raw, ["Tenant", "Tenant Name", "Customer", "Account"])

missing = [label for label, col in {
    "Shipment Created (UTC)": col_date,
    "Tracked": col_tracked,
    "Carrier Name": col_carrier,
}.items() if col is None]

if missing:
    st.error(
        "Missing required column(s): " + ", ".join(missing) +
        ".\n\nMake sure your file has these columns (case-insensitive)."
    )
    st.stop()

# -------------------------------
# Clean & normalize types
# -------------------------------
df = df_raw.copy()

# Date
df[col_date] = coerce_datetime_utc(df[col_date])

# Tracked as bool
df[col_tracked] = coerce_bool(df[col_tracked])

# Normalize carrier to string
df[col_carrier] = df[col_carrier].astype(str).str.strip()

# Optional tenant
if col_tenant:
    df[col_tenant] = df[col_tenant].astype(str).str.strip()

# Drop rows without date
df = df.dropna(subset=[col_date])

# -------------------------------
# Sidebar filters
# -------------------------------
with st.sidebar:
    st.header("Filters")

    # Start date picker — default to the earliest date in the file rounded down to date
    min_dt = df[col_date].min()
    default_start = (min_dt.to_pydatetime().date() if isinstance(min_dt, pd.Timestamp) else date.today())

    start_date = st.date_input(
        "Start date (inclusive)",
        value=default_start,
        min_value=default_start,
        help="All shipments created on/after this date (UTC) will be included."
    )

    # Tenant filter (optional)
    if col_tenant:
        tenants = ["All"] + sorted(df[col_tenant].dropna().unique().tolist())
        tenant_choice = st.selectbox("Tenant", tenants, index=0)
    else:
        tenant_choice = "All"

    # Carrier dropdown with typeahead
    # Carrier list respects tenant filter (for faster lookup)
    df_for_carriers = df if tenant_choice == "All" else df[df[col_tenant] == tenant_choice]
    carrier_options = ["All"] + sorted(df_for_carriers[col_carrier].dropna().unique().tolist())
    carrier_choice = st.selectbox("Carrier Name", carrier_options, index=0, placeholder="Type a carrier...")

# -------------------------------
# Apply filters
# -------------------------------
# Build start datetime at 00:00 UTC
start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)

df_f = df[df[col_date] >= pd.Timestamp(start_dt)]

if tenant_choice != "All" and col_tenant:
    df_f = df_f[df_f[col_tenant] == tenant_choice]

if carrier_choice != "All":
    df_f = df_f[df_f[col_carrier] == carrier_choice]

# -------------------------------
# KPIs
# -------------------------------
total_shipments = len(df_f)
tracked_shipments = int(df_f[col_tracked].sum())
tracking_pct = percent(tracked_shipments, total_shipments)

left, mid, right = st.columns(3)
left.metric("Total Shipments", f"{total_shipments:,}")
mid.metric("Tracked Shipments", f"{tracked_shipments:,}")
right.metric("Tracking %", f"{tracking_pct:.2f}%")

# -------------------------------
# Detail tables
# -------------------------------
with st.expander("Show detail (daily breakdown)"):
    # Daily breakdown (UTC)
    if total_shipments > 0:
        grp = (
            df_f.assign(Day=df_f[col_date].dt.floor("D"))
               .groupby("Day", as_index=False)
               .agg(
                   total=("Day", "count"),
                   tracked=(col_tracked, "sum")
               )
        )
        grp["tracking_%"] = grp.apply(lambda r: percent(r["tracked"], r["total"]), axis=1)
        st.dataframe(grp, use_container_width=True)
    else:
        st.write("No rows in the selected range/filters.")

with st.expander("Show filtered rows"):
    st.dataframe(df_f, use_container_width=True)

# -------------------------------
# Downloads
# -------------------------------
def to_excel_bytes(df_to_save: pd.DataFrame) -> bytes:
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="xlsxwriter") as writer:
        df_to_save.to_excel(writer, index=False, sheet_name="Filtered")
    out.seek(0)
    return out.read()

st.download_button(
    "Download filtered rows (Excel)",
    data=to_excel_bytes(df_f),
    file_name="filtered_shipments.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

# -------------------------------
# (Optional) Small trend chart
# -------------------------------
if total_shipments > 0:
    st.subheader("Tracking % Trend")
    grp2 = (
        df_f.assign(Week=df_f[col_date].dt.to_period("W-SUN").dt.start_time)
            .groupby("Week", as_index=False)
            .agg(total=("Week", "count"), tracked=(col_tracked, "sum"))
    )
    grp2["tracking_%"] = grp2.apply(lambda r: percent(r["tracked"], r["total"]), axis=1)
    st.line_chart(
        grp2.set_index("Week")[["tracking_%"]],
        height=220,
    )

# -------------------------------
# Tips / Notes
# -------------------------------
st.caption(
    f"Columns used → Date: **{col_date}**, Tracked: **{col_tracked}**, Carrier: **{col_carrier}**"
    + (f", Tenant: **{col_tenant}**" if col_tenant else "")
)
