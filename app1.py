"""CareMatch AI: home-care scheduling demo.  Run: streamlit run app1.py"""
from __future__ import annotations

import os
import random
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

DISTRICTS = ["中正區", "大安區", "信義區", "松山區", "內湖區"]
ADDRESSES = {"中正區": "台北市中正區忠孝西路一段", "大安區": "台北市大安區復興南路二段",
             "信義區": "台北市信義區市府路1號", "松山區": "台北市松山區八德路四段", "內湖區": "台北市內湖區內湖路一段"}
LANGUAGES = ["國語", "台語", "客語", "英語"]
SKILLS = ["生活照顧", "移位與搬運", "失智症照護", "傷口照護", "陪同就醫", "沐浴協助"]


def make_data(seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = random.Random(seed)
    people = []
    for i in range(1, 25):
        stress, overtime, commute = rng.randint(1, 9), rng.randint(0, 5), rng.randint(15, 55)
        risk = min(100, round(stress * 6.5 + overtime * 7 + max(commute - 30, 0) * 1.1 + rng.uniform(-7, 7), 1))
        people.append({"id": f"CG{i:03d}", "姓名": f"居服員_{i}", "區域": rng.choice(DISTRICTS),
                       "語言": rng.choice(LANGUAGES), "技能": rng.sample(SKILLS, rng.randint(3, 5)),
                       "疲勞風險": risk, "壓力": stress, "近月加班": overtime})
    cases = []
    for i in range(1, 51):
        bp, activity = rng.randint(0, 5), rng.randint(0, 50)
        risk = "高" if bp >= 3 or activity >= 35 else "中" if bp >= 1 or activity >= 15 else "低"
        cases.append({"id": f"CASE{i:03d}", "姓名": f"長輩_{i}", "區域": rng.choice(DISTRICTS),
                      "語言": rng.choice(LANGUAGES), "需求技能": rng.sample(SKILLS, rng.randint(1, 2)),
                      "熟悉居服員": rng.sample([p["id"] for p in people], 2), "血壓異常天數": bp,
                      "活動下降": activity, "風險": risk, "服務日": rng.randrange(7)})
    return pd.DataFrame(people), pd.DataFrame(cases)


def reset_data() -> None:
    st.session_state.seed = random.SystemRandom().randint(1, 2_000_000_000)
    st.session_state.caregivers, st.session_state.cases = make_data(st.session_state.seed)
    st.session_state.protected = set()
    st.session_state.assignments = []
    st.session_state.route_cache = {}


def init() -> None:
    if "caregivers" not in st.session_state:
        reset_data()
    st.session_state.setdefault("protected", set())
    st.session_state.setdefault("assignments", [])
    st.session_state.setdefault("route_cache", {})
    st.session_state.setdefault("map_key", os.getenv("GOOGLE_MAPS_API_KEY", ""))


def route(origin: str, destination: str) -> tuple[float, float, str, str]:
    """Only labels a result as Google when the API actually returned a route."""
    key = (origin, destination, st.session_state.map_key)
    if key in st.session_state.route_cache:
        return st.session_state.route_cache[key]
    if origin == destination:
        result = (4.0, 1.2, "同區估算", "同一行政區")
    elif not st.session_state.map_key:
        result = (18.0, 5.0, "估算", "未設定 Google Maps API 金鑰")
    else:
        body = {"origins": [{"waypoint": {"address": ADDRESSES[origin]}}], "destinations": [{"waypoint": {"address": ADDRESSES[destination]}}],
                "travelMode": "DRIVE", "routingPreference": "TRAFFIC_AWARE"}
        headers = {"Content-Type": "application/json", "X-Goog-Api-Key": st.session_state.map_key,
                   "X-Goog-FieldMask": "duration,distanceMeters,condition,status"}
        try:
            response = requests.post("https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix", json=body, headers=headers, timeout=10)
            if not response.ok:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:120]}")
            item = response.json()[0]
            if "duration" not in item:
                raise RuntimeError(str(item.get("status", item.get("condition", "沒有可用路線"))))
            result = (round(float(item["duration"].rstrip("s")) / 60, 1), round(item["distanceMeters"] / 1000, 1), "Google Routes API", "即時交通路線")
        except (requests.RequestException, RuntimeError, ValueError, IndexError, KeyError) as exc:
            result = (18.0, 5.0, "估算", f"Google Routes API 未成功：{exc}")
    st.session_state.route_cache[key] = result
    return result


