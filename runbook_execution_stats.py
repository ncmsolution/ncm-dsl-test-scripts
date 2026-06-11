#!/usr/bin/env python3
"""
plot_runbook_from_api.py
========================
Fetches runbook execution data directly from the NCM API using a batch/execution
ID (the trailing number in execution names like par_cs_endpoints1_power_off_1781026202),
then produces the same three graphs + interactive HTML report as plot_runbook_timeline.py.

Usage:
    python3 runbook_execution_stats.py \\
        --host  nconprem-10-122-152-117.ccpnx.com \\
        --user  admin \\
        --pass  my_password \\
        --batch-id 1781026202

    # Custom output prefix:
    python3 runbook_execution_stats.py \\
        --host  nconprem-10-122-152-117.ccpnx.com \\
        --user  admin \\
        --pass  my_password \\
        --batch-id 1781026202 \\
        --output ~/reports/power_off_api_run

    # Use calm DSL client (no need to pass credentials):
    python3 runbook_execution_stats.py \\
        --calm-dsl-dir ~/calm-dsl \\
        --batch-id 1781026202

Output files (same as plot_runbook_timeline.py):
    <prefix>_start_times.png     scatter plot – endpoint vs. start time
    <prefix>_duration.png        bar chart    – duration per endpoint
    <prefix>_report.html         interactive HTML (Plotly if installed)

Requirements:
    pip install requests matplotlib plotly
    (plotly optional – falls back to PNG in the HTML report)
"""

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ── optional plotly ────────────────────────────────────────────────────────────
try:
    import plotly.graph_objects as go
    PLOTLY_AVAILABLE = True
except ImportError:
    PLOTLY_AVAILABLE = False
    print("[INFO] plotly not installed – HTML report will embed PNG images.")
    print("       Install with: pip install plotly")

# ── required matplotlib ────────────────────────────────────────────────────────
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import matplotlib.patches as mpatches
except ImportError:
    print("[ERROR] matplotlib is required.  pip install matplotlib")
    sys.exit(1)

# ── optional requests ─────────────────────────────────────────────────────────
try:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


# ─── NCM API client (raw requests) ───────────────────────────────────────────

class NcmClient:
    """Thin wrapper around the NCM v3 REST API."""

    def __init__(self, host: str, username: str, password: str):
        if not REQUESTS_AVAILABLE:
            raise RuntimeError("pip install requests is required for --host mode")
        base = host if host.startswith("http") else f"https://{host}"
        self.base = base.rstrip("/")
        self.session = requests.Session()
        self.session.auth    = (username, password)
        self.session.verify  = False
        self.session.headers.update({"Content-Type": "application/json"})

    def post(self, path: str, payload: dict, timeout: int = 30) -> dict:
        url = f"{self.base}{path}"
        r = self.session.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def get(self, path: str, timeout: int = 30) -> dict:
        url = f"{self.base}{path}"
        r = self.session.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()


# ─── calm DSL client (subprocess helper) ─────────────────────────────────────

_SDK_HELPER = '''
import sys, json
with open(sys.argv[1]) as f:
    req = json.load(f)
try:
    from calm.dsl.api import get_api_client
    from calm.dsl.api.connection import REQUEST
except ImportError as e:
    print("RESULT_JSON:" + json.dumps({"ok": False, "error": str(e)}))
    sys.exit(2)
try:
    import urllib3; urllib3.disable_warnings()
except Exception:
    pass
client = get_api_client()
conn   = client.runbook.connection
method_map = {"GET": REQUEST.METHOD.GET, "POST": REQUEST.METHOD.POST}

def call(method, path, payload=None):
    kw = dict(method=method_map[method], verify=False, ignore_error=True)
    if payload is not None:
        kw["request_json"] = payload
    res, err = conn._call(path.lstrip("/"), **kw)
    if res is not None:
        try:
            return res.status_code, res.json()
        except Exception:
            return res.status_code, {}
    return 0, (err or {})

s, d = call(req["method"], req["path"], req.get("payload"))
print("RESULT_JSON:" + json.dumps({"ok": s in (200, 201, 202), "status": s, "data": d}))
'''

