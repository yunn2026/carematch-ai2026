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
ADDRESSES = {
    "中正區": "台北市中正區忠孝西路一段",
    "大安區": "台北市大安區復興南路二段",
    "信義區": "台北市信義區市府路1號",
    "松山區": "台北市松山區八德路四段",
    "內湖區": "台北市內湖區內湖路一段"
}
# 比賽展示用的公開道路位置：不使用任何真實個案或居服員住址。
# 每一筆模擬資料都有不同起訖點，才能讓同區服務也使用 Google Routes API 計算。
DEMO_ROUTE_POINTS = {
    "中正區": ["台北市中正區忠孝西路一段50號", "台北市中正區杭州南路一段15號", "台北市中正區南昌路一段7號"],
    "大安區": ["台北市大安區新生南路二段1號", "台北市大安區和平東路二段134號", "台北市大安區復興南路二段237號"],
    "信義區": ["台北市信義區市府路1號", "台北市信義區松高路11號", "台北市信義區信義路五段7號"],
    "松山區": ["台北市松山區八德路四段138號", "台北市松山區南京東路四段2號", "台北市松山區敦化北路199巷"],
    "內湖區": ["台北市內湖區內湖路一段91巷", "台北市內湖區成功路四段30巷", "台北市內湖區洲子街12號"],
}
LANGUAGES = ["國語", "台語", "客語", "英語"]
SKILLS = ["生活照顧", "移位與搬運", "失智症照護", "傷口照護", "陪同就醫", "沐浴協助"]


def make_data(seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = random.Random(seed)
    people = []
    for i in range(1, 25):
        stress, overtime, commute = rng.randint(1, 9), rng.randint(0, 5), rng.randint(15, 55)
        risk = min(100, round(stress * 6.5 + overtime * 7 + max(commute - 30, 0) * 1.1 + rng.uniform(-7, 7), 1))
        district = rng.choice(DISTRICTS)
        people.append({
            "id": f"CG{i:03d}", "姓名": f"居服員_{i}", "區域": district,
            "路線位置": rng.choice(DEMO_ROUTE_POINTS[district]),
            "語言": rng.choice(LANGUAGES), "技能": rng.sample(SKILLS, rng.randint(3, 5)),
            "疲勞風險": risk, "壓力": stress, "近月加班": overtime
        })
    cases = []
    for i in range(1, 51):
        bp, activity = rng.randint(0, 5), rng.randint(0, 50)
        risk = "高" if bp >= 3 or activity >= 35 else "中" if bp >= 1 or activity >= 15 else "低"
        district = rng.choice(DISTRICTS)
        cases.append({
            "id": f"CASE{i:03d}", "姓名": f"長輩_{i}", "區域": district,
            "路線位置": rng.choice(DEMO_ROUTE_POINTS[district]),
            "語言": rng.choice(LANGUAGES), "需求技能": rng.sample(SKILLS, rng.randint(1, 2)),
            "熟悉居服員": rng.sample([p["id"] for p in people], 2), "血壓異常天數": bp,
            "活動下降": activity, "風險": risk, "服務日": rng.randrange(7)
        })
    return pd.DataFrame(people), pd.DataFrame(cases)


def reset_data() -> None:
    st.session_state.seed = random.SystemRandom().randint(1, 2_000_000_000)
    st.session_state.caregivers, st.session_state.cases = make_data(st.session_state.seed)
    st.session_state.protected = set()
    st.session_state.assignments = []
    st.session_state.route_cache = {}


def init() -> None:
    # 舊工作階段的模擬資料沒有「路線位置」欄位；自動重建一次即可相容新版路線計算。
    if (
        "caregivers" not in st.session_state
        or "路線位置" not in st.session_state.caregivers.columns
        or "路線位置" not in st.session_state.cases.columns
    ):
        reset_data()
    st.session_state.setdefault("protected", set())
    st.session_state.setdefault("assignments", [])
    st.session_state.setdefault("route_cache", {})
    try:
        secret_map_key = st.secrets.get("GOOGLE_MAPS_API_KEY", "")
    except Exception:
        secret_map_key = ""
    st.session_state.setdefault(
        "map_key", secret_map_key or os.getenv("GOOGLE_MAPS_API_KEY", "")
    )


def route(origin: str, destination: str) -> tuple[float, float, str, str]:
    """使用 Google Routes API (computeRoutes) 計算台北市兩區域間的車程與距離"""
    key = (origin, destination, st.session_state.map_key)
    if key in st.session_state.route_cache:
        return st.session_state.route_cache[key]
    
    if not st.session_state.map_key:
        result = (18.0, 5.0, "估算", "未設定 Google Maps API 金鑰")
    else:
        url = "https://routes.googleapis.com/directions/v2:computeRoutes"
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": st.session_state.map_key,
            "X-Goog-FieldMask": "routes.distanceMeters,routes.duration",
        }
        payload = {
            "origin": {"address": ADDRESSES.get(origin, origin)},
            "destination": {"address": ADDRESSES.get(destination, destination)},
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE",
            "languageCode": "zh-TW",
            "units": "METRIC"
        }
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=20)
            if not response.ok:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:120]}")
            
            data = response.json()
            if "routes" not in data or not data["routes"]:
                raise RuntimeError("未回傳有效路線資訊")
            
            route_data = data["routes"][0]
            duration_sec = float(route_data["duration"].rstrip("s"))
            distance_m = float(route_data["distanceMeters"])
            
            minutes = round(duration_sec / 60, 1)
            km = round(distance_m / 1000, 1)
            result = (minutes, km, "Google Routes API", "即時交通路線")
        except (requests.RequestException, RuntimeError, ValueError, IndexError, KeyError):
            result = (
                18.0,
                5.0,
                "估算",
                "Google 即時路線暫時連線逾時，已改用預估路程"
            )

    st.session_state.route_cache[key] = result
    return result