def load_for(cg_id: str, target_date: date) -> int:
    return sum(x["居服員ID"] == cg_id and x["服務日"] == target_date for x in st.session_state.assignments)


def candidates(case: pd.Series, target_date: date, use_maps: bool = True) -> list[dict]:
    rows = []
    for _, cg in st.session_state.caregivers.iterrows():
        # 帶入 target_date 檢查當日是否接滿 3 件
        if cg.id in st.session_state.protected or load_for(cg.id, target_date) >= 3:
            continue
        if not set(case["需求技能"]).issubset(set(cg["技能"])):
            continue
        if cg["語言"] not in ("國語", case["語言"]):
            continue
        if use_maps:
            minutes, km, source, detail = route(cg["區域"], case["區域"])
        else:
            minutes = 4.0 if cg["區域"] == case["區域"] else 18.0
            km, source, detail = (1.2 if cg["區域"] == case["區域"] else 5.0), "模型估算", "週期模擬"
            
        familiar, same_area = cg.id in case["熟悉居服員"], cg["區域"] == case["區域"]
        # 依據選擇的日期計算預估疲勞
        projected = min(100, cg["疲勞風險"] + 6 + minutes / 12 + load_for(cg.id, target_date) * 9)
        score = 100 + (13 if familiar else 0) + (7 if same_area else 0) - minutes * 1.25 - projected * .32
        
        rows.append({"居服員ID": cg.id, "姓名": cg["姓名"], "居服員區域": cg["區域"],"技能": cg["技能"], "路程分鐘": minutes, "公里": km,
                     "路線來源": source, "路線說明": detail, "原疲勞": cg["疲勞風險"], "預估疲勞": round(projected, 1),
                     "綜合分數": round(score, 1), "熟悉個案": familiar})
    return sorted(rows, key=lambda r: r["綜合分數"], reverse=True)


def three_options(pool: list[dict]) -> list[tuple[str, dict]]:
    """Choose distinct staff, so the three decision lenses cannot accidentally render one person three times."""
    if not pool:
        return []
    picked = []
    rules = [("方案 A：綜合效益最佳", lambda r: (-r["綜合分數"],)),
             ("方案 B：最低交通成本", lambda r: (r["路程分鐘"], r["公里"], -r["綜合分數"])),
             ("方案 C：最低預估疲勞", lambda r: (r["預估疲勞"], r["路程分鐘"]))]
    for label, key in rules:
        available = [r for r in pool if r["居服員ID"] not in {x[1]["居服員ID"] for x in picked}]
        choice = min(available or pool, key=key)
        picked.append((label, choice))
    return picked


def commit(case: pd.Series, choice: dict, target_date: date) -> tuple[bool, str]:
    if choice["居服員ID"] in st.session_state.protected:
        return False, "此居服員已被強制休息，系統拒絕排班。"
    if load_for(choice["居服員ID"], target_date) >= 3:
        return False, "此居服員當日已達 3 件服務上限。"
    if any(x["個案ID"] == case.id and x["服務日"] == target_date for x in st.session_state.assignments):
        return False, f"此長輩在 {target_date} 當日已完成排班，不可重複排班。"
        
    peers = candidates(case, target_date)
    st.session_state.assignments.append({
        "服務日": target_date, "個案ID": case.id, "個案": case["姓名"], 
        "居服員ID": choice["居服員ID"], "居服員": choice["姓名"], 
        "分鐘": choice["路程分鐘"], "公里": choice["公里"], "疲勞": choice["預估疲勞"],
        "基準分鐘": sum(p["路程分鐘"] for p in peers) / len(peers), 
        "基準疲勞": sum(p["預估疲勞"] for p in peers) / len(peers)
    })
    return True, f"已成功儲存 {target_date} 的排班紀錄！"