_helper_path: Optional[str] = None


def _ensure_helper() -> str:
    global _helper_path
    if _helper_path and os.path.exists(_helper_path):
        return _helper_path
    fd, p = tempfile.mkstemp(suffix=".py", prefix="ncm_api_helper_")
    with os.fdopen(fd, "w") as fh:
        fh.write(_SDK_HELPER)
    _helper_path = p
    return p


def _venv_python(calm_dsl_dir: str) -> str:
    for py in (
        os.path.join(calm_dsl_dir, "venv", "bin", "python3"),
        os.path.join(calm_dsl_dir, "venv", "bin", "python"),
    ):
        if os.path.exists(py):
            return py
    return sys.executable


def calm_dsl_call(calm_dsl_dir: str, method: str, path: str,
                  payload: Optional[dict] = None, timeout: int = 60) -> dict:
    req: dict = {"method": method, "path": path}
    if payload is not None:
        req["payload"] = payload
    fd, req_file = tempfile.mkstemp(suffix=".json", prefix="ncm_req_")
    with os.fdopen(fd, "w") as fh:
        json.dump(req, fh)
    try:
        r = subprocess.run(
            [_venv_python(calm_dsl_dir), _ensure_helper(), req_file],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout,
        )
        for line in (r.stdout or "").splitlines():
            if line.startswith("RESULT_JSON:"):
                result = json.loads(line[len("RESULT_JSON:"):])
                if result.get("ok"):
                    return result.get("data", {})
                raise RuntimeError(f"API error {result.get('status')}: {result.get('error','')}")
        raise RuntimeError(f"No RESULT_JSON in helper output.\n{r.stderr}")
    finally:
        try:
            os.unlink(req_file)
        except OSError:
            pass


# ─── fetch runlogs from NCM ───────────────────────────────────────────────────

def _epoch_us_to_dt(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(value) / 1_000_000)
    except (TypeError, ValueError):
        return None


def fetch_runlogs_by_batch(
    batch_id: str,
    *,
    client: Optional[NcmClient] = None,
    calm_dsl_dir: Optional[str] = None,
    page_size: int = 250,
) -> List[dict]:
    """
    Query POST /api/nutanix/v3/runbooks/runlogs/list, filtering to execution
    names that contain `batch_id`.  Returns a list of parsed row dicts.
    """

    def do_post(path: str, payload: dict) -> dict:
        if client:
            return client.post(path, payload)
        return calm_dsl_call(calm_dsl_dir, "POST", path, payload)  # type: ignore

    filter_str = f"execution_name=~.*{re.escape(batch_id)}.*"
    path = "/api/nutanix/v3/runbooks/runlogs/list"

    print(f"[INFO] Querying NCM: {path}  filter={filter_str!r}")

    all_entities: List[dict] = []
    offset = 0

    while True:
        payload = {
            "filter":  filter_str,
            "length":  page_size,
            "offset":  offset,
        }
        try:
            data = do_post(path, payload)
        except Exception as e:
            # Some NCM versions don't support regex filter on execution_name.
            # Fall back: fetch without filter and do client-side filtering.
            print(f"[WARN] Filtered query failed ({e}). "
                  f"Fetching without filter and filtering client-side …")
            return _fetch_all_and_filter(batch_id,
                                         do_post=do_post,
                                         page_size=page_size)

        entities = data.get("entities", [])
        all_entities.extend(entities)
        total = data.get("metadata", {}).get("total_matches", len(entities))
        print(f"[INFO]   page offset={offset}: got {len(entities)} / {total} total")
        offset += len(entities)
        if offset >= total or not entities:
            break

    print(f"[INFO] {len(all_entities)} runlog entit{'y' if len(all_entities)==1 else 'ies'} fetched")
    return _parse_entities(all_entities, batch_id)


