import streamlit as st
import json

# Dummy data for now — replace with real Firestore calls later
DUMMY_REQUESTS = [
    {
        "requestId": "REQ_001",
        "rawText": "Help!! 6 people stuck on roof near City Bank",
        "language": "English",
        "urgency": "HIGH",
        "peopleCount": 6,
        "vulnerable": True,
        "vulnerableDetails": "2 children",
        "location": "Market Road",
        "status": "PENDING",
        "needs": ["evacuation"]
    },
    {
        "requestId": "REQ_002",
        "rawText": "मेरे घर में पानी भर गया है, 4 लोग हैं, बुजुर्ग माँ है",
        "language": "Hindi",
        "urgency": "CRITICAL",
        "peopleCount": 4,
        "vulnerable": True,
        "vulnerableDetails": "elderly woman, cannot walk",
        "location": "Ward 7",
        "status": "PENDING",
        "needs": ["evacuation", "medical"]
    },
    {
        "requestId": "REQ_003",
        "rawText": "HELP ward7 paani bahut zyada 3 log please boat jaldi",
        "language": "Mixed",
        "urgency": "HIGH",
        "peopleCount": 3,
        "vulnerable": False,
        "vulnerableDetails": "",
        "location": "Ward 7",
        "status": "PENDING",
        "needs": ["evacuation"]
    }
]

DUMMY_RESOURCES = {
    "boats": 5,
    "volunteer_teams": 5,
    "shelters": 4,
    "medical_units": 2
}

# ─────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────
st.set_page_config(
    page_title="RescueIQ",
    page_icon="🆘",
    layout="wide"
)

# ─────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────
col1, col2 = st.columns([3, 1])
with col1:
    st.title("🆘 RescueIQ — Relief Coordination")
    st.caption("AI-powered disaster relief coordination system")
with col2:
    st.metric("Status", "🔴 ACTIVE")

st.divider()

# ─────────────────────────────────────────
# DASHBOARD METRICS
# ─────────────────────────────────────────
st.subheader("📊 Live Dashboard")
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Pending Requests", "23", "3 critical")
m2.metric("Boats Available", "5", "-2 dispatched")
m3.metric("Volunteer Teams", "5", "3 active")
m4.metric("Shelters", "4", "745 capacity")
m5.metric("Resolved Today", "12", "+4 since morning")

st.divider()

# ─────────────────────────────────────────
# TABS
# ─────────────────────────────────────────
tab1, tab2, tab3, tab4 = st.tabs([
    "📥 Intake",
    "🚀 Dispatch",
    "🔄 Replan",
    "📊 Metrics"
])

# ─────────────────────────────────────────
# TAB 1 — INTAKE
# ─────────────────────────────────────────
with tab1:
    st.subheader("Incoming Help Requests")
    
    col1, col2 = st.columns([2, 1])
    
    with col1:
        message = st.text_area(
            "Paste message (any language — Hindi, Tamil, English, mixed)",
            placeholder="e.g. मेरे घर में पानी भर गया है, 4 लोग हैं...",
            height=100
        )
        
        if st.button("🔍 Process Request", type="primary"):
            if message:
                with st.spinner("Extracting information with Gemini..."):
                    # TODO: Replace with real intake agent call
                    st.success("✅ Request extracted!")
                    st.json({
                        "language": "Hindi",
                        "translated_text": "Water has filled my house, 4 people, elderly mother cannot walk",
                        "location_description": "Ward 7, near temple",
                        "people_count": 4,
                        "urgency": "CRITICAL",
                        "vulnerable": True,
                        "vulnerable_details": "elderly woman, cannot walk",
                        "needs": ["evacuation", "medical"],
                        "confidence": 0.95
                    })
                    
                    if st.button("➕ Add to Queue"):
                        st.success("Added to dispatch queue!")
            else:
                st.warning("Please enter a message")
    
    with col2:
        st.subheader("Queue")
        for req in DUMMY_REQUESTS:
            color = "🔴" if req["urgency"] == "CRITICAL" else "🟠"
            with st.expander(
                f"{color} {req['requestId']} — {req['location']}"
            ):
                st.write(f"**Language:** {req['language']}")
                st.write(f"**People:** {req['peopleCount']}")
                st.write(f"**Urgency:** {req['urgency']}")
                st.write(f"**Needs:** {', '.join(req['needs'])}")
                if req['vulnerable']:
                    st.warning(f"⚠️ {req['vulnerableDetails']}")