def load_for(cg_id: str, target_date: date) -> int:
    return sum(x["居服員ID"] == cg_id and x["服務日"] == target_date for x in st.session_state.assignments)


def candidates(case: pd.Series, target_date: date, use_maps: bool = True) -> list[dict]:
    rows = []
    for _, cg in st.session_state.caregivers.iterrows():
        if cg.id in st.session_state.protected or load_for(cg.id, target_date) >= 3:
            continue
        if not set(case["需求技能"]).issubset(set(cg["技能"])):
            continue
        if cg["語言"] not in ("國語", case["語言"]):
            continue
        if use_maps:
            minutes, km, source, detail = route(cg["路線位置"], case["路線位置"])
        else:
            minutes = 4.0 if cg["區域"] == case["區域"] else 18.0
            km, source, detail = (1.2 if cg["區域"] == case["區域"] else 5.0), "模型估算", "週期模擬"
            
        familiar, same_area = cg.id in case["熟悉居服員"], cg["區域"] == case["區域"]
        projected = min(100, cg["疲勞風險"] + 6 + minutes / 12 + load_for(cg.id, target_date) * 9)
        score = 100 + (13 if familiar else 0) + (7 if same_area else 0) - minutes * 1.25 - projected * .32
        
        rows.append({
            "居服員ID": cg.id, "姓名": cg["姓名"], "居服員區域": cg["區域"], "技能": cg["技能"], 
            "路程分鐘": minutes, "公里": km, "路線來源": source, "路線說明": detail, 
            "原疲勞": cg["疲勞風險"], "預估疲勞": round(projected, 1),
            "綜合分數": round(score, 1), "熟悉個案": familiar
        })
    return sorted(rows, key=lambda r: r["綜合分數"], reverse=True)