def weekly_simulation() -> pd.DataFrame:
    """Create a fresh weekly allocation based on current randomized data; no fixed KPI figures."""
    loads: dict[tuple[str, int], int] = {}
    rows = []
    # Serve high-risk cases first. This is a calculation only and never changes manual assignments.
    for _, case in st.session_state.cases.sort_values(["服務日", "風險"], ascending=[True, False]).iterrows():
        day = int(case["服務日"])
        pool = []
        for _, cg in st.session_state.caregivers.iterrows():
            key = (cg.id, day)
            if cg.id in st.session_state.protected or loads.get(key, 0) >= 3 or not set(case["需求技能"]).issubset(set(cg["技能"])) or cg["語言"] not in ("國語", case["語言"]):
                continue
            mins = 4.0 if cg["區域"] == case["區域"] else 18.0
            fatigue = min(100, cg["疲勞風險"] + 6 + mins / 12 + loads.get(key, 0) * 9)
            score = 100 + (13 if cg.id in case["熟悉居服員"] else 0) + (7 if cg["區域"] == case["區域"] else 0) - mins * 1.25 - fatigue * .32
            pool.append((score, mins, fatigue, cg.id, cg["區域"] == case["區域"]))
        if pool:
            pool.sort(reverse=True)
            score, mins, fatigue, cg_id, same = pool[0]
            loads[(cg_id, day)] = loads.get((cg_id, day), 0) + 1
            baseline_minutes = sum(x[1] for x in pool) / len(pool)
            baseline_fatigue = sum(x[2] for x in pool) / len(pool)
            rows.append({"日": day + 1, "案件數": 1, "路程分鐘": mins, "基準路程分鐘": baseline_minutes,
                         "預估疲勞": fatigue, "基準疲勞": baseline_fatigue, "高疲勞": int(fatigue >= 70), "未配對": 0})
        else:
            rows.append({"日": day + 1, "案件數": 0, "路程分鐘": 0, "基準路程分鐘": 0, "預估疲勞": 0, "基準疲勞": 0, "高疲勞": 0, "未配對": 1})
    frame = pd.DataFrame(rows)
    result = frame.groupby("日", as_index=False).agg({"案件數": "sum", "路程分鐘": "sum", "基準路程分鐘": "sum", "預估疲勞": "mean", "基準疲勞": "mean", "高疲勞": "sum", "未配對": "sum"})
    result["路程節省分鐘"] = result["基準路程分鐘"] - result["路程分鐘"]
    result["疲勞改善"] = result["基準疲勞"] - result["預估疲勞"]
    return result.fillna(0)


st.set_page_config(page_title="CareMatch AI", layout="wide")
init()
with st.sidebar:
    st.header("資料與地圖設定")
    if st.button("重新產生隨機資料", use_container_width=True):
        reset_data(); st.rerun()
    st.session_state.map_key = st.text_input("Google Maps API 金鑰", st.session_state.map_key, type="password")
    st.caption("需啟用 Routes API 與計費。金鑰僅保留在本次工作階段。")



# 🌟 禁止瀏覽器自動翻譯日曆元件（防止「蘇莫圖我們釷法蘭西斯罐」發生）
st.markdown("""
    <style>
        /* 鎖定 Streamlit 日曆與下拉選單區塊，禁止自動翻譯 */
        div[data-baseweb="calendar"], 
        div[data-baseweb="popover"],
        div[data-baseweb="select"] {
            translate: no !important;
        }
    </style>
""", unsafe_allow_html=True)

st.title("CareMatch AI 居家照護排班")

tabs = st.tabs(["🧠 過勞風險預警", "🎯 智慧媒合", "🩺 長輩健康", "🗺️ 供需預測", "📈 營運效率"])
caregivers, cases = st.session_state.caregivers, st.session_state.cases

def efficiency_frame() -> pd.DataFrame:
    """依據實際排班紀錄（st.session_state.assignments）計算營運 KPI"""
    if not st.session_state.assignments:
        return pd.DataFrame()
        
    df = pd.DataFrame(st.session_state.assignments)
    
    # 依日期群組統計數據
    grouped = df.groupby("服務日", as_index=False).agg(
        案件數=("個案ID", "count"),
        總路程分鐘=("分鐘", "sum"),
        基準路程分鐘=("基準分鐘", "sum"),
        總公里=("公里", "sum"),
        平均預估疲勞=("疲勞", "mean"),
        基準疲勞=("基準疲勞", "mean"),
        高疲勞排班數=("疲勞", lambda x: sum(v >= 70 for v in x))
    )
    
    # 計算節省時間與疲勞改善
    grouped["路程節省分鐘"] = round(grouped["基準路程分鐘"] - grouped["總路程分鐘"], 1)
    grouped["疲勞改善"] = round(grouped["基準疲勞"] - grouped["平均預估疲勞"], 1)
    
    # 格式化日期欄位名稱，以符合 UI 顯示需求
    grouped["日期"] = grouped["服務日"].astype(str)
    
    return grouped.sort_values("日期")