def _fetch_all_and_filter(
    batch_id: str,
    *,
    do_post,
    page_size: int = 250,
) -> List[dict]:
    path = "/api/nutanix/v3/runbooks/runlogs/list"
    all_entities: List[dict] = []
    offset = 0
    while True:
        payload = {"length": page_size, "offset": offset}
        data = do_post(path, payload)
        entities = data.get("entities", [])
        all_entities.extend(entities)
        total = data.get("metadata", {}).get("total_matches", len(entities))
        offset += len(entities)
        if offset >= total or not entities:
            break
    # client-side filter
    filtered = [
        e for e in all_entities
        if batch_id in _exec_name(e)
    ]
    print(f"[INFO] {len(filtered)} matching (out of {len(all_entities)} total) after client filter")
    return _parse_entities(filtered, batch_id)


def _exec_name(entity: dict) -> str:
    """Extract execution_name from a runlog entity (checks spec and status)."""
    for key in ("spec", "status"):
        block = entity.get(key, {})
        if isinstance(block, dict):
            name = block.get("execution_name") or block.get("name") or ""
            if name:
                return name
    # fall back to metadata name
    return entity.get("metadata", {}).get("name", "")


def _extract_endpoint(exec_name: str) -> str:
    """'par_cs_endpoints1_power_off_1781026202' → 'cs_endpoints1'."""
    m = re.match(r"^par_(.+?)_(?:power_off|power_on|restart)_\d+$", exec_name)
    if m:
        return m.group(1)
    return re.sub(r"^par_", "", exec_name) or exec_name


def _state_to_status(state: str) -> str:
    return {
        "SUCCESS": "Success", "FAILURE": "Failure", "ERROR": "Error",
        "ABORTED": "Aborted", "CANCELLED": "Cancelled",
        "RUNNING": "Running", "PENDING": "Running",
    }.get(state.upper(), state.title() or "Unknown")


def _parse_entities(entities: List[dict], batch_id: str) -> List[dict]:
    rows: List[dict] = []
    for e in entities:
        exec_name = _exec_name(e)
        if not exec_name:
            continue
        md  = e.get("metadata", {}) or {}
        st  = e.get("status",   {}) or {}
        state_raw = str(st.get("state") or "").strip().upper()

        start_time = _epoch_us_to_dt(md.get("creation_time"))
        # Use last_update_time as end time only when the run is terminal
        terminal = state_raw in ("SUCCESS", "FAILURE", "ERROR", "ABORTED", "CANCELLED")
        end_time  = _epoch_us_to_dt(md.get("last_update_time")) if terminal else None

        owner  = (md.get("owner_reference") or {})
        run_by = str(owner.get("name") or "").strip()
        uuid   = md.get("uuid", "")

        rows.append({
            "endpoint":   _extract_endpoint(exec_name),
            "exec_name":  exec_name,
            "start_time": start_time,
            "end_time":   end_time,
            "status":     _state_to_status(state_raw),
            "run_by":     run_by,
            "uuid":       uuid,
            # URL will be filled in after we know the host
            "url":        "",
        })
    return rows


def fill_urls(rows: List[dict], host: str) -> None:
    base = host if host.startswith("http") else f"https://{host}"
    base = base.rstrip("/")
    for r in rows:
        if r["uuid"]:
            r["url"] = (f"{base}/services/self_service/runbooks/runlogs/{r['uuid']}")


# ─── graph helpers (shared with plot_runbook_timeline.py) ─────────────────────

STATUS_COLORS = {"Success": "steelblue", "Running": "orange",
                 "Failure": "crimson",   "Error": "crimson"}

def _dur(row) -> Optional[float]:
    if row["start_time"] and row["end_time"]:
        return (row["end_time"] - row["start_time"]).total_seconds()
    return None

def _color(status: str) -> str:
    return STATUS_COLORS.get(status, "gray")