def three_options(pool: list[dict]) -> list[tuple[str, dict]]:
    if not pool:
        return []
    picked = []
    rules = [
        ("方案 A｜整體最佳", lambda r: (-r["綜合分數"],)),
        ("方案 B｜距離最近", lambda r: (r["路程分鐘"], r["公里"], -r["綜合分數"])),
        ("方案 C｜負荷最低", lambda r: (r["預估疲勞"], r["路程分鐘"]))
    ]
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
        "服務日": target_date, "通知狀態": "通知已產生", "個案ID": case.id, "個案": case["姓名"], 
        "居服員ID": choice["居服員ID"], "居服員": choice["姓名"], 
        "分鐘": choice["路程分鐘"], "公里": choice["公里"], "疲勞": choice["預估疲勞"],
        "基準分鐘": sum(p["路程分鐘"] for p in peers) / len(peers), 
        "基準疲勞": sum(p["預估疲勞"] for p in peers) / len(peers)
    })
    return True, f"已成功儲存 {target_date} 的排班紀錄！"


st.set_page_config(page_title="CareMatch AI", layout="wide")
init()
notification_toast = st.session_state.pop("notification_toast", None)
if notification_toast:
    st.toast(notification_toast, icon="🔔")

with st.sidebar:
    st.header("資料與地圖設定")
    if st.button("重新產生隨機資料", use_container_width=True):
        reset_data()
        st.rerun()
    st.caption("路線資料由系統安全設定提供；若暫時無法連線，將使用預估路程備援。")

# 🌟 禁止瀏覽器自動翻譯日曆與選單元件
st.markdown("""
<style>
    div[data-baseweb="calendar"], 
    div[data-baseweb="popover"],
    div[data-baseweb="select"] {
        translate: no !important;
    }

    /* 讓三大媒合方案卡更緊湊、易讀 */
    div[data-testid="stHorizontalBlock"] h3 {
        font-size: 1.25rem;
        margin-top: 0.2rem;
        margin-bottom: 0.25rem;
    }

    div[data-testid="stHorizontalBlock"] h4 {
        font-size: 1.08rem;
        margin-top: 0.5rem;
        margin-bottom: 0.2rem;
    }

    div[data-testid="stHorizontalBlock"] p {
        font-size: 0.98rem;
        line-height: 1.35;
        margin-bottom: 0.12rem;
    }

    div[data-testid="stHorizontalBlock"] hr {
        margin-top: 0.45rem;
        margin-bottom: 0.45rem;
    }
</style>
""", unsafe_allow_html=True)

st.title("CareMatch AI 居家照護排班")
st.caption("派案流程：① 預測人力需求　→　② 偵測並保護過勞居服員　→　③ AI 智慧媒合　→　④ 確認派案與通知　→　⑤ 檢視營運成效")

tabs = st.tabs([ "🗺️ 今日供需／臨時異動",
    "🧠 過勞風險預警",
    "🎯 AI 智慧媒合",
    "🔔 派案確認與通知",
    "📈 排班結果與成效",
    "📱 居服員服務回覆（展示）"])

caregivers, cases = st.session_state.caregivers, st.session_state.cases



def efficiency_frame() -> pd.DataFrame:
    if not st.session_state.assignments:
        return pd.DataFrame()
        
    df = pd.DataFrame(st.session_state.assignments)
    grouped = df.groupby("服務日", as_index=False).agg(
        案件數=("個案ID", "count"),
        總路程分鐘=("分鐘", "sum"),
        基準路程分鐘=("基準分鐘", "sum"),
        總公里=("公里", "sum"),
        平均預估疲勞=("疲勞", "mean"),
        基準疲勞=("基準疲勞", "mean"),
        高疲勞排班數=("疲勞", lambda x: sum(v >= 70 for v in x))
    )
    
    grouped["路程節省分鐘"] = round(grouped["基準路程分鐘"] - grouped["總路程分鐘"], 1)
    grouped["疲勞改善"] = round(grouped["基準疲勞"] - grouped["平均預估疲勞"], 1)
    grouped["日期"] = grouped["服務日"].astype(str)
    
    return grouped.sort_values("日期")