with tabs[0]:
    st.header("🧠 過勞風險預警與保護機制")
    
    # 取得當前所有高風險居服員 (疲勞風險 >= 60)
    high_risk_cg = caregivers[caregivers["疲勞風險"] >= 60].sort_values("疲勞風險", ascending=False)
    
    # 分流：1. 尚未保護的高風險人員  2. 已經啟動保護的人員
    pending_risk_cg = high_risk_cg[~high_risk_cg["id"].isin(st.session_state.protected)]
    protected_cg = caregivers[caregivers["id"].isin(st.session_state.protected)]
    
    # ----------------------------------------------------
    # 區塊一：高疲勞預警（待處置/未保護）
    # ----------------------------------------------------
    st.subheader("⚠️ 高疲勞預警名單（需督導評估是否強制休息）")
    if pending_risk_cg.empty:
        st.success("✅ 目前無未處置的高過勞風險居服員。")
    else:
        for _, cg in pending_risk_cg.iterrows():
            with st.container(border=True):
                col_a, col_b = st.columns([5, 1])
                with col_a:
                    st.write(f"**{cg['姓名']}（{cg.id}）**｜疲勞風險 **{cg['疲勞風險']}** 分｜近月加班 {cg['近月加班']} 次｜主責區域：{cg['區域']}")
                with col_b:
                    if st.button("強制休息", key=f"rest_{cg.id}", use_container_width=True):
                        st.session_state.protected.add(cg.id)
                        st.toast(f"🛡️ 已將 {cg['姓名']} 納入強制休息保護！", icon="🛑")
                        st.rerun()

    st.divider()

    # ----------------------------------------------------
    # 區塊二：目前執行強制休息中（另一區）
    # ----------------------------------------------------
    st.subheader("🛡️ 目前執行強制休息／保護中名單")
    if protected_cg.empty:
        st.info("目前無任何居服員處於強制休息狀態。所有媒合與儲存排班皆正常運算。")
    else:
        st.caption("以下居服員目前受到系統強制保護，智慧媒合與緊急救火皆會自動過濾排除：")
        for _, cg in protected_cg.iterrows():
            with st.container(border=True):
                col_a, col_b = st.columns([5, 1])
                with col_a:
                    st.write(f"🛑 **{cg['姓名']}（{cg.id}）**｜疲勞風險 {cg['疲勞風險']} 分｜保護狀態：**排班鎖定中**")
                with col_b:
                    if st.button("解除休息", key=f"unrest_{cg.id}", use_container_width=True):
                        st.session_state.protected.remove(cg.id)
                        st.toast(f"✅ 已解除 {cg['姓名']} 的休息狀態，恢復正常排班。", icon="🔓")
                        st.rerun()