# ─────────────────────────────────────────
# TAB 2 — DISPATCH
# ─────────────────────────────────────────
with tab2:
    st.subheader("Dispatch Plan")
    
    if st.button("🤖 Generate Dispatch Plan", type="primary"):
        with st.spinner("AI generating optimal assignments..."):
            st.success("✅ Dispatch plan generated!")
            
            # Dummy plan — replace with real dispatch agent
            st.subheader("Recommended Assignments")
            
            with st.container(border=True):
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.markdown("**Assignment 1** 🔴 CRITICAL")
                    st.write("BOAT_03 → Ward 7 (4 people, elderly woman)")
                    st.write("⏱️ ETA: 12 minutes")
                    st.write("📍 Route: Zone B Dock → Bridge Road → Ward 7")
                    st.info(
                        "**Why:** Life-critical case. Elderly woman cannot "
                        "walk. Nearest available boat with sufficient capacity."
                    )
                with col2:
                    st.button("✅ Approve", key="approve1", type="primary")
                    st.button("✏️ Modify", key="modify1")

            with st.container(border=True):
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.markdown("**Assignment 2** 🟠 HIGH")
                    st.write("TEAM_02 → Market Road (6 people, 2 children)")
                    st.write("⏱️ ETA: 8 minutes")
                    st.write("📍 Route: Zone B Hall → Market Road")
                    st.info(
                        "**Why:** Accessible by foot. Volunteer team has "
                        "child evacuation training. No boat needed."
                    )
                with col2:
                    st.button("✅ Approve", key="approve2", type="primary")
                    st.button("✏️ Modify", key="modify2")

            st.warning(
                "⚠️ **Tradeoff:** Serving Ward 7 first delays Market Road "
                "by 8 minutes. Recommended because elderly member is "
                "life-critical and 2nd group is on 2nd floor (stable)."
            )
            
            if st.button("✅ Approve All Assignments", type="primary"):
                st.success("All assignments approved! Volunteers notified.")

# ─────────────────────────────────────────
# TAB 3 — REPLAN
# ─────────────────────────────────────────
with tab3:
    st.subheader("🔄 What-If Replanning")
    
    st.write("Simulate disruptions and see how the system replans:")
    
    col1, col2 = st.columns(2)
    
    with col1:
        event = st.selectbox(
            "Select disruption event:",
            [
                "Bridge Road closes (flooding)",
                "BOAT_03 breaks down",
                "New CRITICAL request arrives",
                "Volunteer Team unavailable"
            ]
        )
        
        if st.button("🚨 Simulate Event", type="primary"):
            with st.spinner("Replanning..."):
                st.error(f"🚨 EVENT: {event}")
                
                col_a, col_b = st.columns(2)
                with col_a:
                    st.subheader("Before")
                    st.write("BOAT_03 → Ward 7: **9 min**")
                    st.write("Coverage: **100%**")
                    st.write("Violations: **0**")
                
                with col_b:
                    st.subheader("After Replan")
                    st.write("BOAT_03 → Ward 7: **16 min** (+7)")
                    st.write("Coverage: **100%**")
                    st.write("Violations: **0**")
                
                st.info(
                    "**Explanation:** Bridge Road closed. Rerouted BOAT_03 "
                    "via Market Road (+7 minutes). Ward 7 group is stable "
                    "(2nd floor, water not rising). Delay is acceptable. "
                    "No critical risk increase."
                )
                
                if st.button("✅ Approve Replan", type="primary"):
                    st.success("Replan approved! Volunteers notified of new routes.")

# ─────────────────────────────────────────
# TAB 4 — METRICS
# ─────────────────────────────────────────
with tab4:
    st.subheader("📊 Performance Metrics")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("vs Baseline Comparison")
        
        metrics_data = {
            "Metric": [
                "Avg Response Time",
                "Coverage",
                "Critical Cases First",
                "Constraint Violations",
                "Requests/hour"
            ],
            "Baseline": ["42 min", "71%", "34%", "18", "12"],
            "RescueIQ": ["11 min", "96%", "98%", "1", "47"]
        }
        
        st.table(metrics_data)
    
    with col2:
        st.subheader("Extraction Accuracy")
        st.progress(0.98, text="English: 98%")
        st.progress(0.94, text="Hindi: 94%")
        st.progress(0.91, text="Tamil: 91%")
        st.progress(0.87, text="Mixed: 87%")
        
        st.subheader("Today's Summary")
        st.metric("Requests Processed", "47", "+12 vs yesterday")
        st.metric("People Helped", "189", "+43 vs yesterday")
        st.metric("Avg Response Time", "11 min", "-31 min vs baseline")