with tabs[1]:
    st.header("🧠 過勞風險預警與保護機制")
    
    high_risk_cg = caregivers[caregivers["疲勞風險"] >= 60].sort_values("疲勞風險", ascending=False)
    pending_risk_cg = high_risk_cg[~high_risk_cg["id"].isin(st.session_state.protected)]
    protected_cg = caregivers[caregivers["id"].isin(st.session_state.protected)]
    
    st.subheader("⚠️ 高疲勞預警名單（需督導評估是否強制休息（停止本日派案））")
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

    st.subheader("🛡️ 保護中的居服員已自動排除於 AI 智慧媒合與緊急個案派案名單")
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

with tabs[2]:
    st.header("🎯 三大 AI 調度方案與決策分析")
    st.caption("結合多目標優化演算法與可解釋 AI，清晰呈現各推薦人選的適配優勢與風險提醒。")
    
    col_case, col_date = st.columns([2, 1])
    
    with col_case:
        # 派案成功後，下一次重新整理前先套用要切換的個案，避免在同一輪修改既有元件狀態。
        if "next_case_picker_index" in st.session_state:
            st.session_state.case_picker_index = st.session_state.pop(
                "next_case_picker_index"
            )
        selected_index = st.selectbox(
            "選擇匹配／派單個案：", 
            options=cases.index, 
            format_func=lambda idx: f"{cases.loc[idx, 'id']} - {cases.loc[idx, '姓名']}（{cases.loc[idx, '區域']}）",
            key="case_picker_index"
        )
        
    with col_date:
        selected_date = st.date_input("選擇排班日期", value=date.today(), key="service_date")
        
    case = cases.iloc[selected_index]
    st.caption(f"目前排班日期：{selected_date}｜同一位居服員每日最多安排 3 案。")
    selected_id = case["id"]
    
    st.info(f"**選定長輩資訊：{case['姓名']}（{case['id']}）**｜區域：{case['區域']}｜需求標籤：{', '.join(case['需求技能'])}｜語言：{case['語言']}")
    
    pool = candidates(case, selected_date)
    choices = three_options(pool)
    existing_assignment = next(
        (
            assignment
            for assignment in st.session_state.assignments
            if assignment["個案ID"] == selected_id
            and assignment["服務日"] == selected_date
        ),
        None,
    )
    
    if not choices:
        st.warning(f"在 {selected_date} 沒有符合技能、語言、強制休息或當日額滿的可用居服員。")
    else:
        st.subheader(f"💡 AI 推薦三大調度方案比對（排班日期：{selected_date}）")
        st.caption("系統已先排除保護中與當日服務量達上限的人員；督導仍可依現場需求做最終判斷。")
        cols = st.columns(3)
                
        
        for col, (label, pick) in zip(cols, choices):
            with col:
                with st.container(border=True):
                    st.markdown(f"### {label}")
                    st.caption({"方案 A｜整體最佳": "兼顧連續性、路程及疲勞平衡", "方案 B｜距離最近": "優先最短移動時間與距離", "方案 C｜負荷最低": "優先避免居服員過勞"}[label])
                    st.divider()
                    st.subheader(f"👤 {pick['姓名']}（{pick['居服員ID']}）")

                    st.markdown(
                        f"""
                        <div style="
                            display: flex;
                            justify-content: space-between;
                            align-items: center;
                            background: #eff6ff;
                            border-radius: 8px;
                            padding: 0.45rem 0.7rem;
                            margin: 0.35rem 0 0.55rem 0;
                        ">
                            <span style="font-size: 0.95rem; color: #475569;">
                                🏆 綜合適配分數
                            </span>
                            <span style="font-size: 1.55rem; font-weight: 700; color: #2563eb;">
                                {pick['綜合分數']} 分
                            </span>
                        </div>
                        """,
                        unsafe_allow_html=True
                    )
                    skills_text = ", ".join(pick.get("技能", [])) if pick.get("技能") else "無特別紀錄"
                    st.caption(f"🛠️ **居服員專長**：{skills_text}")
                    
                    
                    st.caption(
                        f"🚗 路程：**{pick['路程分鐘']} 分**（{pick['公里']} km）"
                    )
                    st.caption(
                        f"⚡ 預估疲勞：**{pick['預估疲勞']} 分**"
                    )
                    
                    st.divider()
                    st.markdown("#### 🔍  AI 推薦重點")
                    
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
                        
                    for p in pros[:2]:
                        st.markdown(f"• {p}")
                    
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
                        
                    if cons:
                        st.markdown("#### ⚠️ 督導提醒")
                        st.markdown(f"• {cons[0]}")
                        
                    st.markdown("<div style='height: 0.35rem;'></div>", unsafe_allow_html=True)
                    st.caption(f"🗺️ 路線資料來源：{pick['路線來源']}（{pick['路線說明']}）")
                    
                    is_selected_worker = (existing_assignment is not None and existing_assignment["居服員ID"] == pick["居服員ID"])

                    if st.button("✅ 已派遣給此人"if is_selected_worker else ("此個案已派遣" if existing_assignment else "採用此方案派單")
                                 ,key=f"choose_{label}_{selected_id}_{selected_date}",
                                 use_container_width=True,disabled=existing_assignment is not None,):
                        ok, message = commit(case, pick, selected_date)
                        if ok:
                            st.success(message)
                            all_case_indices = list(cases.index)
                            assigned_ids_today = {
                                item["個案ID"]
                                for item in st.session_state.assignments
                                if item["服務日"] == selected_date
                            }
                            pending_indices = [
                                idx
                                for idx in all_case_indices
                                if cases.loc[idx, "id"] not in assigned_ids_today
                            ]
                            current_position = all_case_indices.index(selected_index)
                            next_order = (
                                all_case_indices[current_position + 1:]
                                + all_case_indices[:current_position]
                            )
                            next_index = next(
                                (idx for idx in next_order if idx in pending_indices),
                                None,
                            )
                            if next_index is not None:
                                st.session_state.next_case_picker_index = next_index
                                st.session_state.notification_toast = (
                                    "派案已確認，通知已產生；已切換至下一位待排個案。"
                                )
                            else:
                                st.session_state.notification_toast = (
                                    "派案已確認，通知已產生；這個日期的個案都已完成派遣。"
                                )
                            st.rerun()
                        else:
                            st.error(message)

        if len({x[1]['居服員ID'] for x in choices}) < 3:
            st.caption("💡 合格人選不足三位，因此部分方案使用相同候選人；這不是顯示錯誤。")

    st.divider()
    st.subheader("📋 目前已排定之班表總覽")
    st.caption("班表會依你在「智慧媒合」中選擇的服務日期，記錄個案、居服員、通勤時間與疲勞指數。")
    if st.session_state.assignments:
        for assignment in st.session_state.assignments:
            assignment.setdefault("通知狀態", "通知已產生")
            assignment.setdefault("服務狀態", "已通知")

        df_assign = pd.DataFrame(st.session_state.assignments)
        assignment_options = {
            f"{row['服務日']}｜{row['個案ID']}｜{row['個案']} → {row['居服員']}": index
            for index, row in df_assign.iterrows()
        }

        selected_assignment = st.selectbox(
            "需要調整哪一筆派案？",
            options=list(assignment_options.keys()),
            key="assignment_adjustment"
        )

        selected_index = assignment_options[selected_assignment]
        current_status = st.session_state.assignments[selected_index].setdefault(
            "服務狀態", "已通知"
        )

        st.caption(f"目前服務狀態：**{current_status}**")
        if st.button(
            "取消派案，重新媒合",
            key=f"cancel_{selected_index}",
            use_container_width=True
        ):
            st.session_state.assignments.pop(selected_index)
            st.success("已取消派案；居服員名額已釋放，可重新進行 AI 媒合。")
            st.rerun()
            
        st.divider()
        st.subheader("指定日期班表")

        view_date = st.date_input(
            "選擇要查看的服務日期",
            value=date.today(),
            key="schedule_view_date"
        )

        day_schedule = df_assign[df_assign["服務日"] == view_date]

        if day_schedule.empty:
            st.info(f"{view_date} 尚無已確認的派案。")
        else:
            st.dataframe(
                day_schedule,
                use_container_width=True,
                hide_index=True
            )
            st.download_button(
                "下載此日期班表 CSV",
                data=day_schedule.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"CareMatch_班表_{view_date:%Y%m%d}.csv",
                mime="text/csv"
            )
            
        
    else:
        st.caption("尚無任何排班紀錄。請點選上方方案按鈕進行排班。")