with tabs[1]:
    st.header("🎯 三大 AI 調度方案與可解釋性（XAI）決策分析")
    st.caption("結合多目標優化演算法與可解釋 AI，清晰呈現各推薦人選的適配優勢與風險提醒。")
    
    col_case, col_date = st.columns([2, 1])
    
    with col_case:
        # 安全取得選單 index，避免 KeyError
        selected_index = st.selectbox(
            "選擇匹配／派單個案：", 
            options=cases.index, 
            format_func=lambda idx: f"{cases.loc[idx, 'id']} - {cases.loc[idx, '姓名']}（{cases.loc[idx, '區域']}）",
            key="case_picker_index"
        )
        
    with col_date:
        selected_date = st.date_input("選擇排班日期", value=date.today(), key="service_date")
        
    case = cases.iloc[selected_index]
    selected_id = case["id"]
    
    # 顯示選定個案詳細資訊
    st.info(f"**選定長輩資訊：{case['姓名']}（{case['id']}）**｜區域：{case['區域']}｜需求標籤：{', '.join(case['需求技能'])}｜語言：{case['語言']}")
    
    # 呼叫既有演算法（完全不改變核心計算）
    pool = candidates(case, selected_date)
    choices = three_options(pool)
    
    if not choices:
        st.warning(f"在 {selected_date} 沒有符合技能、語言、強制休息或當日額滿的可用居服員。")
    else:
        st.subheader(f"💡 AI 推薦三大調度方案比對（排班日期：{selected_date}）")
        cols = st.columns(3)
        
        for col, (label, pick) in zip(cols, choices):
            with col:
                with st.container(border=True):
                    # 方案標題與說明
                    st.markdown(f"### {label}")
                    st.caption({"方案 A：綜合效益最佳": "兼顧連續性、路程及疲勞平衡", "方案 B：最低交通成本": "優先最短移動時間與距離", "方案 C：最低預估疲勞": "優先避免居服員過勞"}[label])
                    # 顯示居服員的技能專長
                    st.divider()
                    st.subheader(f"👤 {pick['姓名']}（{pick['居服員ID']}）")
                    st.metric("🏆 綜合適配分數", f"{pick['綜合分數']} 分")
                    skills_text = ", ".join(pick.get("技能", [])) if pick.get("技能") else "無特別紀錄"
                    st.caption(f"🛠️ **居服員專長**：{skills_text}")
                    
                    st.write("") # 留一點微小的空白間隙
                    
                    # 基礎指標小卡
                    c_m1, c_m2 = st.columns(2)
                    c_m1.caption(f"🚗 路程：**{pick['路程分鐘']} 分** ({pick['公里']}km)")
                    c_m2.caption(f"⚡ 預估疲勞：**{pick['預估疲勞']} 分**")
                    
                    st.divider()
                    st.markdown("#### 🔍 可解釋 AI（XAI）適配診斷")
                    
                    # 🌟 創新亮點一：優勢條列 (Pro)
                    pros = []
                    pros.append(f"🎯 **專長完全匹配**：具備個案所需的 {', '.join(case['需求技能'])} 技能")
                    if pick["熟悉個案"]:
                        pros.append("🤝 **服務連續性極佳**：為長輩指定/熟悉之居服員，能大幅降低溝通磨合期。")
                    if pick["居服員區域"] == case["區域"]:
                        pros.append("📍 **同區在地派遣**：居服員與長輩同屬一個行政區，突發狀況應變速度最快。")
                    else:
                        pros.append("⚡ **彈性機動支援**：可提供跨區支援派遣，填補區域人力缺口。")
                    
                    if pick["預估疲勞"] < 50:
                        pros.append("🟢 **體能充沛良好**：派單後預估疲勞指數極低，服務品質穩定度高。")
                    elif pick["路程分鐘"] <= 5.0:
                        pros.append("⏱️ **極短通勤車程**：預估車程在 5 分鐘以內，大幅節省交通時間成本。")
                        
                    st.markdown("**【適配優勢 Pro】**")
                    for p in pros:
                        st.markdown(f"• {p}")
                    
                    # 🌟 創新亮點二：風險提醒 (Con)
                    cons = []
                    today_loads = load_for(pick["居服員ID"], selected_date)
                    if today_loads >= 2:
                        cons.append(f"⚠️ **當日負荷偏高**：該日已排定 {today_loads} 班，此班次為當日第 {today_loads + 1} 件。")
                    if pick["預估疲勞"] >= 70:
                        cons.append(f"🔴 **過勞高風險預警**：派單後預估疲勞高達 {pick['預估疲勞']} 分，建議優先觀察體能狀態。")
                    if pick["居服員區域"] != case["區域"]:
                        cons.append(f"🚗 **跨區交通移動**：車程需 {pick['路程分鐘']} 分鐘，需注意跨區車流與尖峰時段。")
                    if not pick["熟悉個案"]:
                        cons.append("🧩 **初次服務長輩**：非熟悉個案，建議首次服務前先確認照顧注意事項。")
                        
                    st.markdown("**【風險提示 & 建議 Con】**")
                    for c in cons:
                        st.markdown(f"• {c}")
                        
                    st.caption(f"🗺️ 路線資料來源：{pick['路線來源']}（{pick['路線說明']}）")
                    
                    st.write("")
                    # 採用方案按鈕
                    if st.button("採用此方案派單", key=f"choose_{label}_{selected_id}_{selected_date}", use_container_width=True):
                        ok, message = commit(case, pick, selected_date)
                        if ok:
                            st.success(message)
                            st.rerun()
                        else:
                            st.error(message)

        if len({x[1]['居服員ID'] for x in choices}) < 3:
            st.caption("💡 合格人選不足三位，因此部分方案使用相同候選人；這不是顯示錯誤。")

    # ----------------------------------------------------
    # 底下保留歷史與已排定班表總覽
    # ----------------------------------------------------
    st.divider()
    st.subheader("📋 目前已排定之班表總覽")
    if st.session_state.assignments:
        df_assign = pd.DataFrame(st.session_state.assignments)
        st.dataframe(
            df_assign[["服務日", "個案ID", "個案", "居服員ID", "居服員", "分鐘", "疲勞"]], 
            use_container_width=True, 
            hide_index=True
        )
    else:
        st.caption("尚無任何排班紀錄。請點選上方方案按鈕進行排班。")