def _fmt_xaxis(ax, start_times):
    span = (max(start_times) - min(start_times)).total_seconds()
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=12))
    ax.xaxis.set_major_formatter(
        mdates.DateFormatter("%H:%M:%S" if span < 3600 else "%m/%d %H:%M")
    )
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right")


def plot_scatter_png(rows, prefix):
    valid = sorted([r for r in rows if r["start_time"]], key=lambda r: r["start_time"])
    if not valid:
        print("[WARN] No start times – scatter skipped"); return None
    endpoints   = [r["endpoint"]   for r in valid]
    start_times = [r["start_time"] for r in valid]
    colors      = [_color(r["status"]) for r in valid]
    fig, ax = plt.subplots(figsize=(16, max(8, len(valid) * 0.38)))
    ax.scatter(start_times, range(len(valid)), c=colors, alpha=0.85, s=130,
               edgecolors="navy", linewidths=1.3)
    ax.set_yticks(range(len(valid))); ax.set_yticklabels(endpoints, fontsize=8)
    ax.set_xlabel("Start Time", fontsize=12, fontweight="bold")
    ax.set_ylabel("Endpoint",   fontsize=12, fontweight="bold")
    ax.set_title("Runbook Execution Start Times\n(Points clustered together = started at the same time)",
                 fontsize=13, fontweight="bold")
    ax.grid(True, axis="x", alpha=0.3, linestyle="--")
    _fmt_xaxis(ax, start_times)
    ax.legend(handles=[mpatches.Patch(color=c, label=s) for s, c in STATUS_COLORS.items()],
              loc="lower right", fontsize=9)
    plt.tight_layout()
    out = f"{prefix}_start_times.png"
    plt.savefig(out, dpi=150, bbox_inches="tight"); plt.close()
    print(f"[INFO] Saved: {out}"); return out


def plot_duration_png(rows, prefix):
    valid = sorted([(r["endpoint"], _dur(r)) for r in rows if _dur(r)], key=lambda x: x[1])
    if not valid:
        print("[WARN] No duration data – bar chart skipped"); return None
    names, durations = zip(*valid)
    avg = sum(durations) / len(durations)
    colors = ["coral" if d > avg else "steelblue" for d in durations]
    fig, ax = plt.subplots(figsize=(14, max(8, len(valid) * 0.38)))
    bars = ax.barh(range(len(valid)), durations, color=colors, alpha=0.85,
                   edgecolor="white", linewidth=0.5)
    ax.set_yticks(range(len(valid))); ax.set_yticklabels(names, fontsize=8)
    ax.set_xlabel("Duration (seconds)", fontsize=12, fontweight="bold")
    ax.set_ylabel("Endpoint",           fontsize=12, fontweight="bold")
    ax.set_title(f"Runbook Execution Duration per Endpoint\n"
                 f"avg: {avg:.0f}s  |  min: {min(durations):.0f}s  |  max: {max(durations):.0f}s  "
                 f"|  sorted shortest → longest", fontsize=13, fontweight="bold")
    ax.axvline(avg, color="red", linestyle="--", linewidth=1.3)
    ax.grid(True, axis="x", alpha=0.3, linestyle="--")
    for bar, d in zip(bars, durations):
        ax.text(d + 0.5, bar.get_y() + bar.get_height() / 2,
                f"{int(d)}s", va="center", fontsize=7)
    ax.legend(handles=[
        mpatches.Patch(color="coral",     label="Above average"),
        mpatches.Patch(color="steelblue", label="Below average"),
        plt.Line2D([0],[0], color="red", linestyle="--", label=f"Avg ({avg:.0f}s)"),
    ], loc="lower right", fontsize=9)
    plt.tight_layout()
    out = f"{prefix}_duration.png"
    plt.savefig(out, dpi=150, bbox_inches="tight"); plt.close()
    print(f"[INFO] Saved: {out}"); return out


# ─── plotly interactive ───────────────────────────────────────────────────────

