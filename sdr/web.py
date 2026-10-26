"""日本語のブラウザー画面（FastAPI + Jinja2）。

画面処理は repo / jobs / csv_io / backup を呼ぶだけにし、SQL を直接書かない。
"""
from __future__ import annotations

import logging
import secrets
import tempfile
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import APP_NAME, __version__, auth, backup, csv_io, discovery, repo, seed
from . import classify as C
from .config import DEFAULT_SETTINGS, Paths, resource_dir
from .db import connect, migrate
from .jobs import JobManager
from . import compose, runtime, web_sales
from .inbound import SyncService
from .mail.oauth import PendingAuth
from .outbound import OutboundService
from .scheduler import Scheduler
from .secretstore import SecretStore

log = logging.getLogger("sdr.web")

PUBLIC_PATHS = ("/login", "/static/", "/healthz", "/u/")
SESSION_COOKIE = "sdr_session"
SETTING_LABELS = {
    "max_pages_per_site": "1サイトの最大取得ページ数",
    "min_interval_sec": "同一サイトへの取得間隔（秒・2以上）",
    "global_concurrency": "全体の同時調査数（再起動後に反映）",
    "timeout_sec": "通信の制限時間（秒）",
    "max_bytes": "1ページの最大応答サイズ（バイト）",
    "max_retries": "再試行の上限回数",
    "cache_hours": "取得キャッシュの有効時間（時間）",
    "firecrawl_monthly_credit_cap": "Firecrawl の月間使用上限（クレジット）",
    "firecrawl_max_results_per_run": "Firecrawl 1回あたりの最大取得件数",
}


def safe_href(url: Optional[str]) -> str:
    if not url:
        return "#"
    low = str(url).strip().lower()
    return str(url).strip() if low.startswith(("http://", "https://")) else "#"