with tabs[3]:
    st.header("🔔 臨時異動與高優先派案中心")
    st.caption("當原排班人員臨時無法服務或個案優先度升高時，督導可快速檢視候選人、調整通知並確認派案。")
    
    target_date = st.session_state.get("service_date", date.today())

    assigned_case_ids = {
        x["個案ID"]
        for x in st.session_state.assignments
        if x["服務日"] == target_date
    }

    high_risk_cases = cases[cases["風險"] == "高"]
    medium_risk_cases = cases[cases["風險"] == "中"]
    low_risk_cases = cases[cases["風險"] == "低"]

    pending_cases = high_risk_cases[
        ~high_risk_cases["id"].isin(assigned_case_ids)
    ]
    pending_medium_cases = medium_risk_cases[
        ~medium_risk_cases["id"].isin(assigned_case_ids)
    ]
    pending_low_cases = low_risk_cases[
        ~low_risk_cases["id"].isin(assigned_case_ids)
    ]

    available_caregivers = caregivers[
        ~caregivers["id"].isin(st.session_state.protected)
    ]

    st.subheader(f"🚨 待處理：高優先個案與臨時異動（{target_date}）")

    a, b, c, d = st.columns(4)
    a.metric("🔴 待處理高優先個案", len(pending_cases))
    b.metric("🟡 待追蹤優先個案", len(pending_medium_cases))
    c.metric("🟢 一般待服務個案", len(pending_low_cases))
    d.metric("🛡️ 可調度居服員", len(available_caregivers))

    st.divider()
    if pending_cases.empty:
        st.success(f"🎉 {target_date} 的高優先個案都已完成派案。")
    else:
        st.caption(f"目前還有 {len(pending_cases)} 位高優先個案尚未指派：")

        for idx, row in pending_cases.iterrows():
            with st.container(border=True):
                c1, c2, c3 = st.columns([2, 3, 2])

                with c1:
                    st.markdown(f"### 👵 **{row['姓名']}（{row['id']}）**")
                    st.caption(f"📍 區域：{row['區域']}｜語言：{row['語言']}")

                with c2:
                    st.write(f"🩸 **血壓異常天數：** 近一週連續 **{row['血壓異常天數']}** 天")
                    st.write(f"📉 **活動下降比例：** 近三日下降 **{row['活動下降']}%**")
                    st.write(f"🏷️ **需求技能：** {', '.join(row['需求技能'])}")

                with c3:
                    with st.popover("🚨 一鍵指派緊急巡視", use_container_width=True):
                        st.write(f"**為 {row['姓名']} 安排緊急巡視**（日期：{target_date}）")
                        pool = candidates(row, target_date)

                        if not pool:
                            st.error("⚠️ 沒有符合條件的可用居服員。")
                        else:
                            cg_options = {
                                f"{p['姓名']}（{p['居服員ID']}）｜適配度 {p['綜合分數']} 分": p
                                for p in pool
                            }
                            selected_cg_label = st.selectbox(
                                "AI 推薦居服員",
                                list(cg_options.keys()),
                                key=f"triage_select_{row['id']}"
                            )
                            selected_pick = cg_options[selected_cg_label]
                            reasons = []

                            if selected_pick["熟悉個案"]:
                               reasons.append("🤝 曾服務過此個案，交接成本較低")

                            if selected_pick["居服員區域"] == row["區域"]:
                               reasons.append("📍 同區服務，交通時間較短")
                            else:
                                reasons.append("🚗 跨區支援，請留意交通時間")
                            if selected_pick["預估疲勞"] < 60:
                                reasons.append("🧠 預估疲勞在可接受範圍")
                            st.caption("💡 **AI 推薦理由：** " + "｜".join(reasons))
                            st.divider()
                            st.markdown("#### 📱 通知預覽與服務注意事項")
                            cg_memo = st.text_area("傳給居服員的指派訊息（可自行調整）：",
                                                   value=(f"【CareMatch 緊急派遣】{selected_pick['姓名']}您好："
                                                          f"請前往 {row['區域']} 訪視 {row['姓名']}。"
                                                          f"請優先確認意識、呼吸與血壓狀況；如有異常請立即回報督導。"),
                                                   key=f"cg_msg_{row['id']}")
                            case_msg = st.text_area("派案確認後傳給長輩／家屬的通知內容（可自行調整）：",
                                                    value=(f"【CareMatch 關懷通知】{row['姓名']}您好："
                                                           f"督導已安排居服員 {selected_pick['姓名']} 前往訪視關懷，請安心休息。"),
                                                    key=f"case_msg_{row['id']}")

                            if st.button("確認派案並產生通知", key=f"commit_triage_{row['id']}"):
                                ok, message = commit(row, selected_pick, target_date)
                                if ok:
                                    st.session_state.notification_toast = (
                                        f"派案已確認，{row['姓名']} 的通知已產生。"
                                    )
                                    st.rerun()
                                else:
                                    st.error(message)

    st.divider()

    if assigned_case_ids:
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