def plotly_scatter_html(rows) -> Optional[str]:
    valid = sorted([r for r in rows if r["start_time"]], key=lambda r: r["start_time"])
    if not valid: return None
    endpoints   = [r["endpoint"]   for r in valid]
    start_times = [r["start_time"] for r in valid]
    hover = [
        f"<b>{r['endpoint']}</b><br>"
        f"Start:  {r['start_time'].strftime('%H:%M:%S')}<br>"
        f"Status: {r['status']}"
        for r in valid
    ]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=start_times, y=list(range(len(valid))), mode="markers",
        marker=dict(size=12, color=[_color(r["status"]) for r in valid],
                    line=dict(width=1.5, color="navy")),
        hovertext=hover, hoverinfo="text", showlegend=False,
    ))
    fig.update_layout(
        title="Runbook Execution Start Times<br><sub>Hover for details</sub>",
        xaxis_title="Start Time",
        yaxis=dict(tickmode="array", tickvals=list(range(len(valid))), ticktext=endpoints),
        yaxis_title="Endpoint",
        height=max(600, len(valid) * 22),
        xaxis=dict(tickangle=-45, tickformat="%H:%M:%S"),
        hovermode="closest", plot_bgcolor="white",
        xaxis_showgrid=True, xaxis_gridcolor="#e0e0e0", yaxis_showgrid=False,
    )
    return fig.to_html(include_plotlyjs="cdn", div_id="scatter_chart", full_html=False)


def plotly_duration_html(rows) -> Optional[str]:
    valid = sorted([(r["endpoint"], _dur(r)) for r in rows if _dur(r)], key=lambda x: x[1])
    if not valid: return None
    names, durations = zip(*valid)
    avg    = sum(durations) / len(durations)
    colors = ["coral" if d > avg else "steelblue" for d in durations]
    hover  = [f"<b>{n}</b><br>Duration: {int(d)}s<br>"
              f"{'Above' if d > avg else 'Below'} avg ({avg:.0f}s)"
              for n, d in zip(names, durations)]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=list(durations), y=list(names), orientation="h",
        marker_color=colors, marker_opacity=0.85,
        hovertext=hover, hoverinfo="text", showlegend=False,
    ))
    fig.add_vline(x=avg, line_dash="dash", line_color="red",
                  annotation_text=f"Avg {avg:.0f}s", annotation_position="top right")
    fig.update_layout(
        title=f"Duration per Endpoint<br><sub>Sorted shortest → longest · avg {avg:.0f}s</sub>",
        xaxis_title="Duration (seconds)", yaxis_title="Endpoint",
        height=max(600, len(valid) * 22), plot_bgcolor="white",
        xaxis_showgrid=True, xaxis_gridcolor="#e0e0e0", yaxis_showgrid=False,
    )
    return fig.to_html(include_plotlyjs=False, div_id="duration_chart", full_html=False)


# ─── HTML report ──────────────────────────────────────────────────────────────

def _png_b64(path) -> Optional[str]:
    if not path or not os.path.exists(path): return None
    with open(path, "rb") as f: return base64.b64encode(f.read()).decode()

def _chart_block(title, interactive, b64):
    if interactive:
        return f"<h2>{title}</h2><div style='margin:12px 0'>{interactive}</div>"
    if b64:
        return (f"<h2>{title}</h2>"
                f"<img src='data:image/png;base64,{b64}' style='max-width:100%;border:1px solid #ddd'>")
    return ""

