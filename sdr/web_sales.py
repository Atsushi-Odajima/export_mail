"""営業機能の画面（ダッシュボード・案件・文面・テンプレート・自社情報・送信・予約・受信・連絡拒否・照合・メール接続・AI・利用者）。

画面からメールを直接送らない。送信は app.state.outbound（共通の送信前確認・二重送信防止）を通す。
設定変更（メール接続・送信モード・AI・利用者・照合後の再開）は管理者だけが行える。
"""
from __future__ import annotations

import hashlib
import json
import secrets as pysecrets
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from . import ai, auth, compose, runtime, sales
from .db import transaction, utcnow
from .ids import PREFIX_ATTACHMENT, new_id
from .mail import MailError
from .mail import oauth as oauth_mod
from .mail import testbox
from .mail.smtp_imap import DEFAULTS as SMTP_DEFAULTS
from .mail.smtp_imap import PRESETS as SMTP_PRESETS
from .tz import TZ_CHOICES, AmbiguousTimeError, TimeInputError, fmt, local_to_utc, split_local

MAX_ATTACHMENT = 10 * 1024 * 1024


def register(app: FastAPI, *, page, form_checked, back, actor, db, paths, mode, templates) -> None:
    env = templates.env
    env.filters["t"] = lambda value, tz="Asia/Tokyo": fmt(value, tz)
    env.globals.update(STEPS=sales.STEPS, DEAL_STAGES=sales.DEAL_STAGES, PROVIDERS=sales.PROVIDERS, TZ_CHOICES=TZ_CHOICES,
                       SIMULATIONS=testbox.SIMULATIONS, INJECT_KINDS=testbox.INJECT_KINDS, AI_MODELS=ai.MODELS)

    # ------------------------------------------------------------------ 共通
    def tz_of(request: Request, conn=None) -> str:
        q = request.query_params.get("tz")
        if q in TZ_CHOICES:
            return q
        c = request.cookies.get("sdr_tz")
        if c in TZ_CHOICES:
            return c
        own = conn or db()
        try:
            return runtime.get_option(own, "display_tz") or "Asia/Tokyo"
        finally:
            if conn is None:
                own.close()

    def P(request: Request, name: str, **ctx):
        ctx.setdefault("tz", tz_of(request))
        ctx.setdefault("msg", request.query_params.get("msg", ""))
        ctx.setdefault("err", request.query_params.get("err", ""))
        user = getattr(request.state, "user", None) or {}
        ctx.setdefault("is_admin", user.get("role") == "admin")
        resp = page(request, name, **ctx)
        if request.query_params.get("tz") in TZ_CHOICES:
            resp.set_cookie("sdr_tz", request.query_params["tz"], max_age=3600 * 24 * 365, samesite="lax", path="/")
        return resp

    def require_admin(request: Request) -> None:
        user = getattr(request.state, "user", None) or {}
        if user.get("role") != "admin":
            raise HTTPException(403, "この操作は管理者だけが行えます")

    def run_tx(fn):
        conn = db()
        try:
            with transaction(conn):
                return fn(conn)
        finally:
            conn.close()

    def ok_or_err(url: str, fn, ok_msg: str | None = None):
        try:
            r = run_tx(fn)
        except (sales.SalesError, ValueError, TimeInputError, ai.AIError, MailError) as exc:
            return back(url, err=str(exc))
        return back(url, msg=(r if isinstance(r, str) and ok_msg is None else ok_msg) or "保存しました")

    def with_conn(fn):
        """トランザクションの外で実行する（AI の利用記録は失敗も含めてその場で保存される）。"""
        conn = db()
        try:
            return fn(conn)
        finally:
            conn.close()

    def person_of(conn, person_id: str | None) -> dict | None:
        row = conn.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone() if person_id else None
        return dict(row) if row else None

    async def ai_then_save(url: str, gen, save, ok_msg: str | None = None):
        """時間のかかる AI の呼び出しは別スレッドで、DB の書き込みロックを持たずに行い（画面と他の利用者を待たせない）、
        結果の保存だけをトランザクションで行う。"""
        try:
            r = await run_in_threadpool(with_conn, gen)
        except (sales.SalesError, ai.AIError) as exc:
            return back(url, err=str(exc))
        return ok_or_err(url, lambda c: save(c, r), ok_msg)

    def parse_time(form, prefix: str = "") -> tuple[str, str]:
        tzv = str(form.get(prefix + "tz") or "Asia/Tokyo")
        fold_raw = str(form.get(prefix + "fold") or "")
        fold = int(fold_raw) if fold_raw in ("0", "1") else None
        dt = local_to_utc(str(form.get(prefix + "date") or ""), str(form.get(prefix + "time") or ""), tzv, fold)
        return dt.replace(microsecond=0).isoformat(), tzv

    # ------------------------------------------------------------------ ダッシュボード
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        conn = db()
        try:
            st = sales.dashboard(conn)
            accounts = sales.list_accounts(conn)
            opts = runtime.options(conn)
            mode_ = runtime.send_mode(conn)
            recent = [dict(r) for r in conn.execute(
                "SELECT a.*, r.subject FROM send_attempts a LEFT JOIN drafts r ON r.id=a.draft_id ORDER BY a.id DESC LIMIT 8")]
            reconcile = runtime.get_state(conn, "reconcile_required") == "1"
        finally:
            conn.close()
        sched = app.state.scheduler.status()
        return P(request, "dashboard.html", st=st, accounts=accounts, opts=opts, send_mode=mode_, recent=recent, sched=sched,
                 reconcile=reconcile, ai_status=_ai_status())

    def _ai_status():
        conn = db()
        try:
            return ai.status(conn, app.state.secrets)
        finally:
            conn.close()

    # ------------------------------------------------------------------ 案件
    @app.get("/deals", response_class=HTMLResponse)
    def deals_page(request: Request, stage: str = "", owner: str = ""):
        conn = db()
        try:
            deals = sales.list_deals(conn, stage, owner)
            dealers = [dict(r) for r in conn.execute(
                "SELECT id, COALESCE(name_manual, name_auto, website_host) AS name FROM dealers WHERE archived=0 ORDER BY name")]
        finally:
            conn.close()
        return P(request, "deals.html", deals=deals, dealers=dealers, stage=stage, owner=owner)

    @app.post("/deals/new")
    async def deal_new(request: Request):
        form = await form_checked(request)
        dealer_id = str(form.get("dealer_id", ""))
        try:
            did = run_tx(lambda c: sales.create_deal(c, dealer_id, str(form.get("title") or "英国向け中古車の営業"),
                                                       str(form.get("owner") or "") or actor(request), actor(request),
                                                       str(form.get("person_id") or "") or None))
        except sales.SalesError as exc:
            return back("/deals", err=str(exc))
        return back(f"/deals/{did}", msg="案件を作成しました")

    @app.get("/deals/{deal_id}", response_class=HTMLResponse)
    def deal_page(request: Request, deal_id: str):
        conn = db()
        try:
            deal = sales.get_deal(conn, deal_id)
            if not deal:
                raise HTTPException(404, "案件が見つかりません")
            from . import repo
            dealer = repo.get_dealer(conn, deal["dealer_id"])
            people = sales.list_people(conn, deal["dealer_id"])
            drafts = sales.list_drafts(conn, deal_id)
            schedules = sales.list_schedules(conn, deal_id=deal_id)
            messages = sales.deal_messages(conn, deal_id)
            accounts = [a for a in sales.list_accounts(conn) if a["enabled"]]
            supp = sales.blocking_suppressions(conn, deal["dealer_id"], [])
            other = [m for m in sales.dealer_messages(conn, deal["dealer_id"]) if m["deal_id"] != deal_id]
        finally:
            conn.close()
        emails = [c["value"] for c in dealer["emails_ok"]] + [p["email"] for p in people if p.get("email")]
        return P(request, "deal.html", deal=deal, dealer=dealer, people=people, drafts=drafts, schedules=schedules,
                 messages=messages, other_messages=other, accounts=accounts, emails=emails, supp=supp,
                 ai_status=_ai_status())

    @app.post("/deals/{deal_id}/edit")
    async def deal_edit(request: Request, deal_id: str):
        form = await form_checked(request)
        values = {k: str(form.get(k, "")) for k in ("title", "owner", "stage", "next_action", "notes", "send_basis", "person_id")}
        values["send_allowed"] = "1" if form.get("send_allowed") else "0"
        if form.get("next_date"):
            try:
                values["next_action_at"], _ = parse_time({"date": form.get("next_date"), "time": form.get("next_time") or "09:00",
                                                          "tz": form.get("tz") or "Asia/Tokyo", "fold": "0"})
            except TimeInputError as exc:
                return back(f"/deals/{deal_id}", err=str(exc))
        else:
            values["next_action_at"] = ""
        return ok_or_err(f"/deals/{deal_id}", lambda c: sales.update_deal(c, deal_id, values, actor(request)), "案件を更新しました")

    @app.post("/dealers/{dealer_id}/people")
    async def person_add(request: Request, dealer_id: str):
        form = await form_checked(request)
        nxt = str(form.get("next") or f"/dealers/{dealer_id}")
        return ok_or_err(nxt if nxt.startswith("/") else "/", lambda c: sales.add_person(
            c, dealer_id, str(form.get("name", "")), str(form.get("role", "")), str(form.get("email", "")),
            str(form.get("phone", "")), str(form.get("source_url", "")), str(form.get("notes", "")), actor(request)),
            "担当者を登録しました")

    @app.post("/dealers/{dealer_id}/stop")
    async def dealer_stop(request: Request, dealer_id: str):
        form = await form_checked(request)
        return ok_or_err(f"/dealers/{dealer_id}", lambda c: sales.add_suppression(
            c, "会社", actor(request), dealer_id=dealer_id, reason=str(form.get("reason") or "会社単位で送信停止（手動）")),
            "この会社への送信を停止しました（予約は保留になりました）")

    # ------------------------------------------------------------------ 文面の作成
    def _new_draft_values(conn, deal: dict, step: str, account_id: str, to_address: str, person_id: str | None,
                          reply_to: dict | None = None) -> tuple[dict, dict | None, str]:
        person = conn.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone() if person_id else None
        person = dict(person) if person else None
        prev = conn.execute("SELECT subject FROM messages WHERE deal_id=? ORDER BY COALESCE(sent_at, received_at) DESC LIMIT 1",
                            (deal["id"],)).fetchone()
        prev_subject = (reply_to or {}).get("subject") or (prev["subject"] if prev else "")
        prev_subject = prev_subject[4:] if prev_subject.lower().startswith("re: ") else prev_subject
        base = runtime.get_option(conn, "public_base_url")
        token_url = None
        if base.startswith("https://") and to_address and step != "reply":
            token = pysecrets.token_urlsafe(18)
            conn.execute("INSERT INTO unsubscribe_tokens(token_hash, dealer_id, address, created_at) VALUES(?,?,?,?)",
                         (hashlib.sha256(token.encode()).hexdigest(), deal["dealer_id"], to_address.lower(), utcnow()))
            token_url = base.rstrip("/") + "/u/" + token
        values = compose.variables(conn, deal, person, prev_subject, compose.unsubscribe_text(base or None, token_url))
        return values, person, prev_subject

    @app.post("/deals/{deal_id}/drafts/new")
    async def draft_new(request: Request, deal_id: str):
        form = await form_checked(request)
        method = str(form.get("method", "template"))
        step = str(form.get("step", "initial"))
        account_id = str(form.get("account_id", "")) or None
        to_address = str(form.get("to_address", "")).strip()
        person_id = str(form.get("person_id", "")) or None
        instruction = str(form.get("instruction", ""))
        generated = None
        if method == "ai":
            def gen(c):
                deal = sales.get_deal(c, deal_id)
                if not deal:
                    raise HTTPException(404)
                return ai.generate_draft(c, app.state.secrets, deal, step, instruction, person_of(c, person_id))
            try:
                generated = await run_in_threadpool(with_conn, gen)
            except (sales.SalesError, ai.AIError) as exc:
                return back(f"/deals/{deal_id}", err=str(exc))
        conn = db()
        try:
            with transaction(conn):
                deal = sales.get_deal(conn, deal_id)
                if not deal:
                    raise HTTPException(404)
                values, person, _ = _new_draft_values(conn, deal, step, account_id or "", to_address, person_id)
                base = {"account_id": account_id, "to_address": to_address}
                if method == "template":
                    t = sales.pick_template(conn, step, compose.dealer_facts(conn, deal["dealer_id"])["categories"])
                    if form.get("template_id"):
                        t = sales.get_template(conn, str(form.get("template_id")))
                    if not t:
                        raise sales.SalesError("使えるテンプレートがありません")
                    did = sales.create_draft(conn, deal_id, step, {**base, "subject": compose.render(t["subject"], values),
                                                                    "body": compose.render(t["body"], values)},
                                             "定型文", actor(request), template_id=t["id"], template_version=t["version"],
                                             basis_json=compose.basis_for(conn, deal, t, values))
                elif method == "ai":
                    r = generated
                    did = sales.create_draft(conn, deal_id, step, {**base, "subject": r["subject"], "body": r["body"]}, "AI",
                                             actor(request), ai_usage_id=r["usage_id"], summary_ja=r["summary_ja"],
                                             basis_json=json.dumps({"used_facts": r["used_facts"], "instruction_ja": instruction},
                                                                   ensure_ascii=False))
                else:
                    did = sales.create_draft(conn, deal_id, step, {**base, "subject": "", "body": ""}, "手動", actor(request),
                                             basis_json=json.dumps({"instruction_ja": instruction}, ensure_ascii=False))
        except (sales.SalesError, ai.AIError) as exc:
            return back(f"/deals/{deal_id}", err=str(exc))
        finally:
            conn.close()
        return back(f"/drafts/{did}", msg="下書きを作成しました。内容を確認・編集してから承認してください")

    @app.get("/drafts", response_class=HTMLResponse)
    def drafts_page(request: Request, status: str = ""):
        conn = db()
        try:
            drafts = sales.list_drafts(conn, status=status)
        finally:
            conn.close()
        return P(request, "drafts.html", drafts=drafts, status=status)

    @app.get("/drafts/{draft_id}", response_class=HTMLResponse)
    def draft_page(request: Request, draft_id: str):
        conn = db()
        try:
            d = sales.get_draft(conn, draft_id)
            if not d:
                raise HTTPException(404, "文面が見つかりません")
            deal = sales.get_deal(conn, d["deal_id"])
            approval = sales.current_approval(conn, d)
            approvals = [dict(r) for r in conn.execute("SELECT * FROM approvals WHERE draft_id=? ORDER BY id DESC", (draft_id,))]
            versions = [dict(r) for r in conn.execute("SELECT * FROM draft_versions WHERE draft_id=? ORDER BY version DESC", (draft_id,))]
            attempts = [dict(r) for r in conn.execute("SELECT * FROM send_attempts WHERE draft_id=? ORDER BY id DESC", (draft_id,))]
            schedules = [dict(r) for r in conn.execute("SELECT * FROM schedules WHERE draft_id=? ORDER BY send_at_utc", (draft_id,))]
            accounts = [a for a in sales.list_accounts(conn) if a["enabled"]]
            problems = sales.approval_problems(conn, d)
            preflight = app.state.outbound.preflight(conn, d) if approval else []
            dups = compose.duplicate_warnings(conn, d["dealer_id"], d["subject"], d["body"], exclude_draft=draft_id)
            attachments = sales.draft_attachments(conn, draft_id)
            person = conn.execute("SELECT * FROM people WHERE id=?", (deal["person_id"],)).fetchone() if deal.get("person_id") else None
            sys_prompt, user_prompt = compose.ai_prompt(conn, deal, d["step"], "", dict(person) if person else None)
            usage = conn.execute("SELECT * FROM ai_usage WHERE id=?", (d["ai_usage_id"],)).fetchone() if d["ai_usage_id"] else None
        finally:
            conn.close()
        basis = json.loads(d["basis_json"]) if d.get("basis_json") else {}
        return P(request, "draft.html", d=d, deal=deal, approval=approval, approvals=approvals, versions=versions, attempts=attempts,
                 schedules=schedules, accounts=accounts, problems=problems, preflight=preflight, dups=dups,
                 attachments=attachments, ai_prompt=sys_prompt + "\n\n" + user_prompt, basis=basis,
                 usage=dict(usage) if usage else None, ai_status=_ai_status())

    @app.post("/drafts/{draft_id}/save")
    async def draft_save(request: Request, draft_id: str):
        form = await form_checked(request)
        values = {k: str(form.get(k, "")) for k in ("account_id", "to_address", "cc", "bcc", "subject", "body")}
        values["account_id"] = values["account_id"] or None

        def fn(c):
            for k in ("to_address", "cc", "bcc"):
                sales.validate_addresses(values[k])
            changed = sales.update_draft(c, draft_id, values, actor(request), source=None)
            return "保存しました（新しい版になりました。承認は取り消されています）" if changed else "変更はありません"
        return ok_or_err(f"/drafts/{draft_id}", fn)

    @app.post("/drafts/{draft_id}/approve")
    async def draft_approve(request: Request, draft_id: str):
        await form_checked(request)
        return ok_or_err(f"/drafts/{draft_id}", lambda c: sales.approve_draft(c, draft_id, actor(request)),
                         "この版を承認しました（宛先・本文などを変えると承認は取り消されます）")

    @app.post("/drafts/{draft_id}/send-now")
    async def draft_send(request: Request, draft_id: str):
        form = await form_checked(request)
        if not form.get("confirm"):
            return back(f"/drafts/{draft_id}", err="送信するには確認欄にチェックを入れてください")
        r = await run_in_threadpool(app.state.outbound.send_draft, draft_id, actor(request))
        if r["result"] == "accepted":
            return back(f"/drafts/{draft_id}", msg=r["message"])
        if r["result"] == "blocked":
            return back(f"/drafts/{draft_id}", err="送信しませんでした：" + " / ".join(r["problems"]))
        return back(f"/drafts/{draft_id}", err=r.get("message", "送信できませんでした"))

    @app.post("/drafts/{draft_id}/schedule")
    async def draft_schedule(request: Request, draft_id: str):
        form = await form_checked(request)
        try:
            at, tzv = parse_time(form)
        except AmbiguousTimeError as exc:
            return back(f"/drafts/{draft_id}", err=str(exc) + "（下の「2 回ある時刻」で選んでから再度押してください）")
        except TimeInputError as exc:
            return back(f"/drafts/{draft_id}", err=str(exc))

        def fn(c):
            d = sales.get_draft(c, draft_id)
            acc = str(form.get("account_id") or d.get("account_id") or "")
            if not acc:
                raise sales.SalesError("送信元アカウントを選んでください")
            sid = sales.create_schedule(c, d["deal_id"], draft_id, acc, d["step"], at, tzv, actor(request))
            s = sales.get_schedule(c, sid)
            return f"予約しました（{fmt(at, tzv)}・状態：{s['status']}）"
        return ok_or_err(f"/drafts/{draft_id}", fn)

    @app.post("/drafts/{draft_id}/duplicate")
    async def draft_dup(request: Request, draft_id: str):
        await form_checked(request)
        try:
            nid = run_tx(lambda c: sales.duplicate_draft(c, draft_id, actor(request)))
        except sales.SalesError as exc:
            return back(f"/drafts/{draft_id}", err=str(exc))
        return back(f"/drafts/{nid}", msg="複製しました（新しい下書き・未承認）")

    @app.post("/drafts/{draft_id}/ai")
    async def draft_ai(request: Request, draft_id: str):
        form = await form_checked(request)
        instruction = str(form.get("instruction", ""))

        def gen(c):
            d = sales.get_draft(c, draft_id)
            deal = sales.get_deal(c, d["deal_id"])
            return ai.generate_draft(c, app.state.secrets, deal, d["step"], instruction, person_of(c, deal.get("person_id")))

        def save(c, r):
            sales.update_draft(c, draft_id, {"subject": r["subject"], "body": r["body"]}, actor(request), source="AI",
                               ai_usage_id=r["usage_id"], summary_ja=r["summary_ja"],
                               basis_json=json.dumps({"used_facts": r["used_facts"], "instruction_ja": instruction}, ensure_ascii=False))
            return "AI で作り直しました（承認は取り消されています。内容を確認してください）"
        return await ai_then_save(f"/drafts/{draft_id}", gen, save)

    @app.post("/drafts/{draft_id}/paste")
    async def draft_paste(request: Request, draft_id: str):
        form = await form_checked(request)
        parsed = compose.parse_manual_ai(str(form.get("text", "")))
        if not parsed["body"]:
            return back(f"/drafts/{draft_id}", err="貼り付けた内容から本文を読み取れませんでした")

        def fn(c):
            d = sales.get_draft(c, draft_id)
            values = {"body": parsed["body"]}
            if parsed["subject"]:
                values["subject"] = parsed["subject"]
            sales.update_draft(c, draft_id, values, actor(request), source="AI手動貼付",
                               summary_ja=parsed["summary_ja"] or None,
                               basis_json=json.dumps({"used_facts": parsed["used_facts"], "manual_ai": True}, ensure_ascii=False))
            return "貼り付けた文面を下書きにしました（承認は取り消されています）"
        return ok_or_err(f"/drafts/{draft_id}", fn)

    @app.post("/drafts/{draft_id}/summary")
    async def draft_summary(request: Request, draft_id: str):
        await form_checked(request)

        def gen(c):
            d = sales.get_draft(c, draft_id)
            return ai.summarize_ja(c, app.state.secrets, f"Subject: {d['subject']}\n\n{d['body']}", d["dealer_id"])

        def save(c, summary):
            sales.update_draft(c, draft_id, {}, actor(request), summary_ja=summary)
            return "日本語の内容確認を作成しました（AI による要約です。翻訳の正確さは保証しません）"
        return await ai_then_save(f"/drafts/{draft_id}", gen, save)

    @app.post("/drafts/{draft_id}/attachments")
    async def draft_attach(request: Request, draft_id: str):
        form = await form_checked(request)
        upload = form.get("file")
        if not upload or not hasattr(upload, "read"):
            return back(f"/drafts/{draft_id}", err="ファイルを選んでください")
        data = await upload.read(MAX_ATTACHMENT + 1)
        if len(data) > MAX_ATTACHMENT:
            return back(f"/drafts/{draft_id}", err="添付は 10MB までです")
        name = Path(getattr(upload, "filename", "") or "attachment").name[:120]
        ctype = getattr(upload, "content_type", None) or "application/octet-stream"
        (paths.data_dir / "attachments").mkdir(exist_ok=True)
        aid = new_id(PREFIX_ATTACHMENT)
        (paths.data_dir / "attachments" / aid).write_bytes(data)

        def fn(c):
            d = sales.get_draft(c, draft_id)
            if d["status"] == "送信済み":
                raise sales.SalesError("送信済みの文面には添付できません")
            c.execute("INSERT INTO attachments(id, draft_id, filename, content_type, size, sha256, stored_name, created_at) "
                      "VALUES(?,?,?,?,?,?,?,?)", (aid, draft_id, name, ctype, len(data), hashlib.sha256(data).hexdigest(), aid, utcnow()))
            sales.update_draft(c, draft_id, {}, actor(request))
            sales.revoke_approvals(c, draft_id, "添付が変わったため")
            c.execute("UPDATE drafts SET version=version+1, status='下書き', updated_at=? WHERE id=?", (utcnow(), draft_id))
            return "添付しました（承認は取り消されています）"
        return ok_or_err(f"/drafts/{draft_id}", fn)

    @app.post("/attachments/{att_id}/delete")
    async def attach_delete(request: Request, att_id: str):
        await form_checked(request)
        conn = db()
        try:
            a = conn.execute("SELECT * FROM attachments WHERE id=?", (att_id,)).fetchone()
        finally:
            conn.close()
        if not a:
            raise HTTPException(404)

        def fn(c):
            c.execute("DELETE FROM attachments WHERE id=?", (att_id,))
            sales.revoke_approvals(c, a["draft_id"], "添付が変わったため")
            c.execute("UPDATE drafts SET version=version+1, status='下書き', updated_at=? WHERE id=?", (utcnow(), a["draft_id"]))
            return "添付を外しました（承認は取り消されています）"
        return ok_or_err(f"/drafts/{a['draft_id']}", fn)

    # ------------------------------------------------------------------ 一連の予定
    @app.post("/deals/{deal_id}/sequence")
    async def sequence_new(request: Request, deal_id: str):
        form = await form_checked(request)
        account_id = str(form.get("account_id", ""))
        to_address = str(form.get("to_address", "")).strip()
        method = str(form.get("method", "template"))
        steps = [s for s in ("initial", "followup1", "followup2", "reactivation") if form.get(f"use_{s}")]
        if not steps:
            return back(f"/deals/{deal_id}", err="送る回を 1 つ以上選んでください")
        times = {}
        try:
            for s in steps:
                times[s] = parse_time(form, prefix=f"{s}_")
        except TimeInputError as exc:
            return back(f"/deals/{deal_id}", err=f"{str(exc)}")
        ordered = [times[s][0] for s in steps]
        if ordered != sorted(ordered):
            return back(f"/deals/{deal_id}", err="送信日時は 初回 → 2回目 → 3回目 の順になるようにしてください")

        def gen(c):
            sales.validate_addresses(to_address, required=True)
            if method != "ai":
                return {}
            deal = sales.get_deal(c, deal_id)
            person = person_of(c, deal.get("person_id"))
            return {s: ai.generate_draft(c, app.state.secrets, deal, s, str(form.get("instruction", "")), person) for s in steps}

        def save(c, generated):
            deal = sales.get_deal(c, deal_id)
            seq = sales.create_sequence(c, deal_id, str(form.get("name") or "営業の一連の予定"), bool(form.get("stop_on_reply", "1")),
                                        actor(request))
            cats = compose.dealer_facts(c, deal["dealer_id"])["categories"]
            last_subject = None
            for s in steps:
                # 一連の予定では、まだ送っていない前の回の件名を「前回の件名」として使う
                values, _, _ = _new_draft_values(c, deal, s, account_id, to_address, deal.get("person_id"),
                                                 reply_to={"subject": last_subject} if last_subject and s != "reactivation" else None)
                if method == "ai":
                    r = generated[s]
                    did = sales.create_draft(c, deal_id, s, {"account_id": account_id, "to_address": to_address,
                                                             "subject": r["subject"], "body": r["body"]}, "AI", actor(request),
                                             ai_usage_id=r["usage_id"], summary_ja=r["summary_ja"])
                else:
                    t = sales.pick_template(c, s, cats)
                    if not t:
                        raise sales.SalesError(f"{sales.STEPS[s]}のテンプレートがありません")
                    did = sales.create_draft(c, deal_id, s, {"account_id": account_id, "to_address": to_address,
                                                             "subject": compose.render(t["subject"], values),
                                                             "body": compose.render(t["body"], values)}, "定型文", actor(request),
                                             template_id=t["id"], template_version=t["version"],
                                             basis_json=compose.basis_for(c, deal, t, values))
                sales.create_schedule(c, deal_id, did, account_id, s, times[s][0], times[s][1], actor(request), seq)
                if s == "initial":
                    last_subject = sales.get_draft(c, did)["subject"]
            return f"{len(steps)} 回分の文面と予約を作りました。各文面を確認して承認すると「予約済み」になります"
        return await ai_then_save(f"/deals/{deal_id}", gen, save)

    # ------------------------------------------------------------------ 送信・予約
    @app.get("/schedules", response_class=HTMLResponse)
    def schedules_page(request: Request, status: str = ""):
        conn = db()
        try:
            rows = sales.list_schedules(conn, status)
            mode_ = runtime.send_mode(conn)
            unknown = [dict(r) for r in conn.execute("SELECT * FROM send_attempts WHERE result='結果不明' ORDER BY id DESC")]
        finally:
            conn.close()
        for r in rows:
            r["date_local"], r["time_local"] = split_local(r["send_at_utc"], r["input_tz"])
        return P(request, "schedules.html", rows=rows, status=status, send_mode=mode_, unknown=unknown,
                 sched=app.state.scheduler.status())

    @app.post("/schedules/{sid}/{action}")
    async def schedule_act(request: Request, sid: str, action: str):
        form = await form_checked(request)
        at = tzv = None
        if action in ("resume", "reschedule", "duplicate") and form.get("date"):
            try:
                at, tzv = parse_time(form)
            except TimeInputError as exc:
                return back("/schedules", err=str(exc))
        nxt = str(form.get("next") or "/schedules")
        return ok_or_err(nxt if nxt.startswith("/") else "/schedules",
                         lambda c: sales.schedule_action(c, sid, action, actor(request), at, tzv), "予約を更新しました")

    @app.post("/sending/stop-all")
    async def stop_all(request: Request):
        await form_checked(request)

        def fn(c):
            runtime.set_send_mode(c, "停止", actor(request))
            return "全送信を停止しました（送信モード：停止）。予約は残っていますが送られません"
        return ok_or_err("/schedules", fn)

    @app.post("/scheduler/run")
    async def scheduler_run(request: Request):
        await form_checked(request)
        r = await run_in_threadpool(app.state.scheduler.run_once)
        return back(str((await request.form()).get("next") or "/schedules"),
                    msg=f"確認しました（受信同期 {len(r['synced'])} 件・遅延で保留 {r['late']} 件・送信処理 {len(r['sent'])} 件）")

    # ------------------------------------------------------------------ 受信・対応
    @app.get("/inbox", response_class=HTMLResponse)
    def inbox_page(request: Request, all: str = "", kind: str = ""):
        conn = db()
        try:
            msgs = sales.inbox(conn, only_pending=not all, kind=kind)
            accounts = sales.list_accounts(conn)
        finally:
            conn.close()
        return P(request, "inbox.html", msgs=msgs, all=all, kind=kind, accounts=accounts)

    @app.get("/messages/{mid}", response_class=HTMLResponse)
    def message_page(request: Request, mid: str):
        conn = db()
        try:
            m = sales.get_message(conn, mid)
            if not m:
                raise HTTPException(404, "メッセージが見つかりません")
            thread = [dict(r) for r in conn.execute("SELECT * FROM messages WHERE thread_id=? ORDER BY COALESCE(sent_at, received_at)",
                                                    (m["thread_id"],))] if m["thread_id"] else []
            deals = sales.list_deals(conn, dealer_id=m["dealer_id"]) if m["dealer_id"] else []
            all_deals = sales.list_deals(conn)
            supp = sales.list_suppressions(conn)
        finally:
            conn.close()
        related_supp = [s for s in supp if s.get("message_id") == mid]
        return P(request, "mail_message.html", m=m, thread=thread, deals=deals, all_deals=all_deals, related_supp=related_supp,
                 ai_status=_ai_status())

    @app.post("/messages/{mid}/handled")
    async def message_handled(request: Request, mid: str):
        form = await form_checked(request)

        def fn(c):
            c.execute("UPDATE messages SET handled_at=?, handled_by=?, note=COALESCE(?, note) WHERE id=?",
                      (utcnow(), actor(request), str(form.get("note") or "") or None, mid))
            runtime.audit(c, actor(request), "受信を対応済みにする", mid)
            return "対応済みにしました（保留中の予約は自動では再開しません。予約の画面で確認して再開してください）"
        return ok_or_err(f"/messages/{mid}", fn)

    @app.post("/messages/{mid}/relink")
    async def message_relink(request: Request, mid: str):
        form = await form_checked(request)
        deal_id = str(form.get("deal_id", ""))

        def fn(c):
            deal = sales.get_deal(c, deal_id)
            if not deal:
                raise sales.SalesError("案件が見つかりません")
            c.execute("UPDATE messages SET deal_id=?, dealer_id=?, link_method='手動' WHERE id=?", (deal_id, deal["dealer_id"], mid))
            runtime.audit(c, actor(request), "受信の関連付けを修正", mid, deal_id)
            return "関連付けを修正しました"
        return ok_or_err(f"/messages/{mid}", fn)

    @app.post("/messages/{mid}/kind")
    async def message_kind(request: Request, mid: str):
        form = await form_checked(request)
        kind = str(form.get("kind", ""))
        if kind not in ("返信", "自動応答", "不達通知", "配信停止要求", "拒否の疑い", "未分類"):
            return back(f"/messages/{mid}", err="分類が不正です")

        def fn(c):
            c.execute("UPDATE messages SET kind=?, needs_action=? WHERE id=?", (kind, 0 if kind == "自動応答" else 1, mid))
            runtime.audit(c, actor(request), "受信の分類を人が修正", mid, kind)
            return "分類を修正しました"
        return ok_or_err(f"/messages/{mid}", fn)

    @app.post("/messages/{mid}/reply-draft")
    async def message_reply(request: Request, mid: str):
        form = await form_checked(request)

        use_ai = str(form.get("method")) == "ai"

        def gen(c):
            m = sales.get_message(c, mid)
            if not m["deal_id"]:
                raise sales.SalesError("先にこの受信を案件に関連付けてください")
            if not use_ai:
                return None
            deal = sales.get_deal(c, m["deal_id"])
            return ai.generate_draft(c, app.state.secrets, deal, "reply", str(form.get("instruction", "")),
                                     person_of(c, deal.get("person_id")))

        def fn(c, r):
            m = sales.get_message(c, mid)
            deal = sales.get_deal(c, m["deal_id"])
            acc = m["account_id"]
            values, person, _ = _new_draft_values(c, deal, "reply", acc, m["from_addr"], deal.get("person_id"), reply_to=m)
            if r is not None:
                return sales.create_draft(c, deal["id"], "reply", {"account_id": acc, "to_address": m["from_addr"],
                                                                   "subject": r["subject"], "body": r["body"]}, "AI",
                                          actor(request), ai_usage_id=r["usage_id"], summary_ja=r["summary_ja"],
                                          reply_to_message_id=mid)
            t = sales.pick_template(c, "reply", [])
            subj = m["subject"] if (m["subject"] or "").lower().startswith("re:") else "Re: " + (m["subject"] or "")
            return sales.create_draft(c, deal["id"], "reply", {"account_id": acc, "to_address": m["from_addr"], "subject": subj,
                                                               "body": compose.render(t["body"], values) if t else ""},
                                      "定型文" if t else "手動", actor(request), reply_to_message_id=mid,
                                      template_id=t["id"] if t else None, template_version=t["version"] if t else None)
        try:
            r = await run_in_threadpool(with_conn, gen)
            did = run_tx(lambda c: fn(c, r))
        except (sales.SalesError, ai.AIError) as exc:
            return back(f"/messages/{mid}", err=str(exc))
        return back(f"/drafts/{did}", msg="返信の下書きを作りました（自動では送信しません）")

    @app.post("/messages/{mid}/summary")
    async def message_summary(request: Request, mid: str):
        await form_checked(request)

        def gen(c):
            m = sales.get_message(c, mid)
            return ai.summarize_ja(c, app.state.secrets, f"Subject: {m['subject']}\n\n{m['body_text']}", m["dealer_id"])

        def save(c, summary):
            c.execute("UPDATE messages SET note=? WHERE id=?", ("【AI による日本語の要約】" + summary, mid))
            return "日本語の要約を作成しました（メモ欄に保存）"
        return await ai_then_save(f"/messages/{mid}", gen, save)

    @app.post("/sync/now")
    async def sync_now(request: Request):
        form = await form_checked(request)
        conn = db()
        try:
            accounts = [a for a in sales.list_accounts(conn) if a["enabled"]]
        finally:
            conn.close()
        def sync_all():
            return [f"{a['label']}：{app.state.sync.sync_account(a['id'])['message']}" for a in accounts
                    if a["provider"] != "smtp_imap" or a["config"].get("imap_host")]
        msgs = await run_in_threadpool(sync_all)
        nxt = str(form.get("next") or "/inbox")
        return back(nxt if nxt.startswith("/") else "/inbox", msg=" / ".join(msgs) or "受信できるアカウントがありません")

    # ------------------------------------------------------------------ 連絡拒否
    @app.get("/suppressions", response_class=HTMLResponse)
    def suppressions_page(request: Request, status: str = ""):
        conn = db()
        try:
            rows = sales.list_suppressions(conn, status)
            dealers = [dict(r) for r in conn.execute(
                "SELECT id, COALESCE(name_manual, name_auto, website_host) AS name FROM dealers ORDER BY name")]
        finally:
            conn.close()
        return P(request, "suppressions.html", rows=rows, status=status, dealers=dealers)

    @app.post("/suppressions/add")
    async def suppression_add(request: Request):
        form = await form_checked(request)
        return ok_or_err("/suppressions", lambda c: sales.add_suppression(
            c, str(form.get("scope", "")), actor(request), dealer_id=str(form.get("dealer_id") or "") or None,
            address=str(form.get("address") or "") or None, domain=str(form.get("domain") or "") or None,
            reason=str(form.get("reason") or "") or None), "送信停止を登録しました")

    @app.post("/suppressions/{sid}/{action}")
    async def suppression_act(request: Request, sid: str, action: str):
        await form_checked(request)
        label = {"confirm": "確定", "release": "解除"}.get(action)
        if not label:
            raise HTTPException(404)
        if label == "解除":
            require_admin(request)
        return ok_or_err("/suppressions", lambda c: sales.resolve_suppression(c, sid, label, actor(request)), f"{label}しました")

    # ------------------------------------------------------------------ 照合・稼働環境
    @app.get("/reconcile", response_class=HTMLResponse)
    def reconcile_page(request: Request):
        conn = db()
        try:
            unknown = [dict(r) for r in conn.execute(
                "SELECT a.*, r.subject, r.to_address FROM send_attempts a LEFT JOIN drafts r ON r.id=a.draft_id "
                "WHERE a.result IN ('結果不明','送信中') ORDER BY a.id DESC")]
            recent = [dict(r) for r in conn.execute(
                "SELECT a.*, r.subject FROM send_attempts a LEFT JOIN drafts r ON r.id=a.draft_id ORDER BY a.id DESC LIMIT 30")]
            ok, why = runtime.sending_environment(conn, paths.data_dir)
            info = {"owner": runtime.get_state(conn, "send_owner_instance"), "seq": runtime.get_state(conn, "send_seq", "0"),
                    "reason": runtime.get_state(conn, "reconcile_reason", ""), "required": runtime.get_state(conn, "reconcile_required") == "1"}
        finally:
            conn.close()
        inst = runtime.local_instance(paths.data_dir)
        return P(request, "reconcile.html", unknown=unknown, recent=recent, env_ok=ok, env_reason=why, info=info, inst=inst)

    @app.post("/attempts/{aid}/reconcile")
    async def attempt_reconcile(request: Request, aid: int):
        await form_checked(request)
        r = await run_in_threadpool(app.state.outbound.reconcile, aid, actor(request))
        return back("/reconcile", **({"msg": r["message"]} if r["result"] in ("ok", "noop") else {"err": r["message"]}))

    @app.post("/attempts/{aid}/decide")
    async def attempt_decide(request: Request, aid: int):
        form = await form_checked(request)
        r = app.state.outbound.decide(aid, str(form.get("decision", "")), actor(request))
        return back("/reconcile", **({"msg": r["message"]} if r["result"] in ("ok", "noop") else {"err": r["message"]}))

    @app.post("/reconcile/take-ownership")
    async def take_ownership(request: Request):
        form = await form_checked(request)
        require_admin(request)
        if not form.get("confirm"):
            return back("/reconcile", err="照合を終えたことを確認する欄にチェックを入れてください")

        def fn(c):
            n = c.execute("SELECT COUNT(*) FROM send_attempts WHERE result IN ('結果不明','送信中')").fetchone()[0]
            if n:
                raise sales.SalesError(f"結果不明の送信が {n} 件あります。先に照合するか、人が判断してください")
            runtime.take_ownership(c, paths.data_dir, actor(request))
            return "この環境を送信担当にしました。送信モードは「停止」のままです。必要なら設定で変更してください"
        return ok_or_err("/reconcile", fn)

    # ------------------------------------------------------------------ 自社情報・テンプレート
    @app.get("/profile", response_class=HTMLResponse)
    def profile_page(request: Request):
        conn = db()
        try:
            prof = sales.get_profile(conn)
        finally:
            conn.close()
        return P(request, "profile.html", prof=prof, fields=sales.PROFILE_FIELDS)

    @app.post("/profile")
    async def profile_save(request: Request):
        form = await form_checked(request)
        return ok_or_err("/profile", lambda c: sales.save_profile(c, {k: str(form.get(k, "")) for k, _, _ in sales.PROFILE_FIELDS},
                                                                   actor(request)), "自社情報を保存しました")

    @app.get("/templates", response_class=HTMLResponse)
    def templates_page(request: Request):
        conn = db()
        try:
            rows = sales.list_templates(conn)
        finally:
            conn.close()
        return P(request, "templates_list.html", rows=rows, var_help=compose.VAR_HELP)

    @app.get("/templates/{tid}", response_class=HTMLResponse)
    def template_page(request: Request, tid: str):
        conn = db()
        try:
            t = sales.get_template(conn, tid) if tid != "new" else None
            versions = [dict(r) for r in conn.execute("SELECT * FROM template_versions WHERE template_id=? ORDER BY version DESC",
                                                      (tid,))] if t else []
        finally:
            conn.close()
        return P(request, "template_edit.html", t=t, versions=versions, var_help=compose.VAR_HELP,
                 categories=["一般中古車", "スポーツカー", "ミニバン・SUV", "クラシックカー", "共通"])

    @app.post("/templates/save")
    async def template_save(request: Request):
        form = await form_checked(request)
        tid = str(form.get("id", "")) or None
        try:
            new = run_tx(lambda c: sales.save_template(c, tid, str(form.get("name", "")), str(form.get("step", "")),
                                                       str(form.get("category", "")), str(form.get("subject", "")),
                                                       str(form.get("body", "")), actor(request)))
        except sales.SalesError as exc:
            return back(f"/templates/{tid or 'new'}", err=str(exc))
        return back(f"/templates/{new}", msg="テンプレートを保存しました（既存の下書きには影響しません）")

    # ------------------------------------------------------------------ 設定：メール接続・送信モード・AI
    @app.get("/settings/mail", response_class=HTMLResponse)
    def mail_settings(request: Request):
        conn = db()
        try:
            accounts = sales.list_accounts(conn)
            opts = runtime.options(conn)
            mode_ = runtime.send_mode(conn)
        finally:
            conn.close()
        for a in accounts:
            a["has_password"] = app.state.secrets.has(f"{a['id']}:password")
            a["has_token"] = app.state.secrets.has(f"{a['id']}:refresh_token")
        return P(request, "mail_settings.html", accounts=accounts, opts=opts, send_mode=mode_, ai_status=_ai_status(),
                 secret_backend=app.state.secrets.backend_name, port=app.state.port)

    @app.post("/settings/send-mode")
    async def send_mode_set(request: Request):
        form = await form_checked(request)
        require_admin(request)
        mode_ = str(form.get("send_mode", ""))
        if mode_ == "実送信" and not form.get("confirm_live"):
            return back("/settings/mail", err="実送信にするには、注意事項を確認する欄にチェックを入れてください")

        def fn(c):
            if mode_ != "停止":
                ok, why = runtime.sending_environment(c, paths.data_dir)
                if not ok:
                    raise sales.SalesError(why)
            runtime.set_send_mode(c, mode_, actor(request))
            return f"送信モードを「{mode_}」にしました"
        return ok_or_err("/settings/mail", fn)

    @app.post("/settings/options")
    async def options_set(request: Request):
        form = await form_checked(request)
        require_admin(request)

        def fn(c):
            for k in ("display_tz", "test_recipient", "late_grace_minutes", "sync_interval_minutes", "sync_fresh_minutes",
                      "public_base_url"):
                if k in form:
                    v = str(form.get(k, "")).strip()
                    if k == "display_tz" and v not in TZ_CHOICES:
                        raise sales.SalesError("表示のタイムゾーンが不正です")
                    if k == "test_recipient" and v:
                        sales.validate_addresses(v)
                    if k in ("late_grace_minutes", "sync_interval_minutes", "sync_fresh_minutes"):
                        n = int(v or 0)
                        lo, hi = {"late_grace_minutes": (1, 240), "sync_interval_minutes": (1, 120), "sync_fresh_minutes": (5, 1440)}[k]
                        v = str(min(hi, max(lo, n)))
                    if k == "public_base_url" and v and not v.startswith("https://"):
                        raise sales.SalesError("配信停止リンク用のアドレスは https:// で始まる、外部から開けるアドレスにしてください")
                    runtime.set_option(c, k, v)
            runtime.audit(c, actor(request), "送信・受信の設定を変更", "options")
            return "設定を保存しました"
        return ok_or_err("/settings/mail", fn)

    @app.get("/accounts/{aid}", response_class=HTMLResponse)
    def account_page(request: Request, aid: str, provider: str = "test"):
        require_admin(request)
        conn = db()
        try:
            a = sales.get_account(conn, aid) if aid != "new" else None
        finally:
            conn.close()
        prov = a["provider"] if a else provider
        if prov not in sales.PROVIDERS:
            raise HTTPException(404)
        cfg = {**(SMTP_DEFAULTS if prov == "smtp_imap" else {}), **((a or {}).get("config") or {})}
        return P(request, "account_edit.html", a=a, provider=prov, cfg=cfg, presets=SMTP_PRESETS,
                 has_password=bool(a) and app.state.secrets.has(f"{a['id']}:password"),
                 has_token=bool(a) and app.state.secrets.has(f"{a['id']}:refresh_token"),
                 has_client_secret=bool(a) and app.state.secrets.has(f"{a['id']}:client_secret"),
                 port=app.state.port,
                 redirect=(oauth_mod.redirect_uri({"provider": prov, "config": cfg}, app.state.port or 8765)
                           if prov in ("gmail", "graph") else None))

    @app.post("/accounts/save")
    async def account_save(request: Request):
        form = await form_checked(request)
        require_admin(request)
        aid = str(form.get("id", "")) or None
        provider = str(form.get("provider", ""))
        cfg: dict = {}
        if provider == "test":
            cfg["simulate"] = str(form.get("simulate") or "正常")
        elif provider == "smtp_imap":
            preset = SMTP_PRESETS.get(str(form.get("preset", "")), {})
            for k, default in SMTP_DEFAULTS.items():
                raw = form.get(k)
                if k == "save_to_sent":
                    cfg[k] = bool(form.get(k))
                elif raw not in (None, ""):
                    cfg[k] = int(raw) if isinstance(default, int) and str(raw).isdigit() else str(raw)
                else:
                    cfg[k] = preset.get(k, default)
            if preset:
                cfg.update({k: v for k, v in preset.items() if not form.get(k)})
        else:
            cfg["client_id"] = str(form.get("client_id", "")).strip()
            cfg["sync_days"] = int(str(form.get("sync_days") or 14))
            if provider == "graph":
                cfg["tenant"] = str(form.get("tenant") or "common")
        try:
            new = run_tx(lambda c: sales.save_account(c, aid, provider, str(form.get("label") or sales.PROVIDERS[provider]),
                                                      str(form.get("from_address", "")), str(form.get("from_name", "")), cfg,
                                                      str(form.get("signature", "")), int(str(form.get("daily_limit") or 30)),
                                                      actor(request)))
        except (sales.SalesError, ValueError) as exc:
            return back(f"/accounts/{aid or 'new'}?provider={provider}", err=str(exc))
        if form.get("password"):
            app.state.secrets.set(f"{new}:password", str(form.get("password")))
        if form.get("client_secret"):
            app.state.secrets.set(f"{new}:client_secret", str(form.get("client_secret")).strip())
        return back(f"/accounts/{new}", msg="保存しました（パスワード等は OS の資格情報保管に保存し、DB・バックアップには入れません）")

    @app.post("/accounts/{aid}/check")
    async def account_check(request: Request, aid: str):
        await form_checked(request)
        require_admin(request)
        conn = db()
        try:
            a = sales.get_account(conn, aid)
        finally:
            conn.close()
        try:
            note = await run_in_threadpool(lambda: app.state.outbound.provider(a).check())
        except MailError as exc:
            run_tx(lambda c: sales.set_account_status(c, aid, "認証エラー" if exc.__class__.__name__ == "AuthError" else "接続エラー", str(exc)))
            return back(f"/accounts/{aid}", err=f"接続を確認できませんでした：{exc}")
        run_tx(lambda c: sales.set_account_status(c, aid, "接続確認済み", note))
        return back(f"/accounts/{aid}", msg=note)

    @app.post("/accounts/{aid}/sync")
    async def account_sync(request: Request, aid: str):
        await form_checked(request)
        r = await run_in_threadpool(app.state.sync.sync_account, aid)
        return back(f"/accounts/{aid}" if (getattr(request.state, "user", None) or {}).get("role") == "admin" else "/inbox",
                    **({"msg": r["message"]} if r["ok"] else {"err": r["message"]}))

    @app.post("/accounts/{aid}/toggle")
    async def account_toggle(request: Request, aid: str):
        await form_checked(request)
        require_admin(request)

        def fn(c):
            c.execute("UPDATE mail_accounts SET enabled=1-enabled, updated_at=? WHERE id=?", (utcnow(), aid))
            runtime.audit(c, actor(request), "メール接続の有効・無効を切替", aid)
            return "切り替えました"
        return ok_or_err(f"/accounts/{aid}", fn)

    @app.post("/accounts/{aid}/disconnect")
    async def account_disconnect(request: Request, aid: str):
        await form_checked(request)
        require_admin(request)
        app.state.secrets.delete_prefix(aid)
        oauth_mod.TokenCache.drop(aid)

        def fn(c):
            sales.set_account_status(c, aid, "切断", "利用者が切断（保存した認証情報を削除）")
            c.execute("UPDATE schedules SET status='保留', hold_reason='送信元アカウントが切断されました', updated_at=? "
                      "WHERE account_id=? AND status IN ('予約済み','承認待ち')", (utcnow(), aid))
            runtime.audit(c, actor(request), "メール接続を切断", aid)
            return "切断しました（認証情報を削除し、このアカウントの予約は保留にしました）"
        return ok_or_err(f"/accounts/{aid}", fn)

    @app.get("/accounts/{aid}/oauth/start")
    def oauth_start(request: Request, aid: str):
        require_admin(request)
        conn = db()
        try:
            a = sales.get_account(conn, aid)
        finally:
            conn.close()
        if not a or a["provider"] not in ("gmail", "graph"):
            raise HTTPException(404)
        if not app.state.port:
            return back(f"/accounts/{aid}", err="接続先のポートが分かりません。アプリを通常の方法で起動してから操作してください")
        state, challenge = app.state.oauth_pending.add(aid)
        try:
            url = oauth_mod.authorization_url(a, app.state.port, state, challenge)
        except MailError as exc:
            return back(f"/accounts/{aid}", err=str(exc))
        return RedirectResponse(url, status_code=303)

    @app.get("/oauth/callback", response_class=HTMLResponse)
    def oauth_callback(request: Request, state: str = "", code: str = "", error: str = "", error_description: str = ""):
        pending = app.state.oauth_pending.pop(state) if state else None
        if not pending:
            return back("/settings/mail", err="認証の手続きが見つからないか期限切れです。もう一度「接続する」を押してください")
        aid, verifier = pending
        if error or not code:
            return back(f"/accounts/{aid}", err=f"認証が完了しませんでした：{error_description or error or 'コードがありません'}")
        conn = db()
        try:
            a = sales.get_account(conn, aid)
        finally:
            conn.close()
        try:
            oauth_mod.exchange_code(a, app.state.secrets, code, verifier, app.state.port)
            note = app.state.outbound.provider(a).check()
        except MailError as exc:
            run_tx(lambda c: sales.set_account_status(c, aid, "認証エラー", str(exc)))
            return back(f"/accounts/{aid}", err=str(exc))
        run_tx(lambda c: sales.set_account_status(c, aid, "接続確認済み", note))
        run_tx(lambda c: runtime.audit(c, "利用者", "メール接続を認証", aid))
        return back(f"/accounts/{aid}", msg=note)

    @app.post("/settings/ai")
    async def ai_settings(request: Request):
        form = await form_checked(request)
        require_admin(request)

        def fn(c):
            runtime.set_option(c, "ai_enabled", "1" if form.get("ai_enabled") else "0")
            if str(form.get("ai_model")) in ai.MODELS:
                runtime.set_option(c, "ai_model", str(form.get("ai_model")))
            if str(form.get("ai_effort")) in ai.EFFORTS:
                runtime.set_option(c, "ai_effort", str(form.get("ai_effort")))
            runtime.set_option(c, "ai_fallbacks", "1" if form.get("ai_fallbacks") else "0")
            for k in ("ai_monthly_request_cap", "ai_monthly_token_cap"):
                runtime.set_option(c, k, str(max(0, int(str(form.get(k) or 0)))))
            runtime.audit(c, actor(request), "AI の設定を変更", "ai")
            return "AI の設定を保存しました"
        if form.get("api_key"):
            app.state.secrets.set(ai.SECRET_KEY, str(form.get("api_key")).strip())
        if form.get("clear_key"):
            app.state.secrets.delete(ai.SECRET_KEY)
        return ok_or_err("/settings/mail", fn)

    @app.post("/settings/ai-test")
    async def ai_test(request: Request):
        await form_checked(request)
        require_admin(request)
        return await ai_then_save("/settings/mail", lambda c: ai.check_key(c, app.state.secrets),
                                  lambda c, summary: "AI に接続できました：" + summary)

    # ------------------------------------------------------------------ テスト用受信箱
    @app.get("/testbox", response_class=HTMLResponse)
    def testbox_page(request: Request):
        conn = db()
        try:
            accounts = [a for a in sales.list_accounts(conn) if a["provider"] == "test"]
            rows = [dict(r) for r in conn.execute(
                "SELECT t.id, t.account_id, t.folder, t.message_id_header, t.created_at, substr(t.raw, 1, 4000) AS head "
                "FROM test_mailbox t ORDER BY t.id DESC LIMIT 60")]
        finally:
            conn.close()
        import re as _re
        for r in rows:
            m = _re.search(r"(?im)^Subject:\s*(.*)$", r["head"] or "")
            to = _re.search(r"(?im)^To:\s*(.*)$", r["head"] or "")
            fr = _re.search(r"(?im)^From:\s*(.*)$", r["head"] or "")
            r["subject"], r["to"], r["from"] = (m.group(1) if m else ""), (to.group(1) if to else ""), (fr.group(1) if fr else "")
        return P(request, "testbox.html", accounts=accounts, rows=rows)

    @app.post("/testbox/inject")
    async def testbox_inject(request: Request):
        form = await form_checked(request)
        kind = str(form.get("kind", ""))
        sent_id = str(form.get("sent_id", ""))

        def fn(c):
            row = c.execute("SELECT * FROM test_mailbox WHERE id=? AND folder='sent'", (sent_id,)).fetchone() if sent_id else None
            account_id = row["account_id"] if row else str(form.get("account_id", ""))
            acc = sales.get_account(c, account_id)
            if not acc or acc["provider"] != "test":
                raise sales.SalesError("テスト用接続先のアカウントではありません")
            recipient = None
            if row:
                import re as _re
                to = _re.search(r"(?im)^To:\s*(.*)$", row["raw"])
                recipient = (to.group(1).split(",")[0].strip() if to else "")
                recipient = recipient.split("<")[-1].rstrip(">").strip()
            testbox.inject(c, acc, kind, row["raw"] if row else None, recipient, str(form.get("body") or "") or None)
            return f"テスト用の受信（{testbox.INJECT_KINDS.get(kind, kind)}）を入れました。「受信を確認」で取り込まれます"
        return ok_or_err("/testbox", fn)

    # ------------------------------------------------------------------ 利用者（共有モード）
    @app.get("/users", response_class=HTMLResponse)
    def users_page(request: Request):
        require_admin(request)
        conn = db()
        try:
            users = [dict(r) for r in conn.execute("SELECT id, username, role, disabled, created_at FROM users ORDER BY id")]
        finally:
            conn.close()
        return P(request, "users.html", users=users, shared=(mode == "shared"))

    @app.post("/users/create")
    async def users_create(request: Request):
        form = await form_checked(request)
        require_admin(request)
        pw = str(form.get("password", ""))
        if pw != str(form.get("password2", "")):
            return back("/users", err="確認用のパスワードが一致しません")

        def fn(c):
            auth.create_user(c, str(form.get("username", "")), pw, str(form.get("role") or "member"))
            runtime.audit(c, actor(request), "利用者を作成・更新", str(form.get("username", "")))
            return "利用者を作成（または更新）しました"
        return ok_or_err("/users", fn)

    @app.post("/users/{uid}/{action}")
    async def users_action(request: Request, uid: int, action: str):
        await form_checked(request)
        require_admin(request)

        def fn(c):
            row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if not row:
                raise sales.SalesError("利用者が見つかりません")
            if row["username"] == actor(request) and action in ("disable", "member"):
                raise sales.SalesError("自分自身の権限は変更できません")
            if action == "disable":
                c.execute("UPDATE users SET disabled=1 WHERE id=?", (uid,))
                c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            elif action == "enable":
                c.execute("UPDATE users SET disabled=0 WHERE id=?", (uid,))
            elif action in ("admin", "member"):
                c.execute("UPDATE users SET role=? WHERE id=?", (action, uid))
            else:
                raise sales.SalesError("不明な操作です")
            runtime.audit(c, actor(request), f"利用者を変更（{action}）", row["username"])
            return "変更しました"
        return ok_or_err("/users", fn)

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request):
        conn = db()
        try:
            rows = [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 300")]
        finally:
            conn.close()
        return P(request, "audit.html", rows=rows)

    # ------------------------------------------------------------------ 配信停止の受け口（ログイン不要・最小限）
    @app.get("/u/{token}", response_class=HTMLResponse)
    def unsubscribe_page(request: Request, token: str):
        return templates.TemplateResponse(request, "unsubscribe.html", {"request": request, "token": token, "done": False,
                                                                        "user": None, "csrf": ""})

    @app.post("/u/{token}", response_class=HTMLResponse)
    async def unsubscribe_post(request: Request, token: str):
        th = hashlib.sha256(token.encode()).hexdigest()

        def fn(c):
            row = c.execute("SELECT * FROM unsubscribe_tokens WHERE token_hash=?", (th,)).fetchone()
            if not row:
                return False
            sales.add_suppression(c, "アドレス", "配信停止リンク", source="停止リンク", status="確定", dealer_id=row["dealer_id"],
                                  address=row["address"], reason="配信停止リンクから依頼")
            c.execute("UPDATE unsubscribe_tokens SET used_at=? WHERE token_hash=?", (utcnow(), th))
            return True
        done = run_tx(fn)
        return templates.TemplateResponse(request, "unsubscribe.html", {"request": request, "token": token, "done": done,
                                                                        "invalid": not done, "user": None, "csrf": ""})


def query(**kw) -> str:
    return urlencode({k: v for k, v in kw.items() if v})
