import streamlit as st
import requests
from bs4 import BeautifulSoup
import pandas as pd
import re
import os

st.set_page_config(page_title="Cumberland River Flow Rates", layout="centered")

# Inject custom CSS for faded background image using base64-encoded image
from utils import get_img_as_base64
img_path = "img/james-wheeler-HJhGcU_IbsQ-unsplash.jpg"
img_base64 = get_img_as_base64(img_path)
st.markdown(f"""
    <style>
    [data-testid="stAppViewContainer"]::before {{
        content: "";
        position: fixed;
        top: 0;
        left: 0;
        width: 100vw;
        height: 100vh;
        background: url('data:image/jpeg;base64,{img_base64}') center/cover no-repeat;
        opacity: 0.55; /* fade overlay */
        z-index: 0;
        pointer-events: none;
    }}
    [data-testid="stAppViewContainer"] > * {{
        position: relative;
        z-index: 1;
    }}
    </style>
""", unsafe_allow_html=True)

st.title("Cumberland River Downstream Flow Calculator")

st.markdown("""
<span style='font-size: 1rem;'>
<strong>Instructions:</strong><br>
Retrieve the "Average Hourly Discharge" (in CFS) from the <a href="https://www.tva.com/environment/lake-levels/Old-Hickory" target="_blank">TVA Old Hickory Dam lake levels page</a>.<br>
Enter it below to calculate the flow rates at each mile marker downstream.
</span>
""", unsafe_allow_html=True)

# --- Load dams from static JSON file and select dam before any calculations ---
import json
with open("cumberland_dams.json", "r") as f:
    dams = json.load(f)
if not dams:
    st.error("Could not load dam data from cumberland_dams.json.")
    st.stop()
dams.sort(key=lambda d: -d["river_mile"])
dam_names = [d["name"] for d in dams]
# Default to Old Hickory Dam if present
if "Old Hickory Dam" in dam_names:
    default_index = dam_names.index("Old Hickory Dam")
else:
    default_index = 0

# Arrange form elements in a grid (3 columns on desktop, stack on mobile)
with st.container():
    # First row: dam, discharge, max mile marker
    row1_col1, row1_col2, row1_col3 = st.columns(3)
    with row1_col1:
        selected_dam_name = st.selectbox(
            "Starting dam",
            dam_names,
            index=default_index,
            key="dam_selectbox",
            help="Choose the dam to start calculations from."
        )
    with row1_col2:
        flow_cfs = st.number_input(
            "Discharge (CFS)",
            min_value=0,
            value=2500,
            step=1,
            format="%d",
            help="Average Hourly Discharge in Cubic Feet per Second."
        )
    with row1_col3:
        # Set selected dam and max mile allowed
        selected_dam_idx = dam_names.index(selected_dam_name)
        selected_dam = dams[selected_dam_idx]
        if selected_dam_idx < len(dams) - 1:
            next_dam = dams[selected_dam_idx + 1]
            max_mile_allowed = selected_dam["river_mile"] - next_dam["river_mile"]
        else:
            max_mile_allowed = selected_dam["river_mile"]  # allow up to river mouth
        max_mile_marker = st.number_input(
            "Max mile marker",
            min_value=1,
            value=min(30, int(max_mile_allowed)),
            max_value=int(max_mile_allowed),
            step=1,
            format="%d",
            help=f"Maximum mile marker downstream from the dam (max {int(max_mile_allowed)})."
        )
    # Second row: loss per mile, latitude, longitude
    row2_col1, row2_col2, row2_col3 = st.columns(3)
    with row2_col1:
        loss_percent = st.number_input(
            "Loss per mile (%)",
            min_value=0.0,
            max_value=100.0,
            value=0.5,
            step=0.1,
            format="%.2f",
            help="Estimated flow loss per mile as a percent."
        )
    with row2_col2:
        user_lat = st.number_input(
            "Your Latitude",
            value=selected_dam.get("lat", 36.2912),
            format="%.6f",
            help="Enter your latitude (decimal degrees)."
        )
    with row2_col3:
        user_lon = st.number_input(
            "Your Longitude",
            value=selected_dam.get("lon", -86.6515),
            format="%.6f",
            help="Enter your longitude (decimal degrees)."
        )

mile_markers = list(range(0, max_mile_marker + 1))

# Compact input style for all widgets
st.markdown("""
    <style>
    .stNumberInput, .stSelectbox, .stTextInput, .stSlider, .stButton, .stTextArea {
        max-width: 100%;
        font-size: 1rem;
        padding: 0.2rem 0.5rem;
        margin-bottom: 0.3rem;
    }
    /* Make columns tighter */
    section[data-testid="column"] {
        gap: 0.1rem !important;
        min-width: 0 !important;
    }
    /* Reduce padding on container */
    [data-testid="stAppViewContainer"] > .main {
        padding-top: 0.5rem;
    }
    </style>
""", unsafe_allow_html=True)

if flow_cfs == 0:
    st.warning("Please enter a valid discharge value to proceed.")

import numpy as np
import requests
import shapely.geometry
import geopandas as gpd
import folium
from shapely.geometry import LineString, Point
from streamlit_folium import st_folium