def create_app(paths: Paths, mode: str = "local", allowed_hosts: set[str] | None = None,
               secure_cookies: bool = False, jobs: JobManager | None = None, start_jobs: bool = True,
               shutdown_cb: Callable[[], None] | None = None, load_seed: bool = True) -> FastAPI:
    paths.ensure()
    migrate(paths.db, paths.backups)
    if load_seed:
        _first_run_seed(paths)

    app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)
    res = resource_dir()
    templates = Jinja2Templates(directory=str(res / "templates"))
    templates.env.filters["href"] = safe_href
    templates.env.globals.update(APP_NAME=APP_NAME, VERSION=__version__, CATEGORIES=C.CATEGORIES, BASES=C.BASES,
                                 MODE=mode, CONTACT_STATUSES=repo.CONTACT_MANUAL_STATUSES)
    app.mount("/static", StaticFiles(directory=str(res / "static")), name="static")

    app.state.paths = paths
    app.state.mode = mode
    app.state.local_csrf = secrets.token_urlsafe(24)
    app.state.limiter = auth.LoginLimiter()
    app.state.jobs = jobs or JobManager(paths.db, paths.secrets)
    app.state.shutdown_cb = shutdown_cb
    # 営業・メールの処理（送信は必ず outbound の共通処理を通す）
    app.state.secrets = SecretStore(paths.secrets)
    app.state.outbound = OutboundService(paths.data_dir, paths.db, app.state.secrets)
    app.state.sync = SyncService(paths.data_dir, paths.db, app.state.secrets)
    app.state.scheduler = Scheduler(app.state.outbound, app.state.sync)
    app.state.oauth_pending = PendingAuth()
    app.state.port = None
    _c = connect(paths.db)
    try:
        compose.ensure_default_templates(_c)
        runtime.local_instance(paths.data_dir)
    finally:
        _c.close()
    if start_jobs:
        app.state.jobs.start()
        app.state.scheduler.start()

    def db():
        return connect(paths.db)

    # ------------------------------------------------------------------
    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if allowed_hosts is not None and host not in allowed_hosts:
            return Response("許可されていない接続先です（Host ヘッダー不一致）", status_code=400, media_type="text/plain; charset=utf-8")
        path = request.url.path
        if mode == "shared":
            conn = db()
            try:
                user = auth.session_user(conn, request.cookies.get(SESSION_COOKIE))
            finally:
                conn.close()
            if not user and not path.startswith(PUBLIC_PATHS):
                if request.method == "GET" and not path.startswith(("/api/", "/export/")):
                    return RedirectResponse("/login?" + urlencode({"next": path}), status_code=303)
                return JSONResponse({"error": "ログインが必要です"}, status_code=401)
            request.state.user = user
        else:
            request.state.user = {"username": "local", "csrf": app.state.local_csrf, "role": "admin"}
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin != "null" and urlsplit(origin).netloc != host:
                return Response("別のサイトからの操作は受け付けません", status_code=403, media_type="text/plain; charset=utf-8")
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        response.headers["Cache-Control"] = "no-store"
        return response

    def page(request: Request, name: str, **ctx):
        user = getattr(request.state, "user", None)
        ctx.update(request=request, user=user, csrf=(user or {}).get("csrf", ""))
        return templates.TemplateResponse(request, name, ctx)

    async def form_checked(request: Request):
        form = await request.form()
        user = getattr(request.state, "user", None) or {}
        token = form.get("csrf_token") or ""
        if not user.get("csrf") or not secrets.compare_digest(str(token), user["csrf"]):
            raise HTTPException(403, "画面を開き直してから操作してください（CSRF トークン不一致）")
        return form

    def actor(request: Request) -> str:
        return (getattr(request.state, "user", None) or {}).get("username", "local")

    def back(url: str, msg: str | None = None, err: str | None = None):
        q = {}
        if msg:
            q["msg"] = msg
        if err:
            q["err"] = err
        sep = "&" if "?" in url else "?"
        return RedirectResponse(url + (sep + urlencode(q) if q else ""), status_code=303)

    # ------------------------------------------------------------------
    @app.get("/healthz")
    def healthz():
        return {"ok": True, "app": APP_NAME, "version": __version__}

    # 専用ウィンドウ版：2 回目に起動されたとき、既存のウィンドウを前面に出す。
    # 合言葉は起動中の印（利用者フォルダー内の running.json）にだけ書かれ、同じ PC の同じ利用者しか知らない。
    app.state.focus_token = None
    app.state.focus_cb = None

    @app.post("/desktop/focus")
    def desktop_focus(request: Request):
        token = request.headers.get("x-sdr-focus-token") or ""
        expected = app.state.focus_token
        if mode != "local" or not expected or not secrets.compare_digest(token, expected) or not app.state.focus_cb:
            raise HTTPException(404, "見つかりません")
        app.state.focus_cb()
        return {"ok": True}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/"):
        if mode != "shared":
            return RedirectResponse("/", status_code=303)
        request.state.user = None
        return page(request, "login.html", next=next if next.startswith("/") and not next.startswith("//") else "/")

    @app.post("/login")
    async def login_post(request: Request):
        if mode != "shared":
            return RedirectResponse("/", status_code=303)
        form = await request.form()
        username = str(form.get("username", ""))
        key = f"{username}|{request.client.host if request.client else ''}"
        nxt = str(form.get("next") or "/")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        if app.state.limiter.blocked(key):
            return back("/login", err="ログインの失敗が続いたため、15分ほど待ってから再度お試しください")
        conn = db()
        try:
            res_ = auth.login(conn, username, str(form.get("password", "")))
        finally:
            conn.close()
        if not res_:
            app.state.limiter.fail(key)
            return back("/login", err="利用者名またはパスワードが違います")
        app.state.limiter.reset(key)
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(SESSION_COOKIE, res_[0], httponly=True, secure=secure_cookies, samesite="lax",
                        max_age=auth.SESSION_HOURS * 3600, path="/")
        return resp

    @app.post("/logout")
    async def logout_post(request: Request):
        await form_checked(request)
        conn = db()
        try:
            auth.logout(conn, request.cookies.get(SESSION_COOKIE))
        finally:
            conn.close()
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    # ------------------------------------------------------------------ 一覧
    @app.get("/dealers", response_class=HTMLResponse)
    def dealer_list(request: Request, q: str = "", region: str = "", category: str = "", has_email: str = "",
                    has_form: str = "", needs_review: str = "", status: str = "", origin: str = "",
                    include_archived: str = "", msg: str = "", err: str = ""):
        filters = {"q": q, "region": region, "category": category, "has_email": bool(has_email),
                   "has_form": bool(has_form), "needs_review": bool(needs_review), "status": status, "origin": origin,
                   "include_archived": bool(include_archived)}
        conn = db()
        try:
            dealers = repo.list_dealers(conn, filters)
            st = repo.stats(conn)
        finally:
            conn.close()
        export_q = urlencode({k: v for k, v in request.query_params.items() if k not in ("msg", "err")})
        return page(request, "dealers.html", dealers=dealers, f=filters, stats=st, msg=msg, err=err, export_q=export_q,
                    statuses=["未調査", "待機中", "調査中", "完了", "一部取得失敗", "取得失敗", "停止"],
                    origins=["初期調査データ", "利用者追加", "CSV取込", "候補発見"])

    # ------------------------------------------------------------------ 詳細
    @app.get("/dealers/{dealer_id}", response_class=HTMLResponse)
    def dealer_detail(request: Request, dealer_id: str, msg: str = "", err: str = ""):
        conn = db()
        try:
            d = repo.get_dealer(conn, dealer_id)
            if not d:
                raise HTTPException(404, "販売店が見つかりません")
            evidence = repo.list_evidence(conn, dealer_id)
            history = repo.list_history(conn, dealer_id)
            runs = repo.list_runs(conn, dealer_id)
            dup = repo.get_dealer(conn, d["duplicate_of"]) if d["duplicate_of"] else None
            parent = repo.get_dealer(conn, d["parent_dealer_id"]) if d["parent_dealer_id"] else None
            branches = [dict(r) for r in conn.execute(
                "SELECT id, COALESCE(name_manual, name_auto) AS name FROM dealers WHERE parent_dealer_id=?", (dealer_id,))]
            from . import sales as _sales
            deals = _sales.list_deals(conn, dealer_id=dealer_id)
            people = _sales.list_people(conn, dealer_id)
            stops = _sales.blocking_suppressions(conn, dealer_id, [])
            dmsgs = _sales.dealer_messages(conn, dealer_id)
        finally:
            conn.close()
        latest_run = runs[0]["id"] if runs else None
        ev_latest = [e for e in evidence if e["research_run_id"] == latest_run or e["origin"] == "手動"]
        ev_older = [e for e in evidence if e not in ev_latest]
        return page(request, "dealer.html", d=d, ev_latest=ev_latest, ev_older=ev_older, history=history, runs=runs,
                    dup=dup, parent=parent, branches=branches, msg=msg, err=err, deals=deals, people=people, stops=stops,
                    dmsgs=dmsgs)

    @app.post("/dealers/{dealer_id}/edit")
    async def dealer_edit(request: Request, dealer_id: str):
        form = await form_checked(request)
        values = {k: str(form.get(k, "")) for k in ("name", "region", "address", "summary", "notes", "eligibility_manual")}
        values["categories"] = form.getlist("categories")
        values["category_basis"] = str(form.get("category_basis", ""))
        if form.get("website_url"):
            values["website_url"] = str(form.get("website_url"))
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                repo.update_manual(conn, dealer_id, values, actor(request))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        except repo.RepoError as exc:
            return back(f"/dealers/{dealer_id}", err=str(exc))
        finally:
            conn.close()
        return back(f"/dealers/{dealer_id}", msg="保存しました（人の確定値として記録。再調査で上書きされません）")

    @app.post("/dealers/{dealer_id}/review")
    async def dealer_review(request: Request, dealer_id: str):
        form = await form_checked(request)
        conn = db()
        try:
            repo.set_review(conn, dealer_id, str(form.get("status", "")), actor(request))
        except repo.RepoError as exc:
            return back(f"/dealers/{dealer_id}", err=str(exc))
        finally:
            conn.close()
        return back(f"/dealers/{dealer_id}", msg="確認状態を更新しました")

    @app.post("/dealers/{dealer_id}/research")
    async def dealer_research(request: Request, dealer_id: str):
        await form_checked(request)
        jid = app.state.jobs.start_research([dealer_id], "個別の再調査")
        return back(f"/dealers/{dealer_id}", msg=f"再調査を開始しました（{jid}）。数十秒後に再読み込みしてください")

    @app.post("/dealers/{dealer_id}/contacts")
    async def dealer_add_contact(request: Request, dealer_id: str):
        form = await form_checked(request)
        conn = db()
        try:
            repo.add_contact_manual(conn, dealer_id, str(form.get("kind", "")), str(form.get("value", "")),
                                    str(form.get("source_url", "")) or None, str(form.get("note", "")) or None,
                                    str(form.get("person_name", "")) or None, str(form.get("person_role", "")) or None,
                                    actor(request))
        except repo.RepoError as exc:
            return back(f"/dealers/{dealer_id}", err=str(exc))
        finally:
            conn.close()
        return back(f"/dealers/{dealer_id}", msg="連絡方法を登録しました")

    @app.post("/contacts/{contact_id}/status")
    async def contact_status(request: Request, contact_id: str):
        form = await form_checked(request)
        conn = db()
        try:
            did = repo.set_contact_status(conn, contact_id, str(form.get("status", "")) or None, actor(request),
                                          str(form.get("note", "")) or None)
        except repo.RepoError as exc:
            return back("/dealers", err=str(exc))
        finally:
            conn.close()
        return back(f"/dealers/{did}", msg="連絡方法の状態を更新しました")

    @app.post("/dealers/{dealer_id}/duplicate")
    async def dealer_duplicate(request: Request, dealer_id: str):
        form = await form_checked(request)
        action = str(form.get("action", ""))
        conn = db()
        try:
            d = repo.get_dealer(conn, dealer_id)
            if not d:
                raise HTTPException(404)
            if action == "distinct":
                repo.update_manual(conn, dealer_id, {"duplicate_status": "別事業者と確認済み"}, actor(request))
            elif action == "branch":
                target = str(form.get("parent_id") or d["duplicate_of"] or "")
                repo.update_manual(conn, dealer_id, {"parent_dealer_id": target, "duplicate_status": "支店として関連付け"},
                                   actor(request))
            elif action == "unlink":
                repo.update_manual(conn, dealer_id, {"parent_dealer_id": "", "duplicate_status": None}, actor(request))
            elif action == "archive":
                repo.update_manual(conn, dealer_id, {"archived": 1, "duplicate_status": "重複のため非表示"}, actor(request))
            elif action == "unarchive":
                repo.update_manual(conn, dealer_id, {"archived": 0}, actor(request))
            else:
                return back(f"/dealers/{dealer_id}", err="不明な操作です")
        except repo.RepoError as exc:
            return back(f"/dealers/{dealer_id}", err=str(exc))
        finally:
            conn.close()
        return back(f"/dealers/{dealer_id}", msg="更新しました")

    # ------------------------------------------------------------------ 調査開始
    @app.post("/research/start")
    async def research_start(request: Request):
        form = await form_checked(request)
        scope = str(form.get("scope", ""))
        conn = db()
        try:
            if scope == "selected":
                ids = [str(x) for x in form.getlist("dealer_ids")]
            elif scope == "unresearched":
                ids = [r["id"] for r in conn.execute("SELECT id FROM dealers WHERE research_status IN ('未調査', '停止') AND archived=0")]
            elif scope == "failed":
                ids = [r["id"] for r in conn.execute("SELECT id FROM dealers WHERE research_status IN ('取得失敗', '一部取得失敗') AND archived=0")]
            else:
                ids = []
        finally:
            conn.close()
        if not ids:
            return back("/dealers", err="調査する販売店がありません")
        jid = app.state.jobs.start_research(ids, {"selected": "選択した販売店", "unresearched": "未調査の販売店",
                                                  "failed": "失敗した販売店の再実行"}.get(scope, ""))
        return back(f"/jobs/{jid}", msg=f"{len(ids)} 件の調査を開始しました")

    # ------------------------------------------------------------------ URL追加・CSV
    @app.get("/add", response_class=HTMLResponse)
    def add_page(request: Request, msg: str = "", err: str = ""):
        return page(request, "add.html", msg=msg, err=err, results=None)

    def _add_rows(request: Request, rows: list[dict], origin: str, start: bool):
        results, new_ids = [], []
        conn = db()
        try:
            for r in rows:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    did, created, note = repo.create_dealer(conn, r["url"], origin, r.get("name") or None,
                                                            r.get("region") or None, r.get("note") or None, by=actor(request))
                    conn.execute("COMMIT")
                    results.append((r["url"], did, note))
                    if created:
                        new_ids.append(did)
                except repo.RepoError as exc:
                    conn.execute("ROLLBACK")
                    results.append((r["url"], None, str(exc)))
        finally:
            conn.close()
        jid = app.state.jobs.start_research(new_ids, "追加したURLの調査") if (start and new_ids) else None
        return results, new_ids, jid

    @app.post("/add/urls", response_class=HTMLResponse)
    async def add_urls(request: Request):
        form = await form_checked(request)
        lines = [l.strip() for l in str(form.get("urls", "")).splitlines() if l.strip()][:200]
        rows = [{"url": l} for l in lines]
        results, new_ids, jid = _add_rows(request, rows, "利用者追加", bool(form.get("start")))
        return page(request, "add.html", results=results, new_count=len(new_ids), job_id=jid, msg="", err="")

    @app.post("/add/csv", response_class=HTMLResponse)
    async def add_csv(request: Request):
        form = await form_checked(request)
        upload = form.get("file")
        if not upload or not hasattr(upload, "read"):
            return page(request, "add.html", results=None, msg="", err="CSVファイルを選んでください")
        data = await upload.read(csv_io.MAX_IMPORT_BYTES + 1)
        rows, errors = csv_io.parse_import(data)
        results, new_ids, jid = _add_rows(request, rows, "CSV取込", bool(form.get("start")))
        return page(request, "add.html", results=results, new_count=len(new_ids), job_id=jid, msg="",
                    err="; ".join(errors))

    # ------------------------------------------------------------------ 候補発見
    @app.get("/discover", response_class=HTMLResponse)
    def discover_page(request: Request, region: str = "", category: str = "一般中古車", msg: str = "", err: str = ""):
        conn = db()
        try:
            settings = repo.get_settings(conn)
            used = repo.api_usage_this_month(conn, "firecrawl")
            recent = [j for j in repo.list_jobs(conn, 10) if j["kind"] == "discover"]
        finally:
            conn.close()
        has_key = app.state.secrets.has(discovery.FIRECRAWL_KEY)
        links = discovery.manual_search_links(category, region or "London（ロンドン中心部）")
        return page(request, "discover.html", regions=list(discovery.REGIONS), categories=list(discovery.CATEGORY_QUERIES),
                    region=region, category=category, links=links, settings=settings, used=used, has_key=has_key,
                    recent=recent, msg=msg, err=err)

    @app.post("/discover/run")
    async def discover_run(request: Request):
        form = await form_checked(request)
        provider = str(form.get("provider", ""))
        region = str(form.get("region", ""))
        category = str(form.get("category", ""))
        try:
            limit = max(1, min(50, int(str(form.get("limit", "20")))))
        except ValueError:
            limit = 20
        if region not in discovery.REGIONS:
            return back("/discover", err="地域を選んでください")
        app.state.jobs.start_discovery(provider, region, category, limit)
        return back("/discover?" + urlencode({"region": region, "category": category}),
                    msg="候補の発見を開始しました。完了後に「候補一覧」に表示されます")

    @app.get("/candidates", response_class=HTMLResponse)
    def candidates_page(request: Request, status: str = "未処理", msg: str = "", err: str = ""):
        conn = db()
        try:
            cands = repo.list_candidates(conn, status or None)
        finally:
            conn.close()
        return page(request, "candidates.html", cands=cands, status=status, msg=msg, err=err)

    @app.post("/candidates/action")
    async def candidates_action(request: Request):
        form = await form_checked(request)
        ids = [str(x) for x in form.getlist("ids")]
        action = str(form.get("action", ""))
        if not ids:
            return back("/candidates", err="候補を選んでください")
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if action == "exclude":
                repo.exclude_candidates(conn, ids)
                new_ids = []
            else:
                new_ids = repo.register_candidates(conn, ids, actor(request))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        if action == "register_research" and new_ids:
            jid = app.state.jobs.start_research(new_ids, "候補から登録した販売店の調査")
            return back(f"/jobs/{jid}", msg=f"{len(new_ids)} 件を登録し調査を開始しました")
        return back("/candidates", msg="除外しました" if action == "exclude" else f"{len(new_ids)} 件を販売店として登録しました")

    # ------------------------------------------------------------------ ジョブ
    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request, msg: str = "", err: str = ""):
        conn = db()
        try:
            jobs_ = repo.list_jobs(conn)
        finally:
            conn.close()
        return page(request, "jobs.html", jobs=jobs_, msg=msg, err=err)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str, msg: str = "", err: str = ""):
        conn = db()
        try:
            job = repo.job_view(conn, job_id)
            if not job:
                raise HTTPException(404, "ジョブが見つかりません")
            items = repo.job_items(conn, job_id)
        finally:
            conn.close()
        return page(request, "job.html", job=job, items=items, msg=msg, err=err)

    @app.get("/api/jobs/{job_id}")
    def job_api(job_id: str):
        conn = db()
        try:
            job = repo.job_view(conn, job_id)
        finally:
            conn.close()
        if not job:
            raise HTTPException(404)
        return {"status": job["status"], "counts": job["counts"], "total": job["total"], "done": job["done"],
                "message": job["message"]}

    @app.post("/jobs/{job_id}/{action}")
    async def job_action(request: Request, job_id: str, action: str):
        await form_checked(request)
        if action == "stop":
            app.state.jobs.stop(job_id)
            return back(f"/jobs/{job_id}", msg="停止を受け付けました")
        if action == "resume":
            app.state.jobs.resume(job_id)
            return back(f"/jobs/{job_id}", msg="再開しました")
        if action == "retry":
            jid = app.state.jobs.retry_failed(job_id)
            return back(f"/jobs/{jid}" if jid else f"/jobs/{job_id}", msg="失敗・停止分を再実行します" if jid else "再実行する対象がありません")
        raise HTTPException(404)

    # ------------------------------------------------------------------ CSV 出力
    def csv_response(data: bytes, name: str) -> Response:
        return Response(data, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/export/dealers.csv")
    def export_dealers(request: Request):
        params = request.query_params
        filters = {"q": params.get("q", ""), "region": params.get("region", ""), "category": params.get("category", ""),
                   "has_email": bool(params.get("has_email")), "has_form": bool(params.get("has_form")),
                   "needs_review": bool(params.get("needs_review")), "status": params.get("status", ""),
                   "origin": params.get("origin", ""), "include_archived": bool(params.get("include_archived"))}
        conn = db()
        try:
            data = csv_io.dealers_csv(repo.list_dealers(conn, filters))
        finally:
            conn.close()
        return csv_response(data, "dealers.csv")

    @app.get("/export/contacts.csv")
    def export_contacts():
        conn = db()
        try:
            return csv_response(csv_io.contacts_csv(conn), "contacts.csv")
        finally:
            conn.close()

    @app.get("/export/evidence.csv")
    def export_evidence():
        conn = db()
        try:
            return csv_response(csv_io.evidence_csv(conn), "evidence.csv")
        finally:
            conn.close()

    # ------------------------------------------------------------------ 設定
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, msg: str = "", err: str = ""):
        conn = db()
        try:
            settings = repo.get_settings(conn)
            used = repo.api_usage_this_month(conn, "firecrawl")
            schema = repo.get_meta(conn, "schema_version")
            seeded = repo.get_meta(conn, "seed_imported_at")
        finally:
            conn.close()
        backups = sorted(paths.backups.glob("*.zip"), reverse=True)[:20]
        has_key = app.state.secrets.has(discovery.FIRECRAWL_KEY)
        return page(request, "settings.html", settings=settings, labels=SETTING_LABELS, defaults=DEFAULT_SETTINGS,
                    used=used, has_key=has_key, backups=backups, data_dir=str(paths.data_dir), schema=schema,
                    seeded=seeded, msg=msg, err=err)

    @app.post("/settings")
    async def settings_post(request: Request):
        form = await form_checked(request)
        conn = db()
        try:
            for key in DEFAULT_SETTINGS:
                if form.get(key) not in (None, ""):
                    repo.set_setting(conn, key, str(form.get(key)))
        except repo.RepoError as exc:
            return back("/settings", err=str(exc))
        finally:
            conn.close()
        return back("/settings", msg="設定を保存しました（範囲外の値は上限・下限に合わせました）")

    @app.post("/settings/firecrawl-key")
    async def settings_key(request: Request):
        form = await form_checked(request)
        key = str(form.get("api_key", "")).strip()
        if form.get("clear"):
            app.state.secrets.delete(discovery.FIRECRAWL_KEY)
        elif key:
            app.state.secrets.set(discovery.FIRECRAWL_KEY, key)
        return back("/settings", msg="APIキーの設定を更新しました（OS の資格情報保管に保存し、データベース・バックアップ・CSVには保存しません）")

    # ------------------------------------------------------------------ バックアップ
    @app.post("/backup/create")
    async def backup_create(request: Request):
        await form_checked(request)
        out = backup.create_backup(paths.db, paths.backups)
        return back("/settings", msg=f"バックアップを作成しました: {out.name}")

    @app.get("/backup/download/{name}")
    def backup_download(name: str):
        target = (paths.backups / name).resolve()
        if target.parent != paths.backups.resolve() or not target.is_file() or target.suffix != ".zip":
            raise HTTPException(404)
        return FileResponse(target, filename=name, media_type="application/zip")

    @app.post("/backup/restore")
    async def backup_restore(request: Request):
        form = await form_checked(request)
        upload = form.get("file")
        if not form.get("confirm"):
            return back("/settings", err="復元するには確認欄にチェックを入れてください")
        if not upload or not hasattr(upload, "read"):
            return back("/settings", err="バックアップの ZIP を選んでください")
        jobs_ = app.state.jobs
        jobs_.paused = True
        try:
            with tempfile.TemporaryDirectory() as tmp:
                zp = Path(tmp) / "upload.zip"
                zp.write_bytes(await upload.read(2_100_000_000))
                manifest, pre = backup.restore_backup(zp, paths.db, paths.backups)
            migrate(paths.db, paths.backups)
            conn = db()
            try:
                repo.recover_interrupted(conn)
                runtime.mark_reconcile_required(conn, f"バックアップから復元（{manifest['created_at']} 作成）", actor(request))
            finally:
                conn.close()
        except backup.BackupError as exc:
            return back("/settings", err=str(exc))
        finally:
            jobs_.paused = False
        return back("/settings", msg=f"復元しました（作成日時 {manifest['created_at']}）。復元前のデータは {pre.name if pre else '-'} に保存しました。"
                    "誤送信を防ぐため送信モードを「停止」にしました。「照合」画面で外部の送信履歴を確認してから再開してください")

    @app.post("/seed/import")
    async def seed_import(request: Request):
        await form_checked(request)
        conn = db()
        try:
            n = seed.import_seed(conn)
        finally:
            conn.close()
        return back("/settings", msg=f"初期データから {n} 件を追加しました（既にある販売店は変更していません）")

    @app.get("/about", response_class=HTMLResponse)
    def about(request: Request):
        return page(request, "about.html", data_dir=str(paths.data_dir))

    @app.post("/shutdown")
    async def shutdown(request: Request):
        await form_checked(request)
        if mode == "shared":
            raise HTTPException(403, "共有モードでは画面から終了できません。サーバーPCで Ctrl+C を押してください")
        cb = app.state.shutdown_cb
        if cb:
            cb()
        return HTMLResponse("<!doctype html><meta charset='utf-8'><title>終了</title>"
                            "<p style='font-family:sans-serif;padding:2em'>アプリを終了しました。このタブは閉じてかまいません。</p>")

    web_sales.register(app, page=page, form_checked=form_checked, back=back, actor=actor, db=db, paths=paths, mode=mode,
                       templates=templates)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        user = getattr(request.state, "user", None)
        return templates.TemplateResponse(request, "message.html",
                                          {"request": request, "user": user, "csrf": (user or {}).get("csrf", ""),
                                           "title": f"エラー（{exc.status_code}）", "message": str(exc.detail)},
                                          status_code=exc.status_code)

    return app


def _first_run_seed(paths: Paths) -> None:
    conn = connect(paths.db)
    try:
        if repo.get_meta(conn, "seed_imported_at") is None and not conn.execute("SELECT 1 FROM dealers LIMIT 1").fetchone():
            n = seed.import_seed(conn)
            log.info("初期データ %s 件を読み込みました", n)
    finally:
        conn.close()