with tabs[2]:
    st.header("🩺 長輩健康風險分流與緊急救火")
    
    # 🌟 1. 統一在最頂部讀取『智慧媒合』所選擇的排班日期
    # 若 session_state 裡有 service_date 則優先使用，否則預設為今日
    target_date = st.session_state.get("service_date", date.today())
    
    # 2. 取得該日期已處置的個案名單
    assigned_case_ids = {
        x["個案ID"] 
        for x in st.session_state.assignments 
        if x["服務日"] == target_date
    }
    
    st.subheader(f"🚨 待處置：高風險長輩即時預警與一鍵救火（{target_date}）")
    
    # 3. 頂部風險燈號統計（呈現整體狀況）
    a, b, c = st.columns(3)
    counts = cases["風險"].value_counts()
    a.metric("🔴 高風險總數", int(counts.get("高", 0)))
    b.metric("🟡 中風險總數", int(counts.get("中", 0)))
    c.metric("🟢 低風險總數", int(counts.get("低", 0)))
    
    st.divider()
    st.subheader(f"🚨 待處置：高風險長輩即時預警與一鍵救火（{target_date}）")
    
    # 4. 關鍵過濾：找出高風險且【在目標日期尚未排班】的個案
    high_risk_cases = cases[cases["風險"] == "高"]
    pending_cases = high_risk_cases[~high_risk_cases["id"].isin(assigned_case_ids)]
    
    # 5. 畫面渲染邏輯
    if pending_cases.empty:
        st.success(f"🎉 太棒了！在 {target_date} 的所有高風險長輩均已完成緊急巡視派遣與處置。")
    else:
        st.caption(f"目前還有 {len(pending_cases)} 位高風險長輩尚未指派緊急巡視：")
        
        for idx, row in pending_cases.iterrows():
            with st.container(border=True):
                c1, c2, c3 = st.columns([2, 3, 2])
                
                with c1:
                    st.markdown(f"### 👵 **{row['姓名']}（{row.id}）**")
                    st.caption(f"📍 區域：{row['區域']}｜語言：{row['語言']}")
                
                with c2:
                    st.write(f"🩸 **血壓異常天數：** 近一週連續 **{row['血壓異常天數']}** 天")
                    st.write(f"📉 **活動下降比例：** 近三日下降 **{row['活動下降']}%**")
                    st.write(f"🏷️ **需求技能：** {', '.join(row['需求技能'])}")
                
                with c3:
                    with st.popover("🚨 一鍵指派緊急巡視", use_container_width=True):
                        st.write(f"**為 {row['姓名']} 安排緊急巡視**（日期：{target_date}）")
        
        # 計算與指派也一律傳入 target_date
                        pool = candidates(row, target_date)
                        
                        if not pool:
                            st.error("⚠️ 當前沒有符合技能/語言或未額滿的可用居服員！")
                        else:
                            # 下拉選單顯示適配推薦
                            cg_options = {
                                f"{p['姓名']}（{p['居服員ID']}）🌟 適配度:{p['綜合分數']}分 (疲勞:{p['預估疲勞']}分)": p
                                for p in pool
                            }
                            selected_cg_label = st.selectbox("AI 推薦最適配居服員：", list(cg_options.keys()), key=f"triage_select_{row.id}")
                            selected_pick = cg_options[selected_cg_label]
                            
                            # 顯示首選推薦理由
                            reasons = []
                            if selected_pick["熟悉個案"]: reasons.append("🤝 歷史熟悉個案")
                            if selected_pick["居服員區域"] == row["區域"]: reasons.append("📍 同區近距離")
                            else: reasons.append("⚠️ 跨區支援")
                            st.caption("💡 **推薦理由與適配分析：** " + "｜".join(reasons))
                            
                            st.divider()
                            st.markdown("#### 📱 訊息通知預覽與注意事項")
                            
                            # 1. 傳給居服員的訊息與注意事項
                            cg_memo = st.text_area(
                                "傳給居服員的指派訊息（注意事項）：", 
                                value=f"【CareMatch 緊急派遣】{selected_pick['姓名']}您好：請前往 {row['區域']} 訪視 {row['姓名']} 長輩。"
                                      f"請務必攜帶血壓計，優先確認長輩意識狀態、呼吸狀況與量測血壓，有異常請立即回報督導。",
                                key=f"cg_msg_{row.id}"
                            )
                            
                            # 2. 傳給長輩/家屬的簡訊
                            case_msg = st.text_area(
                                "傳給長輩/家屬的通知簡訊：",
                                value=f"【CareMatch 關懷通知】{row['姓名']} 您好：系統偵測到您近期身體狀況較為波動，"
                                      f"督導已為您安排居服員 {selected_pick['姓名']} 前往訪視關懷，請您安心休息。",
                                key=f"case_msg_{row.id}"
                            )
                            
                            # 確定指派按鈕
                            if st.button("確定指派並發送雙向訊息", key=f"commit_triage_{row.id}"):
                                ok, message = commit(row, selected_pick, target_date)
                                if ok:
                                    st.success(f"✅ {message}")
                                    st.toast(f"📱 訊息已成功發送！【{row['姓名']}】已移至已處置區域。", icon="🚀")
                                    st.rerun() # 🌟 重新繪製頁面，該長輩卡片會立刻消失！
                                else:
                                    st.error(message)

    # 6. 底下新增一個區塊，展示「當日已完成處置之高風險個案紀錄」
    if assigned_case_ids:
        st.divider()
        st.subheader(f"✅ {target_date} 今日已完成處置之高風險巡視紀錄")
        df_assigned_today = pd.DataFrame([
            x for x in st.session_state.assignments 
            if x["服務日"] == target_date and x["個案ID"] in high_risk_cases["id"].values
        ])
        if not df_assigned_today.empty:
            st.dataframe(
                df_assigned_today[["服務日", "個案ID", "個案", "居服員ID", "居服員", "分鐘", "疲勞"]], 
                use_container_width=True, 
                hide_index=True
            )