if flow_cfs and flow_cfs > 0:
    loss_rate = loss_percent / 100.0
    flow_cfm_initial = flow_cfs * 60
    # Fetch Cumberland River path from OSM Overpass API
    # st.info("Loading real Cumberland River path from OpenStreetMap...")  # Hidden
    overpass_url = "https://overpass-api.de/api/interpreter"
    # Dynamically set bounding box: center on selected dam, extend downstream ~0.3 deg lat, 0.2 deg lon
    dam_lat = selected_dam["lat"]
    dam_lon = selected_dam["lon"]
    bbox = [dam_lat - 0.3, dam_lon - 0.2, dam_lat + 0.1, dam_lon + 0.2]  # south, west, north, east
    query = f"""
    [out:json][timeout:25];
    (
      way["waterway"="river"]["name"="Cumberland River"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
    );
    (._;>;);
    out body;
    """
    try:
        resp = requests.post(overpass_url, data={'data': query})
        resp.raise_for_status()
        data_osm = resp.json()
        nodes = {n['id']: (n['lat'], n['lon']) for n in data_osm['elements'] if n['type'] == 'node'}
        river_lines = []
        for el in data_osm['elements']:
            if el['type'] == 'way':
                coords = [nodes[nid] for nid in el['nodes'] if nid in nodes]
                if len(coords) > 1:
                    river_lines.append(LineString([(lon, lat) for lat, lon in coords]))
        # Merge all river segments into one line
        if len(river_lines) == 0:
            raise Exception("No river lines found in OSM response.")
        river_line = river_lines[0]
        for l in river_lines[1:]:
            river_line = river_line.union(l)
        if river_line.geom_type == 'MultiLineString':
            river_line = max(river_line.geoms, key=lambda g: g.length)
        # Find the point on the river line nearest the selected dam
        dam_point = Point(dam_lon, dam_lat)
        start_dist = river_line.project(dam_point)
        # Interpolate mile markers starting from the selected dam
        n_markers = max(mile_markers)
        distances = [start_dist + (river_line.length - start_dist) * (i / n_markers) for i in range(n_markers + 1)]
        marker_points = [river_line.interpolate(d) for d in distances]
        marker_lats = [p.y for p in marker_points]
        marker_lons = [p.x for p in marker_points]
        marker_miles = list(range(n_markers + 1))
        map_df = pd.DataFrame({"lat": marker_lats, "lon": marker_lons, "Mile Marker": marker_miles})
    except Exception as e:
        st.error(f"Error loading river geometry from OSM: {e}")
        st.stop()

    # Find nearest point on river and river mile
    user_point = Point(user_lon, user_lat)
    dists = [user_point.distance(Point(lon, lat)) for lon, lat in zip(marker_lons, marker_lats)]
    min_idx = int(np.argmin(dists))
    nearest_marker = marker_miles[min_idx]
    nearest_lat = marker_lats[min_idx]
    nearest_lon = marker_lons[min_idx]


    # st.success(f"Nearest Mile Marker: {nearest_marker} (Lat: {nearest_lat:.5f}, Lon: {nearest_lon:.5f})")  # Hidden
    cfm_at_user = int(flow_cfm_initial * ((1 - loss_rate) ** nearest_marker))
    # st.info(f"Estimated Flow Rate at Your Location: {cfm_at_user:,} CFM")  # Hidden

    # Plot with folium for better OSM visualization
    m = folium.Map(location=[marker_lats[0], marker_lons[0]], zoom_start=11, tiles="OpenStreetMap")
    folium.PolyLine(list(zip(marker_lats, marker_lons)), color="blue", weight=3, tooltip="Cumberland River").add_to(m)
    import datetime
    river_velocity_mph = 2.5  # Assumed average river velocity
    now = datetime.datetime.strptime("2025-05-22 13:50:43", "%Y-%m-%d %H:%M:%S")
    # Use selected dam as starting point for calculations
    dam_lat = selected_dam["lat"]
    dam_lon = selected_dam["lon"]
    for idx, (lat, lon, mile) in enumerate(zip(marker_lats, marker_lons, marker_miles)):
        if mile in mile_markers and mile <= max_mile_allowed:
            travel_time_hr = mile / river_velocity_mph
            arrival_time = now + datetime.timedelta(hours=travel_time_hr)
            cfm_at_mile = int(flow_cfm_initial * ((1 - loss_rate) ** mile))
            popup_content = (
                f"<pre style='white-space: pre; font-family: monospace; min-width: 220px; width: 340px;'>"
                f"Mile {mile}<br>Lat: {lat:.5f}<br>Lon: {lon:.5f}<br>Arrival: {arrival_time.strftime('%Y-%m-%d %H:%M:%S')}<br>CFM: {cfm_at_mile:,}"
                f"</pre>"
            )
            folium.CircleMarker(
                location=[lat, lon],
                radius=6,
                color="green",
                fill=True,
                fill_color="green",
                fill_opacity=0.8,
                tooltip=folium.Tooltip(popup_content, sticky=True, direction='right', permanent=False, max_width=340),
                popup=folium.Popup(popup_content, max_width=340)
            ).add_to(m)
    # Calculate flow at user's location (nearest mile marker)
    cfm_at_user = int(flow_cfm_initial * ((1 - loss_rate) ** nearest_marker))
    dam_popup_content = (
        f"<pre style='white-space: pre; font-family: monospace; min-width: 220px; width: 340px;'>"
        f"{selected_dam['name']}<br>Lat: {dam_lat:.5f}<br>Lon: {dam_lon:.5f}<br>Time: {now.strftime('%Y-%m-%d %H:%M:%S')}"
        f"</pre>"
    )
    folium.CircleMarker(
        location=[dam_lat, dam_lon],
        radius=8,
        color="red",
        fill=True,
        fill_color="red",
        fill_opacity=0.9,
        tooltip=folium.Tooltip(dam_popup_content, sticky=True, direction='right', permanent=False, max_width=340),
        popup=folium.Popup(dam_popup_content, max_width=340)
    ).add_to(m)
    st.subheader("Map of Cumberland River, Mile Markers, and Dam Location")
    st_folium(m, width=700, height=700)
    st.caption("River path, markers, and dam from OpenStreetMap and Wikipedia. For high-precision work, use official TVA or GIS data.")