def build_html(rows, batch_id, source_desc,
               scatter_html, duration_html,
               scatter_png, duration_png) -> str:
    total   = len(rows)
    success = sum(1 for r in rows if r["status"] == "Success")
    running = sum(1 for r in rows if r["status"] in ("Running","Pending"))
    error   = total - success - running
    durs    = [_dur(r) for r in rows if _dur(r)]
    avg_d   = sum(durs)/len(durs) if durs else 0
    min_d   = min(durs) if durs else 0
    max_d   = max(durs) if durs else 0

    table_rows = ""
    for i, r in enumerate(rows, 1):
        st  = r["start_time"].strftime("%H:%M:%S") if r["start_time"] else "-"
        et  = r["end_time"].strftime("%H:%M:%S")   if r["end_time"]   else "-"
        dur = f"{int(_dur(r))}s"                   if _dur(r) else "-"
        css = {"Success":"success","Running":"running","Pending":"running"}.get(r["status"],"error")
        url_cell = f"<a href='{r['url']}' target='_blank'>view</a>" if r["url"] else "-"
        table_rows += (
            f"<tr><td>{i}</td><td>{r['endpoint']}</td>"
            f"<td>{st}</td><td>{et}</td><td>{dur}</td>"
            f"<td class='{css}'>{r['status']}</td><td>{url_cell}</td></tr>\n"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Runbook Timeline – batch {batch_id}</title>
  <style>
    body  {{ font-family: Arial, sans-serif; margin: 28px; background: #f5f5f5; color: #222; }}
    h1    {{ font-size: 22px; margin-bottom: 4px; }}
    h2    {{ font-size: 17px; margin-top: 40px; color: #333;
             border-bottom: 2px solid #ddd; padding-bottom: 4px; }}
    .meta {{ font-size: 12px; color: #888; margin-bottom: 20px; }}
    .stats {{ display: flex; gap: 16px; flex-wrap: wrap; margin: 16px 0 28px; }}
    .stat {{ background: #fff; border: 1px solid #ddd; border-radius: 6px;
             padding: 14px 20px; min-width: 110px; }}
    .stat .v {{ font-size: 28px; font-weight: bold; color: #1a73e8; }}
    .stat .l {{ font-size: 11px; color: #777; margin-top: 2px; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 13px; background: #fff; }}
    th {{ background: #444; color: #fff; padding: 8px 10px; text-align: left; }}
    td {{ padding: 6px 10px; border-bottom: 1px solid #eee; }}
    tr:hover td {{ background: #f0f4ff; }}
    .success {{ color: green; font-weight: bold; }}
    .running {{ color: darkorange; font-weight: bold; }}
    .error   {{ color: red; font-weight: bold; }}
    a {{ color: #1a73e8; }}
  </style>
</head>
<body>
  <h1>Runbook Execution Timeline Report</h1>
  <p class="meta">Batch ID: <code>{batch_id}</code> &nbsp;|&nbsp; Source: {source_desc}</p>

  <div class="stats">
    <div class="stat"><div class="v">{total}</div><div class="l">Total</div></div>
    <div class="stat"><div class="v" style="color:green">{success}</div><div class="l">Success</div></div>
    <div class="stat"><div class="v" style="color:darkorange">{running}</div><div class="l">Running</div></div>
    <div class="stat"><div class="v" style="color:red">{error}</div><div class="l">Error / Other</div></div>
    <div class="stat"><div class="v">{avg_d:.0f}s</div><div class="l">Avg Duration</div></div>
    <div class="stat"><div class="v">{min_d:.0f}s</div><div class="l">Min Duration</div></div>
    <div class="stat"><div class="v">{max_d:.0f}s</div><div class="l">Max Duration</div></div>
  </div>

  {_chart_block("Execution Start Times", scatter_html,  _png_b64(scatter_png))}
  {_chart_block("Duration per Endpoint", duration_html, _png_b64(duration_png))}

  <h2>Detailed Table</h2>
  <table>
    <tr><th>#</th><th>Endpoint</th><th>Started</th><th>Completed</th>
        <th>Duration</th><th>Status</th><th>Runlog</th></tr>
    {table_rows}
  </table>
</body>
</html>"""


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fetch runbook execution data from NCM API and plot timeline graphs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Authentication options (use one):
  A) Direct credentials (--host + --user + --pass)
  B) calm DSL client   (--calm-dsl-dir)   — no credentials needed

Examples:
  # Direct credentials:
  python3 plot_runbook_from_api.py \\
      --host nconprem-10-122-152-117.ccpnx.com \\
      --user admin --pass nutanix/4u \\
      --batch-id 1781026202

  # calm DSL client (reads creds from ~/.calm/config.ini):
  python3 plot_runbook_from_api.py \\
      --calm-dsl-dir ~/calm-dsl \\
      --batch-id 1781026202

  # With custom output prefix:
  python3 plot_runbook_from_api.py \\
      --host nconprem-10-122-152-117.ccpnx.com \\
      --user admin --pass nutanix/4u \\
      --batch-id 1781026202 \\
      --output ~/reports/batch_1781026202
""")
    # auth: option A
    parser.add_argument("--host", metavar="HOST",
                        help="NCM hostname or URL  e.g. nconprem-10-122-152-117.ccpnx.com")
    parser.add_argument("--user", "--username", dest="user", default="admin",
                        help="NCM username (default: admin)")
    parser.add_argument("--pass", "--password", dest="password", default=None,
                        help="NCM password")
    # auth: option B
    parser.add_argument("--calm-dsl-dir", metavar="DIR",
                        help="Path to calm-dsl venv directory (uses calm DSL client, "
                             "no explicit credentials needed)")
    # common
    parser.add_argument("--batch-id", required=True, metavar="ID",
                        help="Batch/execution ID to filter by  e.g. 1781026202")
    parser.add_argument("--output", default=None, metavar="PREFIX",
                        help="Output file prefix (default: ./runbook_batch_<ID>)")
    parser.add_argument("--page-size", type=int, default=250,
                        help="API page size (default: 250)")
    args = parser.parse_args()

    # validate auth
    use_dsl = bool(args.calm_dsl_dir)
    use_raw = bool(args.host)
    if not use_dsl and not use_raw:
        parser.error("Provide either --host (+ --user / --pass)  or  --calm-dsl-dir")
    if use_raw and not args.password:
        import getpass
        args.password = getpass.getpass(f"Password for {args.user}@{args.host}: ")

    # output prefix — default: ./runbook_batch_<id>/<id>  (all files in a batch folder)
    if args.output:
        prefix = args.output
    else:
        batch_dir = os.path.join(".", f"runbook_batch_{args.batch_id}")
        prefix    = os.path.join(batch_dir, args.batch_id)
    os.makedirs(os.path.dirname(os.path.abspath(prefix)) or ".", exist_ok=True)

    # build client
    client = NcmClient(args.host, args.user, args.password) if use_raw else None

    # fetch runlogs
    rows = fetch_runlogs_by_batch(
        args.batch_id,
        client=client,
        calm_dsl_dir=args.calm_dsl_dir if use_dsl else None,
        page_size=args.page_size,
    )

    if not rows:
        print(f"[ERROR] No executions found for batch-id '{args.batch_id}'")
        sys.exit(1)

    # fill URLs
    host_for_url = args.host if use_raw else ""
    if host_for_url:
        fill_urls(rows, host_for_url)

    total   = len(rows)
    success = sum(1 for r in rows if r["status"] == "Success")
    running = sum(1 for r in rows if r["status"] in ("Running","Pending"))
    print(f"[INFO] {total} executions  ({success} success, {running} running/pending, "
          f"{total-success-running} other)")

    # static PNGs
    scatter_png  = plot_scatter_png(rows, prefix)
    duration_png = plot_duration_png(rows, prefix)

    # plotly interactive
    if PLOTLY_AVAILABLE:
        scatter_html  = plotly_scatter_html(rows)
        duration_html = plotly_duration_html(rows)
    else:
        scatter_html = duration_html = None

    source = (f"NCM API  {args.host}" if use_raw
              else f"calm DSL  {args.calm_dsl_dir}")
    html = build_html(
        rows, args.batch_id, source,
        scatter_html, duration_html,
        scatter_png,  duration_png,
    )
    html_path = f"{prefix}_report.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[INFO] HTML report : {html_path}")
    print("[INFO] Done.")


if __name__ == "__main__":
    main()