with tabs[0]:
    st.header("🗺️ AI 人力供需預測與動態調度建議")
    st.caption("結合長輩健康風險趨勢與各時段服務需求，預測未來人力缺口並給予即時調度建議。")

    target_date = st.session_state.get("service_date", date.today())
    active_cg = caregivers[~caregivers["id"].isin(st.session_state.protected)]
    
    periods = ["上午 (08:00-12:00)", "下午 (13:00-17:00)", "晚間 (18:00-21:00)"]
    demand_matrix = []
    for _, c in cases.iterrows():
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
    demand_summary = df_demand_detail.groupby(["區域", "時段"]).size().reset_index(name="預測需求人次")
    supply_by_district = active_cg["區域"].value_counts().to_dict()
    
    forecast_rows = []
    for dist in DISTRICTS:
        available_cg_count = supply_by_district.get(dist, 0)
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

    total_shortage = df_forecast[df_forecast["人力供需差額"] < 0]["人力供需差額"].sum()
    tight_periods = len(df_forecast[df_forecast["供需狀態"] == "🔴 人力不足"])
    
    m1, m2, m3 = st.columns(3)
    m1.metric("👥 可排班居服員總數", f"{len(active_cg)} 人", f"受保護：{len(st.session_state.protected)} 人")
    m2.metric("🚨 預估人力不足時段數", f"{tight_periods} 個 時 段", delta_color="inverse")
    m3.metric("📉 全區總人力缺口", f"{abs(total_shortage)} 人", delta_color="inverse")

    st.divider()

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

    st.subheader("📊 區域與時段人力供需詳細預測表")
    
    selected_dist = st.multiselect("篩選區域：", DISTRICTS, default=DISTRICTS, key="forecast_dist_filter")
    df_show = df_forecast[df_forecast["區域"].isin(selected_dist)]
    
    st.dataframe(
        df_show[["區域", "時段", "可排班居服員", "預估時段供給力", "預測需求人次", "人力供需差額", "供需狀態"]],
        use_container_width=True,
        hide_index=True
    )