with tabs[3]:
    st.header("🗺️ AI 人力供需預測與動態調度建議")
    st.caption("結合長輩健康風險趨勢與各時段服務需求，預測未來人力缺口並給予即時調度建議。")

    # 1. 取得選擇的排班日期
    target_date = st.session_state.get("service_date", date.today())
    
    # 可排班人力 (排除強制休息者)
    active_cg = caregivers[~caregivers["id"].isin(st.session_state.protected)]
    
    # ----------------------------------------------------
    # 核心邏輯：建立區域 × 時段 預測模型
    # ----------------------------------------------------
    periods = ["上午 (08:00-12:00)", "下午 (13:00-17:00)", "晚間 (18:00-21:00)"]
    
    # 模擬長輩偏好的服務時段 (根據需求與風險分配權重)
    demand_matrix = []
    for _, c in cases.iterrows():
        # 高風險長輩傾向需要上午與晚間雙重巡視
        if c["風險"] == "高":
            p_choices = ["上午 (08:00-12:00)", "晚間 (18:00-21:00)"]
        elif c["風險"] == "中":
            p_choices = ["上午 (08:00-12:00)", "下午 (13:00-17:00)"]
        else:
            p_choices = [random.choice(periods)]
            
        for p in p_choices:
            demand_matrix.append({
                "區域": c["區域"],
                "時段": p,
                "長輩ID": c["id"],
                "風險": c["風險"]
            })
            
    df_demand_detail = pd.DataFrame(demand_matrix)
    
    # 彙總各區域與時段的需求總量
    demand_summary = df_demand_detail.groupby(["區域", "時段"]).size().reset_index(name="預測需求人次")
    
    # 估算供給能力：假設每位居服員單一時段最多服務 1~2 案
    supply_by_district = active_cg["區域"].value_counts().to_dict()
    
    # 建立整合供需比較表
    forecast_rows = []
    for dist in DISTRICTS:
        available_cg_count = supply_by_district.get(dist, 0)
        # 假設各時段最大承載量為 該區居服員數 * 1.2
        capacity_per_period = int(available_cg_count * 1.2)
        
        for p in periods:
            sub = demand_summary[(demand_summary["區域"] == dist) & (demand_summary["時段"] == p)]
            req = int(sub["預測需求人次"].values[0]) if not sub.empty else 0
            gap = capacity_per_period - req
            
            status = "🟢 充沛" if gap >= 2 else ("🟡 緊繃" if gap >= 0 else "🔴 人力不足")
            
            forecast_rows.append({
                "區域": dist,
                "時段": p,
                "可排班居服員": available_cg_count,
                "預估時段供給力": capacity_per_period,
                "預測需求人次": req,
                "人力供需差額": gap,
                "供需狀態": status
            })
            
    df_forecast = pd.DataFrame(forecast_rows)

    # ----------------------------------------------------
    # UI 呈現一：頂部關鍵指標 Cards
    # ----------------------------------------------------
    total_shortage = df_forecast[df_forecast["人力供需差額"] < 0]["人力供需差額"].sum()
    tight_periods = len(df_forecast[df_forecast["供需狀態"] == "🔴 人力不足"])
    
    m1, m2, m3 = st.columns(3)
    m1.metric("👥 可排班居服員總數", f"{len(active_cg)} 人", f"受保護：{len(st.session_state.protected)} 人")
    m2.metric("🚨 預估人力不足時段數", f"{tight_periods} 個 時 段", delta_color="inverse")
    m3.metric("📉 全區總人力缺口", f"{abs(total_shortage)} 人", delta_color="inverse")

    st.divider()

    # ----------------------------------------------------
    # UI 呈現二：AI 智慧調度建議 (自動分析並產出建議)
    # ----------------------------------------------------
    st.subheader("💡 AI 即時調度與人力轉派建議")
    
    shortage_items = df_forecast[df_forecast["人力供需差額"] < 0]
    surplus_items = df_forecast[df_forecast["人力供需差額"] > 2]
    
    if shortage_items.empty:
        st.success("✅ 全區各時段人力配置均衡，暫無支援需求。")
    else:
        for _, short in shortage_items.iterrows():
            target_dist = short["區域"]
            target_period = short["時段"]
            needed = abs(short["人力供需差額"])
            
            # 尋找同時間有剩餘人力的鄰近區域
            helper = surplus_items[surplus_items["時段"] == target_period]
            
            with st.expander(f"🔴 **警告：{target_dist} 在【{target_period}】預估缺口 {needed} 人次**", expanded=True):
                st.write(f" **現狀分析**：該區可排班居服員 {short['可排班居服員']} 人，但該時段預測需求高達 {short['預測需求人次']} 人。")
                
                if not helper.empty:
                    best_helper = helper.iloc[0]
                    st.markdown(f"👉 **AI 建議解決方案**：可調派 **【{best_helper['區域']}】**（該時段剩餘容量 +{best_helper['人力供需差額']}）進行**跨區支援**。")
                    st.caption(f"💡 系統調配提示：調派 {best_helper['區域']} 居服員跨區支援 {target_dist}，交通時間預估增加 12~18 分鐘，可提供跨區津貼予以獎勵。")
                else:
                    st.markdown("👉 **AI 建議解決方案**：目前周邊區域人力皆緊繃，建議**啟動備勤居服員**或**調整低風險個案之服務時段**至下午。")

    st.divider()

    # ----------------------------------------------------
    # UI 呈現三：詳細供需數據矩陣表
    # ----------------------------------------------------
    st.subheader("📊 區域與時段人力供需詳細預測表")
    
    # 提供區域篩選器
    selected_dist = st.multiselect("篩選區域：", DISTRICTS, default=DISTRICTS, key="forecast_dist_filter")
    df_show = df_forecast[df_forecast["區域"].isin(selected_dist)]
    
    st.dataframe(
        df_show[["區域", "時段", "可排班居服員", "預估時段供給力", "預測需求人次", "人力供需差額", "供需狀態"]],
        use_container_width=True,
        hide_index=True
    )

