"""SrotaAI FastAPI web app — pixel-faithful clone of the reference UI.

Run:
    .venv/bin/uvicorn srotaai.web.app:app --reload --port 8000
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from fastapi import FastAPI, Form, Request, HTTPException, BackgroundTasks
from fastapi.responses import (HTMLResponse, RedirectResponse, JSONResponse,
                                StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from srotaai.project import Project, SourceConfig
from srotaai.timewindow import TimeWindow
from srotaai import runner as runner_mod
from srotaai import signals as signal_job
from srotaai.audit import verify as audit_verify
from srotaai.web import metrics as M
from srotaai import connectors_extra  # noqa: F401  -- registers connectors
from srotaai.scheduler import SrotaScheduler


ROOT = Path(__file__).resolve().parent.parent.parent
WEB_DIR = Path(__file__).resolve().parent
DB_PATH = ROOT / "srotaai.db"
PROJECTS_DIR = ROOT / "projects"

app = FastAPI(title="SrotaAI", default_response_class=HTMLResponse)
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")),
          name="static")
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
import time as _time
_BUILD_ID = str(int(_time.time()))
templates.env.globals["build_id"] = _BUILD_ID


# Demo-reliability middleware: never let the browser cache HTML pages so a
# stale render from an earlier session can't hide on a hard-refresh-resistant
# embedded webview. Static files keep normal caching semantics.
@app.middleware("http")
async def _no_cache_html(request, call_next):
    response = await call_next(request)
    ct = response.headers.get("content-type", "")
    if ct.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.on_event("startup")
def _start_scheduler() -> None:
    try:
        SrotaScheduler.get(DB_PATH).start()
    except Exception:
        pass  # Don't block app startup if scheduler fails


@app.on_event("shutdown")
def _stop_scheduler() -> None:
    try:
        s = SrotaScheduler.get(DB_PATH)
        s.stop()
    except Exception:
        pass


def _common(active: str, store) -> dict:
    return {
        "active":   active,
        "version":  "v1.2.4-stable",
        "kpis":     M.system_kpis(store),
    }


def _store():
    return M.open_store(DB_PATH)


@app.get("/_ping", response_class=HTMLResponse)
def _ping():
    return HTMLResponse(
        '<!doctype html><html><body style="background:#fff;color:#000;font:16px sans-serif;padding:24px">'
        '<h1 style="color:#16a34a">SrotaAI server is up.</h1>'
        '<p>If you can read this, the server is healthy.</p>'
        '<p><a href="/">→ Go to System Overview</a></p>'
        '<p><a href="/signals">→ Go to Signal Triage Hub</a></p>'
        '<p><a href="/signals/531">→ Go to SIG-531 metformin × lactic acidosis</a></p>'
        '</body></html>'
    )


# ---------------------------------------------------------------------------
@app.get("/")
def overview(request: Request):
    s = _store()
    try:
        ctx = _common("overview", s)
        ctx.update(
            page_title="System Overview",
            health=M.source_health(s, limit=8),
            recent=M.recent_signals(s, limit=4),
            audit=M.audit_tail(s, limit=6),
            agent_log=M.agent_log(s, limit=8),
        )
        return templates.TemplateResponse(request, "overview.html", ctx)
    finally:
        s.close()


@app.get("/signals")
def signals_page(request: Request):
    s = _store()
    try:
        ctx = _common("signals", s)
        ctx.update(page_title="Triage Hub", signals=M.all_signals(s))
        return templates.TemplateResponse(request, "signals.html", ctx)
    finally:
        s.close()


@app.get("/projects")
def projects_page(request: Request):
    s = _store()
    try:
        ctx = _common("projects", s)
        ctx.update(page_title="Projects", project_rows=M.project_rows(s))
        return templates.TemplateResponse(request, "projects.html", ctx)
    finally:
        s.close()


@app.get("/sources")
def sources_page(request: Request):
    s = _store()
    try:
        ctx = _common("sources", s)
        ctx.update(
            page_title="Acquisition Health",
            sources=M.source_health(s, limit=200),
            debug_log=M.debug_log(s, limit=14),
        )
        return templates.TemplateResponse(request, "sources.html", ctx)
    finally:
        s.close()


@app.get("/compliance")
def compliance_page(request: Request):
    s = _store()
    try:
        ok, broken_at, total = audit_verify(s)
        ctx = _common("compliance", s)
        ctx.update(
            page_title="Compliance & Audit",
            audit=M.audit_tail(s, limit=200, full=True),
            chain_ok=ok, chain_broken_at=broken_at, chain_total=total,
        )
        return templates.TemplateResponse(request, "compliance.html", ctx)
    finally:
        s.close()


@app.get("/about")
def about_page(request: Request):
    s = _store()
    try:
        ctx = _common("about", s)
        c = s.conn
        stats = {
            "projects":   c.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
            "sources":    c.execute("SELECT COUNT(*) FROM sources WHERE enabled=1").fetchone()[0],
            "records":    c.execute("SELECT COUNT(*) FROM records").fetchone()[0],
            "signals":    c.execute("SELECT COUNT(*) FROM signals").fetchone()[0],
            "audit_rows": c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0],
        }
        ctx.update(page_title="About SrotaAI", stats=stats)
        return templates.TemplateResponse(request, "about.html", ctx)
    finally:
        s.close()


@app.get("/search")
def global_search(request: Request, q: str = ""):
    """Unified search across signals, projects, and sources."""
    q = (q or "").strip()
    s = _store()
    try:
        ctx = _common("search", s)
        signals_hits = []
        projects_hits = []
        sources_hits = []
        if q:
            like = f"%{q.lower()}%"
            c = s.conn
            signals_hits = [dict(r) for r in c.execute(
                """SELECT id, project_id, drug, event, prr, n, chi2
                     FROM signals
                     WHERE LOWER(drug) LIKE ? OR LOWER(event) LIKE ?
                     ORDER BY prr DESC LIMIT 30""",
                (like, like)
            ).fetchall()]
            projects_hits = [dict(r) for r in c.execute(
                """SELECT id, name, description, status, cadence
                     FROM projects
                     WHERE LOWER(id) LIKE ? OR LOWER(name) LIKE ?
                        OR LOWER(COALESCE(description,'')) LIKE ?
                     ORDER BY id LIMIT 20""",
                (like, like, like)
            ).fetchall()]
            sources_hits = [dict(r) for r in c.execute(
                """SELECT id, project_id, name, connector, enabled
                     FROM sources
                     WHERE LOWER(name) LIKE ? OR LOWER(connector) LIKE ?
                     ORDER BY id LIMIT 20""",
                (like, like)
            ).fetchall()]
        ctx.update(
            page_title=(f'Search: "{q}"' if q else "Search"),
            q=q,
            signals_hits=signals_hits,
            projects_hits=projects_hits,
            sources_hits=sources_hits,
            total_hits=len(signals_hits) + len(projects_hits) + len(sources_hits),
        )
        return templates.TemplateResponse(request, "search.html", ctx)
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Project create / detail
# ---------------------------------------------------------------------------
@app.get("/projects/new")
def project_create_form(request: Request):
    s = _store()
    try:
        ctx = _common("projects", s)
        ctx.update(
            page_title="Create Project",
            form={"id": "", "name": "", "description": "",
                   "keywords": "", "lookback_days": 30, "cadence": "daily"},
            error=None,
        )
        return templates.TemplateResponse(request, "project_create.html", ctx)
    finally:
        s.close()


@app.post("/projects/new")
async def project_create_submit(
    request: Request,
    project_id: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    keywords: str = Form(""),
    lookback_days: int = Form(30),
    cadence: str = Form("daily"),
    sources_json: str = Form("[]"),
):
    s = _store()
    try:
        try:
            sources = json.loads(sources_json or "[]")
        except json.JSONDecodeError:
            sources = []
        kws = [k.strip() for k in keywords.replace(",", "\n").splitlines()
               if k.strip()]
        if not project_id.strip() or not kws or not sources:
            ctx = _common("projects", s)
            ctx.update(
                page_title="Create Project",
                form={"id": project_id, "name": name,
                      "description": description, "keywords": keywords,
                      "lookback_days": lookback_days, "cadence": cadence},
                error="Project ID, at least one keyword, and at least one source are required.",
            )
            return templates.TemplateResponse(request, "project_create.html",
                                              ctx, status_code=400)

        if cadence not in ("real_time", "daily", "weekly", "manual"):
            cadence = "daily"

        proj = Project(
            id=project_id.strip(),
            name=name.strip() or project_id,
            description=description.strip(),
            keywords=kws,
            sources=[SourceConfig(
                name=src.get("name") or f"{src['connector']}-{i+1}",
                connector=src["connector"],
                params=src.get("params") or {},
                # Per-source latency is the project cadence — this is what
                # the scheduler uses to decide when to re-fetch.
                latency=cadence if cadence != "manual" else "daily",
                enabled=src.get("enabled", True),
            ) for i, src in enumerate(sources)],
            time_window=TimeWindow(lookback_days=int(lookback_days)),
        )
        s.save_project_full(proj)
        s.set_project_cadence(proj.id, cadence)
        s.set_project_status(proj.id, "running")
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        proj.to_yaml(PROJECTS_DIR / f"{proj.id}.yaml")
        # Kick off the first run immediately in a thread; the scheduler will
        # then take over per cadence.
        import threading
        from srotaai.scheduler import SrotaScheduler
        threading.Thread(
            target=lambda: SrotaScheduler.get(DB_PATH)._tick(),  # type: ignore
            daemon=True, name=f"srota-bootstrap-{proj.id}",
        ).start()
        return RedirectResponse(f"/projects/{proj.id}", status_code=303)
    finally:
        s.close()


@app.get("/projects/{pid}")
def project_detail(request: Request, pid: str, tab: str = "overview"):
    s = _store()
    try:
        proj = s.load_project(pid)
        if not proj:
            raise HTTPException(404)
        ctx = _common("projects", s)
        ctx.update(
            page_title=proj["name"] or proj["id"],
            project=proj,
            tab=tab,
            counts=M.project_counts(s, pid),
            sources=M.source_health(s, limit=200) if tab == "sources" else [],
            signals=M.project_signals(s, pid) if tab in ("overview", "signals") else [],
            entities=M.project_entities(s, pid, limit=100) if tab == "entities" else [],
        )
        return templates.TemplateResponse(request, "project_detail.html", ctx)
    finally:
        s.close()


@app.post("/projects/{pid}/pause")
def project_pause(pid: str):
    s = _store()
    try:
        s.set_project_status(pid, "paused")
        return RedirectResponse(f"/projects/{pid}", status_code=303)
    finally:
        s.close()


@app.post("/projects/{pid}/resume")
def project_resume(pid: str):
    s = _store()
    try:
        s.set_project_status(pid, "running")
        return RedirectResponse(f"/projects/{pid}", status_code=303)
    finally:
        s.close()


@app.post("/projects/{pid}/close")
def project_close(pid: str):
    s = _store()
    try:
        s.set_project_status(pid, "closed")
        return RedirectResponse(f"/projects/{pid}", status_code=303)
    finally:
        s.close()


@app.post("/projects/{pid}/run")
def project_run_now(pid: str, background_tasks: BackgroundTasks):
    """On-demand execution — kicks off ingest + signal-detection in the
    background so the HTTP request returns immediately. The user can
    poll /projects/{pid} (or watch the dashboard) for completion.
    """
    s = _store()
    try:
        proj_dict = s.load_project(pid)
        if not proj_dict:
            raise HTTPException(404)
        proj = Project(
            id=proj_dict["id"], name=proj_dict["name"],
            description=proj_dict["description"],
            keywords=proj_dict["keywords"],
            sources=[SourceConfig(**src) for src in proj_dict["sources"]],
            time_window=TimeWindow(**(proj_dict.get("time_window") or {})),
        )
        s.mark_run_started(pid)
    finally:
        s.close()

    def _do_run(project: Project):
        try:
            runner_mod.run_project(project, output_path=None,
                                   db_path=str(DB_PATH))
            signal_job.run(project.id, db_path=DB_PATH,
                           sentiment_filter=False, min_n=2)
        except Exception as exc:                              # noqa: BLE001
            print(f"[run_now] {project.id} failed: {exc!r}")
        finally:
            s2 = _store()
            try:
                s2.mark_run_completed(project.id)
            finally:
                s2.close()

    background_tasks.add_task(_do_run, proj)
    return JSONResponse({
        "ok": True,
        "status": "started",
        "project": pid,
        "msg": "Ingest + signal detection running in background; refresh in ~30s.",
    })


@app.post("/projects/{pid}/sources/add")
def project_add_source(pid: str,
                       url: str = Form(...),
                       name: str = Form(...)):
    """Attach an external HTML page (e.g. tata1mg, drugs.com) as a new
    source on a project. Uses the html_stealth connector with sane defaults.
    """
    s = _store()
    try:
        proj_dict = s.load_project(pid)
        if not proj_dict:
            raise HTTPException(404)
        # Slug-clean the source name to match DB constraints
        safe_name = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-")
        if not safe_name:
            raise HTTPException(400, "Invalid source name")
        # html_stealth pulls the page and yields review/comment-like blocks.
        # Default selectors are heuristic — the page can override later.
        new_src = SourceConfig(
            name=safe_name,
            connector="html_stealth",
            params={
                "url": url.strip(),
                "list_selector": "div.review-item, article, li.comment, div.user-review",
                "title_selector": "h2, h3, p.review-text, .title",
                "body_selector":  "p, .review-text, .body, .description",
                "link_selector":  "a",
            },
            latency="daily",
            enabled=True,
        )
        s.upsert_source(pid, new_src)
        # Persist back to YAML so cold-restarts pick it up
        proj = Project(
            id=proj_dict["id"], name=proj_dict["name"],
            description=proj_dict["description"],
            keywords=proj_dict["keywords"],
            sources=[SourceConfig(**src) for src in proj_dict["sources"]] + [new_src],
            time_window=TimeWindow(**(proj_dict.get("time_window") or {})),
        )
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        proj.to_yaml(PROJECTS_DIR / f"{pid}.yaml")
    finally:
        s.close()
    return RedirectResponse(f"/projects/{pid}?tab=sources", status_code=303)


@app.get("/projects/{pid}/records.csv")
def project_records_csv(pid: str):
    """Records-as-CSV download — replaces the live Content tab preview.

    Honours the user's preference of *not* exposing record bodies in the UI.
    """
    s = _store()
    try:
        rows = list(s.conn.execute(
            """SELECT r.id, r.posted_at, r.ingested_at, r.url,
                      r.text_redacted, r.matched_keywords_json,
                      r.sentiment_label, r.sentiment_score, r.pii_hits_count,
                      r.entities_json,
                      src.name AS source_name, src.connector
                 FROM records r JOIN sources src ON src.id = r.source_id
                 WHERE src.project_id = ?
                 ORDER BY r.ingested_at DESC""", (pid,)
        ).fetchall())
    finally:
        s.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "posted_at", "ingested_at", "source", "connector",
                "url", "matched_keywords", "sentiment_label",
                "sentiment_score", "pii_hits", "entities_count",
                "text_redacted"])
    for r in rows:
        ents = []
        if r["entities_json"]:
            try:
                ents = json.loads(r["entities_json"]) or []
            except Exception:
                ents = []
        w.writerow([
            r["id"], r["posted_at"] or "", r["ingested_at"] or "",
            r["source_name"], r["connector"], r["url"] or "",
            r["matched_keywords_json"] or "[]",
            r["sentiment_label"] or "",
            f'{(r["sentiment_score"] or 0):.3f}',
            r["pii_hits_count"] or 0,
            len(ents),
            (r["text_redacted"] or "").replace("\n", " ").replace("\r", " "),
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{pid}-records.csv"'},
    )


@app.get("/signals/{sig_id}")
def signal_detail(request: Request, sig_id: int):
    """Investigation Workspace — drug × event deep-dive (ref design)."""
    s = _store()
    try:
        detail = M.signal_detail(s, sig_id)
        if not detail:
            raise HTTPException(404)
        ctx = _common("signals", s)
        ctx.update(
            page_title="Investigation Workspace",
            detail=detail,
            current_batch=M.signal_current_batch(s, detail["project_id"], limit=4),
        )
        return templates.TemplateResponse(request, "signal_detail.html", ctx)
    finally:
        s.close()


@app.post("/projects/{pid}/delete")
def project_delete(pid: str):
    s = _store()
    try:
        s.delete_project(pid)
        yaml_path = PROJECTS_DIR / f"{pid}.yaml"
        if yaml_path.exists():
            yaml_path.unlink()
        return RedirectResponse("/projects", status_code=303)
    finally:
        s.close()


# ---------------------------------------------------------------------------
@app.get("/healthz", response_class=JSONResponse)
def healthz():
    return {"ok": True}