with tabs[4]:
    st.header("AI 導入營運關鍵指標 KPI（依實際排班計算）")
    st.caption("成效比較將聚焦四項指標：交通移動、居服員負荷、服務連續性與督導調度時間。")
    st.write("依本輪隨機資料進行一週每日排班模擬；不是固定預設值。重新產生資料或強制休息名單變動後，結果都會改變。")
    frame = efficiency_frame()
    if frame.empty:
        st.info("尚無排班紀錄。完成排班後，這裡才會產生每日與每週的真實效率指標。")
    else:
        st.dataframe(frame, use_container_width=True, hide_index=True)
        total = frame[["案件數", "總路程分鐘", "路程節省分鐘", "總公里", "高疲勞排班數"]].sum()
        x1, x2, x3, x4 = st.columns(4)
        x1.metric("已排案件", int(total["案件數"]))
        x2.metric("總路程時間", f"{total['總路程分鐘']:.1f} 分")
        x3.metric("相對基準節省", f"{total['路程節省分鐘']:.1f} 分")
        x4.metric("高疲勞排班", int(total["高疲勞排班數"]))
        weekly = frame.copy()
        weekly["週"] = pd.to_datetime(weekly["日期"]).dt.to_period("W").astype(str)
        chart = weekly.groupby("週", as_index=False)[["總路程分鐘", "總公里", "高疲勞排班數"]].sum()
        fig = go.Figure()
        fig.add_bar(name="總路程分鐘", x=chart["週"], y=chart["總路程分鐘"])
        fig.add_bar(name="總公里", x=chart["週"], y=chart["總公里"])
        fig.update_layout(barmode="group", title="每週實際排班路程", template="plotly_white")
        st.plotly_chart(fig, use_container_width=True)
        st.caption("「基準」是該案件、該日期當下所有合格候選人的平均路程與疲勞；正值改善代表這次指派確實低於可選池平均。指標只根據已確認排班累計。")