with tabs[4]:
    st.header("AI 導入營運關鍵指標 KPI（依實際排班計算）")
    st.write("依本輪隨機資料進行一週每日排班模擬；不是固定預設值。重新產生資料或強制休息名單變動後，結果都會改變。")
    frame = efficiency_frame()
    if frame.empty:
        st.info("尚無排班紀錄。完成排班後，這裡才會產生每日與每週的真實效率指標。")
    else:
        st.dataframe(frame, use_container_width=True, hide_index=True)
        total = frame[["案件數", "總路程分鐘", "路程節省分鐘", "總公里", "高疲勞排班數"]].sum()
        x1, x2, x3, x4 = st.columns(4)
        x1.metric("已排案件", int(total["案件數"])); x2.metric("總路程時間", f"{total['總路程分鐘']:.1f} 分")
        x3.metric("相對基準節省", f"{total['路程節省分鐘']:.1f} 分")
        x4.metric("高疲勞排班", int(total["高疲勞排班數"]))
        weekly = frame.copy(); weekly["週"] = pd.to_datetime(weekly["日期"]).dt.to_period("W").astype(str)
        chart = weekly.groupby("週", as_index=False)[["總路程分鐘", "總公里", "高疲勞排班數"]].sum()
        fig = go.Figure()
        fig.add_bar(name="總路程分鐘", x=chart["週"], y=chart["總路程分鐘"])
        fig.add_bar(name="總公里", x=chart["週"], y=chart["總公里"])
        fig.update_layout(barmode="group", title="每週實際排班路程", template="plotly_white")
        st.plotly_chart(fig, use_container_width=True)
        st.caption("「基準」是該案件、該日期當下所有合格候選人的平均路程與疲勞；正值改善代表這次指派確實低於可選池平均。指標只根據已確認排班累計。")

