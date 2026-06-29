#!/usr/bin/env python3
"""
Builds a Grafana precipitation dashboard from CHORDS portal data.
One panel per weather station showing raw precip vars, plus a computed
24h UTC daily total panel for stations that only record instantaneous tips.
"""

import subprocess
import sys
import requests
from requests.auth import HTTPBasicAuth

# ── Configuration ─────────────────────────────────────────────────────────────
GRAFANA_URL     = "https://<chords-portal-here>:3000"
GRAFANA_USER    = "admin"
GRAFANA_PASS    = "<grafana-password-here>"
MYSQL_DB        = "chords_demo_production"
INFLUXDB_URL    = "http://chords_influxdb:8086"
INFLUXDB_DB     = "chords_ts_production"
INFLUXDB_USER   = "guest"
INFLUXDB_PASS   = "guest"
DASHBOARD_TITLE = "Precipitation - All Stations"

# All shortnames to pull from MySQL
PRECIP_SHORTNAMES = [
    'rg',  'rg1',  'rg2',
    'rgt', 'rgt1', 'rgt2',
    'rgp', 'rgp1', 'rgp2',
    'rg1t', 'rain', 'r_rain1',
    'rgc', 'rgs',  'rgds',
]

# Vars that already represent accumulated totals — no computed panel needed
ACCUMULATED_SHORTNAMES = {'rgt', 'rgt1', 'rgt2', 'rgp', 'rgp1', 'rgp2', 'rg1t'}

# Tip/instantaneous vars that can be summed into a 24h total
# (excludes rgs/rgds which are seconds-between-tips, not amounts)
SUMMABLE_TIP_SHORTNAMES = {'rg', 'rg1', 'rg2', 'rgc', 'rain', 'r_rain1'}
# ──────────────────────────────────────────────────────────────────────────────


def get_precip_vars():
    shortnames_sql = ', '.join(f"'{s}'" for s in PRECIP_SHORTNAMES)
    query = (
        f"SELECT i.id, i.name, v.id, v.shortname "
        f"FROM vars v "
        f"JOIN instruments i ON v.instrument_id = i.id "
        f"WHERE v.shortname IN ({shortnames_sql}) "
        f"ORDER BY i.name, v.shortname;"
    )
    cmd = ['docker', 'exec', 'chords_mysql', 'mysql', '-u', 'root', MYSQL_DB, '-e', query]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"MySQL error: {result.stderr.decode('utf-8', errors='replace')}")

    instruments = {}
    for line in result.stdout.decode('utf-8', errors='replace').strip().split('\n')[1:]:
        parts = line.split('\t')
        if len(parts) != 4:
            continue
        inst_id, inst_name, var_id, shortname = parts
        inst_id = int(inst_id)
        if inst_id not in instruments:
            instruments[inst_id] = {'name': inst_name, 'vars': []}
        instruments[inst_id]['vars'].append((int(var_id), shortname))

    return instruments


def needs_computed_total(inst_vars):
    """True if instrument has summable tip vars but no accumulated total vars."""
    shortnames = {s for _, s in inst_vars}
    return (bool(shortnames & SUMMABLE_TIP_SHORTNAMES) and
            not bool(shortnames & ACCUMULATED_SHORTNAMES))


def get_summable_tip_vars(inst_vars):
    return [(var_id, s) for var_id, s in inst_vars if s in SUMMABLE_TIP_SHORTNAMES]


def ensure_datasource(auth):
    r = requests.get(f"{GRAFANA_URL}/api/datasources", auth=auth)
    r.raise_for_status()
    for ds in r.json():
        if ds.get('name') == 'CHORDS':
            print(f"  → Found existing datasource UID: {ds['uid']}")
            return ds['uid']

    print("  → Datasource not found, creating...")
    payload = {
        "name":           "CHORDS",
        "type":           "influxdb",
        "url":            INFLUXDB_URL,
        "database":       INFLUXDB_DB,
        "user":           INFLUXDB_USER,
        "secureJsonData": {"password": INFLUXDB_PASS},
        "access":         "proxy",
        "isDefault":      True,
    }
    r = requests.post(f"{GRAFANA_URL}/api/datasources", json=payload, auth=auth)
    r.raise_for_status()
    uid = r.json()['datasource']['uid']
    print(f"  → Created datasource UID: {uid}")
    return uid