with tabs[5]:
    st.header("📱 居服員服務回覆")
    st.caption("比賽展示用：模擬居服員收到派案後，自行確認接案與回報服務完成。")

    if not st.session_state.assignments:
        st.info("目前沒有待回覆的派案。請先到「AI 智慧媒合」完成派案。")
    else:
        worker_options = {
            f"{item['居服員']}（{item['居服員ID']}）": item["居服員ID"]
            for item in st.session_state.assignments
        }

        selected_worker_label = st.selectbox(
            "切換展示中的居服員身分：",
            list(worker_options.keys()),
            key="caregiver_reply_worker"
        )
        selected_worker_id = worker_options[selected_worker_label]

        my_assignments = [
            (index, item)
            for index, item in enumerate(st.session_state.assignments)
            if item["居服員ID"] == selected_worker_id
        ]

        st.subheader("我的待處理服務")

        for assignment_index, item in my_assignments:
            status = item.setdefault("服務狀態", "已通知")

            with st.container(border=True):
                st.markdown(f"### 👵 {item['個案']}（{item['個案ID']}）")
                st.write(f"📅 服務日期：{item['服務日']}")
                st.write("📍 服務地點：大安區・信義路三段附近（確認接案後提供完整地址）")
                st.write(f"📍 預估交通時間：{item['分鐘']} 分鐘")
                st.write(f"📌 目前狀態：**{status}**")

                if status == "已通知":
                    st.caption("🔒 為保護長輩個資，完整門牌與聯絡方式將於確認接案後顯示。")
                    st.info("督導已發出派案通知，請確認是否可服務。")

                    if st.button(
                        "✅ 確認接案",
                        key=f"caregiver_confirm_{assignment_index}",
                        use_container_width=True
                    ):
                        item["服務狀態"] = "居服員已確認"
                        st.toast("已回覆督導：確認接案", icon="✅")
                        st.rerun()

                elif status == "居服員已確認":
                    st.success("你已確認接案，請依服務時間前往個案住處。")
                    st.info("📍 完整服務地址：台北市大安區信義路三段 100 號")
                    st.caption("📞 個案聯絡人：王小姐｜電話：0912-345-678（比賽展示資料）")

                    if st.button(
                        "🏁 完成服務並回報",
                        key=f"caregiver_done_{assignment_index}",
                        use_container_width=True
                    ):
                        item["服務狀態"] = "已完成服務"
                        st.toast("服務完成回報已送出", icon="🎉")
                        st.rerun()

                else:
                    st.success("🎉 此服務已完成，感謝你的照護服務。")
