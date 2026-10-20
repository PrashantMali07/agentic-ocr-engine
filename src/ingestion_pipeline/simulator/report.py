"""
Interactive HTML Benchmark Dashboard Generator
----------------------------------------------
Generates a standalone, dependency-free interactive HTML report
with KPIs, charts, filterable tables, and drill-down drawers.
"""

import json
from html import escape
from pathlib import Path
from typing import Any, Dict, List

from ..core.safe_html import sanitize_tables


def generate_html_dashboard(
    run_info: Dict[str, Any],
    documents: List[Dict[str, Any]],
    lost_tokens: List[Dict[str, Any]],
    output_path: Path
) -> Path:
    """Generate self-contained HTML report with CSS/JS embedded."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total_docs = len(documents)
    successful = sum(1 for d in documents if d.get("status") == "success")
    total_docs - successful
    success_rate = (successful / total_docs * 100.0) if total_docs else 0.0

    avg_conf = run_info.get("avg_confidence", 0.0)
    avg_latency = run_info.get("avg_duration_sec", 0.0)
    total_tables = sum(d.get("table_count", 0) for d in documents)
    total_recovered = len(lost_tokens)

    # Bin confidences for histogram
    conf_bins = {"95-100%": 0, "90-95%": 0, "80-90%": 0, "<80%": 0}
    for d in documents:
        c = d.get("overall_confidence", 0.0) * 100.0
        if c >= 95.0:
            conf_bins["95-100%"] += 1
        elif c >= 90.0:
            conf_bins["90-95%"] += 1
        elif c >= 80.0:
            conf_bins["80-90%"] += 1
        else:
            conf_bins["<80%"] += 1

    safe_documents = [dict(d, html_content=sanitize_tables(d.get("html_content", ""))) for d in documents]

    def script_json(value):
        return (json.dumps(value, default=str)
                .replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
                .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))

    docs_json = script_json(safe_documents)
    lost_tokens_json = script_json(lost_tokens)
    safe_run_id = escape(str(run_info.get("run_id", "")))
    safe_engine = escape(str(run_info.get("engine", "")))
    safe_created_at = escape(str(run_info.get("created_at", "")))

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Document Ingestion Benchmark Dashboard</title>
<style>
  :root {{
    --bg: #0f172a; --card-bg: #1e293b; --text: #f8fafc; --text-muted: #94a3b8;
    --border: #334155; --accent: #38bdf8; --success: #22c55e; --warning: #f59e0b; --danger: #ef4444;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  body {{ background: var(--bg); color: var(--text); padding: 24px; }}
  .container {{ max-width: 1400px; margin: 0 auto; }}
  header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }}
  h1 {{ font-size: 24px; font-weight: 700; color: var(--accent); }}
  .meta-tag {{ background: var(--card-bg); padding: 6px 14px; border-radius: 6px; font-size: 13px; border: 1px solid var(--border); }}

  /* KPI Grid */
  .kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 24px; }}
  .kpi-card {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 18px; }}
  .kpi-title {{ font-size: 13px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }}
  .kpi-value {{ font-size: 28px; font-weight: 700; color: var(--text); }}
  .kpi-sub {{ font-size: 12px; color: var(--text-muted); margin-top: 4px; }}

  /* Distribution Bars */
  .section {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 24px; }}
  .section-title {{ font-size: 16px; font-weight: 600; margin-bottom: 16px; display: flex; justify-content: space-between; }}
  .dist-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; text-align: center; }}
  .dist-box {{ background: rgba(255,255,255,0.03); border: 1px solid var(--border); border-radius: 6px; padding: 12px; }}
  .dist-count {{ font-size: 20px; font-weight: 700; color: var(--accent); }}
  .dist-label {{ font-size: 12px; color: var(--text-muted); }}

  /* Controls */
  .controls {{ display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }}
  .search-input {{ flex: 1; min-width: 250px; background: var(--card-bg); border: 1px solid var(--border); color: var(--text); padding: 10px 14px; border-radius: 6px; }}
  .filter-select {{ background: var(--card-bg); border: 1px solid var(--border); color: var(--text); padding: 10px 14px; border-radius: 6px; }}

  /* Table */
  table {{ width: 100%; border-collapse: collapse; text-align: left; font-size: 13px; }}
  th {{ background: #172033; padding: 12px 14px; color: var(--text-muted); font-weight: 600; border-bottom: 1px solid var(--border); }}
  td {{ padding: 12px 14px; border-bottom: 1px solid var(--border); vertical-align: middle; }}
  tr:hover {{ background: rgba(56, 189, 248, 0.05); cursor: pointer; }}
  .badge {{ display: inline-block; padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
  .badge-success {{ background: rgba(34, 197, 94, 0.2); color: var(--success); }}
  .badge-warning {{ background: rgba(245, 158, 11, 0.2); color: var(--warning); }}
  .badge-danger {{ background: rgba(239, 68, 68, 0.2); color: var(--danger); }}
  .badge-info {{ background: rgba(56, 189, 248, 0.2); color: var(--accent); }}

  /* Drawer Modal */
  .modal-overlay {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.7); z-index: 100; justify-content: center; align-items: center; }}
  .modal-overlay.active {{ display: flex; }}
  .modal {{ background: var(--card-bg); width: 90%; max-width: 1000px; max-height: 85vh; border-radius: 8px; border: 1px solid var(--border); overflow-y: auto; padding: 24px; box-shadow: 0 20px 25px -5px rgba(0,0,0,0.5); }}
  .modal-header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 12px; margin-bottom: 16px; }}
  .close-btn {{ background: none; border: none; color: var(--text-muted); font-size: 22px; cursor: pointer; }}
  .tab-btn {{ background: none; border: 1px solid var(--border); color: var(--text-muted); padding: 6px 12px; border-radius: 4px; cursor: pointer; margin-right: 8px; }}
  .tab-btn.active {{ background: var(--accent); color: #000; font-weight: 600; }}
  pre {{ background: #090d16; padding: 14px; border-radius: 6px; overflow-x: auto; font-size: 12px; max-height: 400px; color: #38bdf8; }}
</style>
</head>
<body>

<div class="container">
  <header>
    <div>
      <h1>Document Ingestion Benchmark Simulator</h1>
      <p style="color: var(--text-muted); font-size: 13px; margin-top: 4px;">Run ID: {safe_run_id} • Engine: <strong>{safe_engine}</strong></p>
    </div>
    <div class="meta-tag">Timestamp: {safe_created_at}</div>
  </header>

  <!-- KPI Grid -->
  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-title">Total Documents</div>
      <div class="kpi-value">{total_docs}</div>
      <div class="kpi-sub">Pass Rate: <span style="color: var(--success); font-weight: 600;">{success_rate:.1f}%</span> ({successful}/{total_docs})</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Mean OCR Confidence</div>
      <div class="kpi-value">{avg_conf * 100:.1f}%</div>
      <div class="kpi-sub">Across all tokens & pages</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Avg Latency</div>
      <div class="kpi-value">{avg_latency:.2f}s</div>
      <div class="kpi-sub">Total Duration: {run_info.get("total_duration_sec", 0.0):.1f}s</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Tables Reconstructed</div>
      <div class="kpi-value">{total_tables}</div>
      <div class="kpi-sub">Full HTML Grid & Dictionaries</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Tokens Recovered</div>
      <div class="kpi-value" style="color: var(--accent);">{total_recovered}</div>
      <div class="kpi-sub">Missed by Paddle & caught by Tesseract</div>
    </div>
  </div>

  <!-- Confidence Distribution -->
  <div class="section">
    <div class="section-title">Accuracy & Confidence Distribution</div>
    <div class="dist-grid">
      <div class="dist-box">
        <div class="dist-count">{conf_bins['95-100%']}</div>
        <div class="dist-label">95% – 100% Conf</div>
      </div>
      <div class="dist-box">
        <div class="dist-count">{conf_bins['90-95%']}</div>
        <div class="dist-label">90% – 95% Conf</div>
      </div>
      <div class="dist-box">
        <div class="dist-count">{conf_bins['80-90%']}</div>
        <div class="dist-label">80% – 90% Conf</div>
      </div>
      <div class="dist-box">
        <div class="dist-count" style="color: var(--warning);">{conf_bins['<80%']}</div>
        <div class="dist-label">&lt; 80% Conf</div>
      </div>
    </div>
  </div>

  <!-- Documents Table -->
  <div class="section">
    <div class="section-title">
      <span>Processed Documents ({total_docs})</span>
      <span style="font-size: 12px; color: var(--text-muted); font-weight: normal;">Click any row to inspect text, tables, and recovered tokens</span>
    </div>

    <div class="controls">
      <input type="text" id="searchInput" class="search-input" placeholder="Search by file name or format..." onkeyup="filterDocs()">
      <select id="formatFilter" class="filter-select" onchange="filterDocs()">
        <option value="">All Formats</option>
        <option value="pdf">PDF</option>
        <option value="png">PNG</option>
        <option value="jpg">JPG</option>
        <option value="html">HTML</option>
      </select>
      <select id="statusFilter" class="filter-select" onchange="filterDocs()">
        <option value="">All Statuses</option>
        <option value="success">Success</option>
        <option value="failed">Failed</option>
        <option value="recovered">Has Recovered Tokens</option>
      </select>
    </div>

    <table>
      <thead>
        <tr>
          <th>File Name</th>
          <th>Format</th>
          <th>Engine Used</th>
          <th>Latency</th>
          <th>Confidence</th>
          <th>Tables</th>
          <th>Empty Cells</th>
          <th>Recovered</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody id="docTableBody">
      </tbody>
    </table>
  </div>
</div>

<!-- Drawer Modal -->
<div id="modalOverlay" class="modal-overlay" onclick="closeModal(event)">
  <div class="modal" onclick="event.stopPropagation()">
    <div class="modal-header">
      <h3 id="modalTitle" style="color: var(--accent);">Document Details</h3>
      <button class="close-btn" onclick="closeModal()">&times;</button>
    </div>
    <div style="margin-bottom: 16px;">
      <button class="tab-btn active" onclick="switchTab('text')">Clean Text</button>
      <button class="tab-btn" onclick="switchTab('tables')">HTML Tables</button>
      <button class="tab-btn" onclick="switchTab('lost')">Recovered / Lost Tokens</button>
      <button class="tab-btn" onclick="switchTab('audit')">Audit Details</button>
    </div>
    <div id="tabText"><pre id="cleanTextPreview"></pre></div>
    <div id="tabTables" style="display: none;"><div id="htmlTablesPreview" style="background:#090d16; padding:14px; border-radius:6px; overflow:auto;"></div></div>
    <div id="tabLost" style="display: none;"><pre id="lostTokensPreview"></pre></div>
    <div id="tabAudit" style="display: none;"><pre id="auditDetailsPreview"></pre></div>
  </div>
</div>

<script>
  const docsData = {docs_json};
  const lostTokensData = {lost_tokens_json};
  let activeDoc = null;

  function renderTable(data) {{
    const tbody = document.getElementById("docTableBody");
    tbody.innerHTML = "";
    data.forEach((doc, idx) => {{
      const tr = document.createElement("tr");
      tr.onclick = () => openDocModal(doc);

      const confPct = (doc.overall_confidence * 100).toFixed(1) + "%";
      function cell(value) {{
        const td = document.createElement("td");
        td.textContent = String(value);
        tr.appendChild(td);
      }}
      cell(doc.file_name);
      cell(doc.file_format.toUpperCase());
      cell(doc.engine_used);
      cell(doc.duration_sec.toFixed(2) + "s");
      cell(confPct);
      cell(doc.table_count);
      cell(doc.empty_table_cells + " (" + doc.empty_cell_pct.toFixed(1) + "%)");
      cell(doc.lost_tokens_count > 0 ? "+" + doc.lost_tokens_count + " recovered" : "-");
      cell(doc.status === "success" ? "SUCCESS" : "FAILED");
      tbody.appendChild(tr);
    }});
  }}

  function filterDocs() {{
    const q = document.getElementById("searchInput").value.toLowerCase();
    const fmt = document.getElementById("formatFilter").value.toLowerCase();
    const st = document.getElementById("statusFilter").value;

    const filtered = docsData.filter(d => {{
      const matchQ = d.file_name.toLowerCase().includes(q) || d.file_format.toLowerCase().includes(q);
      const matchFmt = !fmt || d.file_format.toLowerCase() === fmt;
      let matchSt = true;
      if (st === "success") matchSt = d.status === "success";
      if (st === "failed") matchSt = d.status === "failed";
      if (st === "recovered") matchSt = d.lost_tokens_count > 0;
      return matchQ && matchFmt && matchSt;
    }});
    renderTable(filtered);
  }}

  function openDocModal(doc) {{
    activeDoc = doc;
    document.getElementById("modalTitle").innerText = doc.file_name;
    document.getElementById("cleanTextPreview").innerText = doc.clean_text || "(No clean text)";
    document.getElementById("htmlTablesPreview").innerHTML = doc.html_content || "<p style='color:var(--text-muted)'>No HTML tables reconstructed.</p>";

    // Recovered tokens for this doc
    const recs = lostTokensData.filter(t => t.doc_id === doc.doc_id);
    document.getElementById("lostTokensPreview").innerText = recs.length
      ? JSON.stringify(recs, null, 2)
      : "(No missing tokens recovered; PaddleOCR extracted 100% of tokens)";

    document.getElementById("auditDetailsPreview").innerText = JSON.stringify({{
      audit_is_valid: Boolean(doc.audit_is_valid),
      requires_hitl: Boolean(doc.audit_requires_hitl),
      critical_issues: doc.critical_issues_count,
      warning_issues: doc.warning_issues_count,
      deskew_angle: doc.deskew_angle,
      error_message: doc.error_message
    }}, null, 2);

    switchTab('text');
    document.getElementById("modalOverlay").classList.add("active");
  }}

  function closeModal() {{
    document.getElementById("modalOverlay").classList.remove("active");
  }}

  function switchTab(tab) {{
    const tabs = ['text', 'tables', 'lost', 'audit'];
    tabs.forEach(t => {{
      document.getElementById('tab' + t.charAt(0).toUpperCase() + t.slice(1)).style.display = (t === tab) ? 'block' : 'none';
    }});
    const btns = document.querySelectorAll('.tab-btn');
    btns.forEach((btn, idx) => {{
      btn.classList.toggle('active', tabs[idx] === tab);
    }});
  }}

  // Initial render
  renderTable(docsData);
</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    return output_path