def build_panel(inst_id, inst_name, var_list, ds_uid, panel_id, x, y):
    targets = []
    for i, (var_id, shortname) in enumerate(sorted(var_list, key=lambda v: v[1])):
        targets.append({
            "datasource": {"type": "influxdb", "uid": ds_uid},
            "query": (
                f'SELECT last("value") FROM "tsdata" '
                f'WHERE "inst" = \'{inst_id}\' '
                f'AND "var" = \'{var_id}\' '
                f'AND $timeFilter '
                f'GROUP BY time($__interval) fill(null)'
            ),
            "alias":    shortname,
            "refId":    chr(65 + i),
            "rawQuery": True,
        })

    return {
        "id":         panel_id,
        "type":       "timeseries",
        "title":      inst_name,
        "datasource": {"type": "influxdb", "uid": ds_uid},
        "targets":    targets,
        "gridPos":    {"x": x, "y": y, "w": 12, "h": 8},
        "fieldConfig": {
            "defaults": {
                "unit": "suffix:mm",
                "custom": {
                    "lineInterpolation": "linear",
                    "fillOpacity":       10,
                    "spanNulls":         False,
                },
            }
        },
        "options": {
            "tooltip": {"mode": "multi", "sort": "none"},
            "legend":  {"displayMode": "list", "placement": "bottom"},
        },
    }


def build_computed_panel(inst_id, inst_name, tip_vars, ds_uid, panel_id, x, y):
    targets = []
    for i, (var_id, shortname) in enumerate(sorted(tip_vars, key=lambda v: v[1])):
        targets.append({
            "datasource": {"type": "influxdb", "uid": ds_uid},
            "query": (
                f'SELECT sum("value") FROM "tsdata" '
                f'WHERE "inst" = \'{inst_id}\' '
                f'AND "var" = \'{var_id}\' '
                f'AND $timeFilter '
                f'GROUP BY time(24h, 0s) fill(0)'
            ),
            "alias": f"{shortname} daily",
            "refId": chr(65 + i),
            "rawQuery": True,
        })
    return {
        "id": panel_id, "type": "barchart",
        "title": f"{inst_name} — Daily UTC Total",
        "datasource": {"type": "influxdb", "uid": ds_uid},
        "targets": targets,
        "gridPos": {"x": x, "y": y, "w": 12, "h": 8},
        "fieldConfig": {
            "defaults": {
                "unit": "suffix:mm",
                "custom": {
                    "fillOpacity": 80,
                    "gradientMode": "none",
                    "lineWidth": 1,
                },
            }
        },
        "options": {
            "barWidth": 0.7,
            "groupWidth": 0.7,
            "tooltip": {"mode": "multi", "sort": "none"},
            "legend": {"displayMode": "list", "placement": "bottom"},
        },
    }


def build_dashboard(panels):
    return {
        "dashboard": {
            "title":         DASHBOARD_TITLE,
            "tags":          ["chords", "precipitation"],
            "timezone":      "browser",
            "refresh":       "30s",
            "time":          {"from": "now-7d", "to": "now"},
            "panels":        panels,
            "schemaVersion": 39,
        },
        "overwrite": True,
        "folderId":  0,
    }


def push_dashboard(auth, payload):
    r = requests.post(f"{GRAFANA_URL}/api/dashboards/db", json=payload, auth=auth)
    r.raise_for_status()
    return r.json()


def main():
    auth = HTTPBasicAuth(GRAFANA_USER, GRAFANA_PASS)

    print("Querying MySQL for precipitation variables...")
    instruments = get_precip_vars()
    print(f"  → {len(instruments)} instruments with precip variables")

    print("Checking Grafana datasource...")
    ds_uid = ensure_datasource(auth)

    print("Building raw panels...")
    panels = []
    sorted_instruments = sorted(instruments.items(), key=lambda x: x[1]['name'])
    for panel_id, (inst_id, data) in enumerate(sorted_instruments, start=1):
        col = (panel_id - 1) % 2
        x   = col * 12
        y   = ((panel_id - 1) // 2) * 8
        panels.append(build_panel(inst_id, data['name'], data['vars'], ds_uid, panel_id, x, y))
        var_names = ', '.join(s for _, s in data['vars'])
        print(f"  + {data['name']}  ({var_names})")

    print("Building computed 24h total panels...")
    next_y  = ((len(panels) + 1) // 2) * 8
    computed = []
    for inst_id, data in sorted_instruments:
        if not needs_computed_total(data['vars']):
            continue
        tip_vars = get_summable_tip_vars(data['vars'])
        idx      = len(computed)
        x        = (idx % 2) * 12
        y        = next_y + (idx // 2) * 8
        panel_id = len(panels) + idx + 1
        computed.append(build_computed_panel(inst_id, data['name'], tip_vars, ds_uid, panel_id, x, y))
        print(f"  ~ {data['name']}  (24h sum)")

    panels.extend(computed)
    print(f"\n  → {len(panels)} total panels ({len(panels) - len(computed)} raw, {len(computed)} computed)")

    print("Pushing dashboard to Grafana...")
    result = push_dashboard(auth, build_dashboard(panels))
    print(f"\nDone!  Open: {GRAFANA_URL}{result.get('url', '')}")


if __name__ == "__main__":
    main()
