import os, re
import sqlite3
from functools import wraps
from typing import Optional, Tuple, List
from pathlib import Path
from flask import request, redirect, url_for, session, flash, abort
from werkzeug.utils import secure_filename
from flask import request
from flask import app, url_for


from flask import Flask, render_template, request, redirect, url_for, session, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash

import db as dbx

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

POSTER_DIR = STATIC_DIR / "posters"
PFP_DIR = STATIC_DIR / "pfps"

POSTER_DIR.mkdir(parents=True, exist_ok=True)
PFP_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_UPLOAD_MB = 5

APP_SECRET = os.getenv("APP_SECRET", "dev-secret-change-me")
DB_PATH = os.getenv("DB_PATH", os.path.join(os.getcwd(), "site.db"))

def get_conn():
    # timeout helps with brief lock contention
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")  # better concurrency than default
    return conn

import os, re
from flask import url_for

def slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"&", "and", s)
    s = re.sub(r"[^a-z0-9]+", "_", s)   # spaces/punct -> _
    s = re.sub(r"_+", "_", s).strip("_")
    return s

def static_file_exists(rel_path: str) -> bool:
    # rel_path like "posters/blue_devils2014.png"
    abs_path = os.path.join(app.root_path, "static", rel_path)
    return os.path.isfile(abs_path)

def pfp_filename_for_username(username: str, ext: str) -> str:
    ext = ext.lower()
    if ext == ".jpeg":
        ext = ".jpg"
    key = slugify(username)
    return f"pfp_{key}{ext}"


def pfp_url_for_username(username: str) -> str | None:
    key = slugify(username)
    base = f"pfps/pfp_{key}"
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        rel = base + ext                          # rel like "pfps/pfp_will.png"
        abs_path = Path(app.root_path) / "static" / rel  # correct absolute path
        if abs_path.is_file():
            return url_for("static", filename=rel)
    return None


def poster_url_for_show(corps: str, year: int) -> str | None:
    key = slugify(corps)
    base = f"posters/{key}{int(year)}"
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        rel = base + ext                          # rel like "posters/blue_devils2014.webp"
        abs_path = Path(app.root_path) / "static" / rel  # correct absolute path
        if abs_path.is_file():
            return url_for("static", filename=rel)
    return None

def poster_filename_for_show(corps: str, year: int, ext: str) -> str:
    ext = ext.lower()
    if ext == ".jpeg":
        ext = ".jpg"
    key = slugify(corps)
    return f"{key}{int(year)}{ext}"

def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = APP_SECRET

    # Ensure DB exists
    if not os.path.exists(DB_PATH):
        dbx.init_db(DB_PATH, schema_path=os.path.join(os.path.dirname(__file__), "schema.sql"))

    def get_conn():
        conn = dbx.connect(DB_PATH)
        dbx.ensure_reviews_table(conn)
        dbx.ensure_profile_columns(conn)
        dbx.ensure_roles_tables(conn)
        dbx.ensure_review_votes_table(conn)  # NEW
        dbx.ensure_social_tables(conn)       # NEW
        return conn

    def current_user():
        uid = session.get("uid")
        if not uid:
            return None
        conn = get_conn()
        try:
            return dbx.user_by_id(conn, int(uid))
        finally:
            conn.close()



    def login_required(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("uid"):
                flash("Please log in to do that.", "warn")
                return redirect(url_for("login", next=request.path))
            return fn(*args, **kwargs)
        return wrapper

    def admin_required(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            u = current_user()
            if not u or int(u["is_admin"]) != 1:
                abort(403)
            return fn(*args, **kwargs)
        return wrapper

    @app.context_processor
    def inject_globals():
        u = current_user()
        return {"me": u}

    @app.get("/")
    def home():
        return redirect(url_for("shows"))

    # =========================================================
# STEP 4A — app.py — PATCH posters in shows listing
# =========================================================

    @app.get("/shows")
    def shows():
        sort = (request.args.get("sort") or "year_desc").strip()
        year_filter = (request.args.get("year") or "").strip()
        corps = (request.args.get("corps") or "").strip()

        conn = None
        try:
            conn = get_conn()

            # Your existing DB query here that returns show rows
            # Example:
            rows = dbx.list_shows(conn, sort=sort, year=year_filter, corps=corps)

            # PATCH poster_url from filesystem when missing
            rows2 = []
            for r in rows:
                d = dict(r)
                if not d.get("poster_url"):
                    d["poster_url"] = poster_url_for_show(d.get("corps", ""), d.get("year") or 0)
                rows2.append(d)

        finally:
            if conn is not None:
                conn.close()

        return render_template("shows.html", rows=rows2, sort=sort, year=year_filter, corps=corps)

    # =========================================================
# STEP 4B — app.py — PATCH poster in show detail
# =========================================================
    @app.get("/show/<int:show_id>")
    def show_detail(show_id: int):
        viewer_uid = session.get("uid")

        conn = None
        try:
            conn = get_conn()

            row = dbx.get_show_by_id(conn, show_id)
            if not row:
                abort(404)

            row = dict(row)

            # ✅ Ensure show page always has a poster_url (DB value OR filesystem fallback)
            if not row.get("poster_url"):
                row["poster_url"] = poster_url_for_show(row.get("corps", ""), row.get("year") or 0)

            me = dbx.user_by_id(conn, viewer_uid) if viewer_uid else None
            my_rating = dbx.user_rating_for_show(conn, show_id, viewer_uid) if viewer_uid else None
            reviews = dbx.list_reviews_for_show(conn, show_id, viewer_user_id=viewer_uid, limit=30)

            # =========================================================
            # STEP 5 — PATCH avatar_url for me + reviews  ✅ PUT IT HERE
            # =========================================================
            if me:
                me = dict(me)
                if not me.get("avatar_url"):
                    me["avatar_url"] = pfp_url_for_username(me.get("username", ""))

            reviews2 = []
            for rv in reviews or []:
                d = dict(rv)
                if not d.get("avatar_url"):
                    d["avatar_url"] = pfp_url_for_username(d.get("username", ""))
                reviews2.append(d)
            reviews = reviews2
            # ===================== END STEP 5 PATCH ===================

        finally:
            if conn is not None:
                conn.close()

        return render_template(
            "show_detail.html",
            row=row,
            me=me,
            my_rating=my_rating,
            reviews=reviews,
        )

    @app.post("/me/upload_pfp")
    @login_required
    def upload_my_pfp():
        uid = int(session.get("uid"))
        file = request.files.get("pfp_file")
        if not file or not file.filename:
            flash("No file selected.", "error")
            return redirect(url_for("me_profile"))

        filename = secure_filename(file.filename)
        ext = os.path.splitext(filename)[1].lower()

        if ext not in ALLOWED_IMAGE_EXTS:
            flash("Profile picture must be PNG, JPG, JPEG, or WEBP.", "error")
            return redirect(url_for("me_profile"))

        conn = None
        try:
            conn = get_conn()
            me = dbx.user_by_id(conn, uid)  # use your actual function name
            if not me:
                abort(404)
            me = dict(me)

            out_name = pfp_filename_for_username(me.get("username", ""), ext)
            out_path = PFP_DIR / out_name

            file.save(out_path)

            avatar_url = url_for("static", filename=f"pfps/{out_name}")
            dbx.update_user_avatar_url(conn, uid, avatar_url)
            conn.commit()

            flash("Profile picture updated.", "ok")
        finally:
            if conn is not None:
                conn.close()

        return redirect(url_for("me_profile"))


    @app.post("/show/<int:show_id>/rate")
    @login_required
    def rate_show(show_id: int):
        rating_half = (request.form.get("rating_half") or "").strip()

        if not rating_half.isdigit():
            flash("Rating must be in half-star steps.", "error")
            return redirect(url_for("show_detail", show_id=show_id))

        rh = int(rating_half)
        if rh < 0 or rh > 10:
            flash("Rating must be between 0 and 5 stars (half steps).", "error")
            return redirect(url_for("show_detail", show_id=show_id))

        conn = get_conn()
        try:
            if not dbx.get_show_by_id(conn, show_id):
                abort(404)
            dbx.upsert_rating(conn, show_id, int(session["uid"]), rh)
        finally:
            conn.close()

        flash("Saved your rating.", "ok")
        return redirect(url_for("show_detail", show_id=show_id))

    @app.post("/show/<int:show_id>/unrate")
    @login_required
    def unrate_show(show_id: int):
        conn = get_conn()
        try:
            dbx.delete_rating(conn, show_id, int(session["uid"]))
        finally:
            conn.close()
        flash("Removed your rating.", "ok")
        return redirect(url_for("show_detail", show_id=show_id))

    @app.get("/register")
    def register():
        return render_template("auth_register.html")

    @app.post("/register")
    def register_post():
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()

        if len(username) < 3:
            flash("Username must be at least 3 characters.", "error")
            return redirect(url_for("register"))
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return redirect(url_for("register"))

        conn = get_conn()
        try:
            if dbx.user_by_username(conn, username):
                flash("That username is taken.", "error")
                return redirect(url_for("register"))

            make_admin = dbx.is_first_user(conn)  # first account becomes admin
            uid = dbx.create_user(conn, username, generate_password_hash(password), make_admin=make_admin)
        finally:
            conn.close()

        session["uid"] = uid
        flash("Account created. You're logged in.", "ok")
        if make_admin:
            flash("First user created: you are admin (can mass add shows).", "ok")
        return redirect(url_for("shows"))

    @app.get("/login")
    def login():
        return render_template("auth_login.html", next=request.args.get("next"))

    @app.post("/login")
    def login_post():
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        nxt = (request.form.get("next") or "").strip()

        conn = get_conn()
        try:
            u = dbx.user_by_username(conn, username)
        finally:
            conn.close()

        if not u or not check_password_hash(u["pass_hash"], password):
            flash("Invalid username/password.", "error")
            return redirect(url_for("login"))

        session["uid"] = int(u["id"])
        flash("Logged in.", "ok")
        return redirect(nxt or url_for("shows"))

    @app.get("/logout")
    def logout():
        session.pop("uid", None)
        flash("Logged out.", "ok")
        return redirect(url_for("shows"))

    @app.get("/admin/bulk_add")
    @admin_required
    def bulk_add():
        return render_template("bulk_add.html")

    def parse_bulk_lines(text: str):
        """
        Accepts lines like:
        2017, Blue Devils, Metamorph
        2016|Carolina Crown|Relentless
        2017, Blue Devils, Metamorph, https://.../image.jpg
        2017|Blue Devils|Metamorph|https://.../image.jpg

        Fields:
        year, corps, title, [optional poster_url]
        """
        items = []
        errors = []

        for i, raw in enumerate((text or "").splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            if "|" in line:
                parts = [p.strip() for p in line.split("|")]
            else:
                parts = [p.strip() for p in line.split(",")]

            if len(parts) not in (3, 4):
                errors.append(f"Line {i}: expected 3 or 4 fields (year, corps, title, [poster_url]). Got: {raw}")
                continue

            y, c, t = parts[0], parts[1], parts[2]
            poster = parts[3] if len(parts) == 4 else None

            if not y.isdigit():
                errors.append(f"Line {i}: year must be a number. Got: {y}")
                continue

            items.append((int(y), c, t, poster))

        return items, errors

    @app.post("/admin/bulk_add")
    @admin_required
    def bulk_add_post():
        text = request.form.get("bulk_text") or ""
        items, errors = parse_bulk_lines(text)
        if errors:
            for e in errors[:10]:
                flash(e, "error")
            if len(errors) > 10:
                flash(f"...and {len(errors)-10} more errors.", "error")
            return redirect(url_for("bulk_add"))

        conn = get_conn()
        inserted = 0
        dupes = 0
        other_bad = 0
        try:
            for (y, c, t, poster) in items:
                ok, _sid, msg = dbx.add_show(conn, y, c, t, poster_url=poster)
                if ok:
                    inserted += 1
                else:
                    if msg == "Duplicate":
                        dupes += 1
                    else:
                        other_bad += 1
        finally:
            conn.close()

        flash(f"Bulk add done. Inserted: {inserted}. Duplicates skipped: {dupes}. Other issues: {other_bad}.", "ok")
        return redirect(url_for("shows"))


    @app.get("/admin/show/<int:show_id>/edit")
    @admin_required
    def admin_edit_show(show_id: int):
        conn = get_conn()
        try:
            s = dbx.get_show_by_id(conn, show_id)
            if not s:
                abort(404)
            s = dict(s)
        finally:
            conn.close()

        return render_template("admin_edit_show.html", s=s)


    @app.post("/admin/show/<int:show_id>/edit")
    @admin_required
    def admin_edit_show_post(show_id: int):
        title = (request.form.get("title") or "").strip()
        corps = (request.form.get("corps") or "").strip()
        year  = (request.form.get("year") or "").strip()
        poster_url = (request.form.get("poster_url") or "").strip()

        if not year.isdigit():
            flash("Year must be a number.", "error")
            return redirect(url_for("admin_edit_show", show_id=show_id))

        conn = get_conn()
        try:
            if not dbx.get_show_by_id(conn, show_id):
                abort(404)
            try:
                dbx.update_show(conn, show_id, int(year), corps, title, poster_url=poster_url)
            except ValueError as e:
                flash(str(e), "error")
                return redirect(url_for("admin_edit_show", show_id=show_id))
        finally:
            conn.close()

        flash("Show updated.", "ok")
        return redirect(url_for("show_detail", show_id=show_id))
    
    @app.post("/admin/show/<int:show_id>/upload_poster")
    @admin_required
    def admin_upload_show_poster(show_id: int):
        file = request.files.get("poster_file")
        if not file or not file.filename:
            flash("No file selected.", "error")
            return redirect(url_for("admin_edit_show", show_id=show_id))

        filename = secure_filename(file.filename)
        ext = os.path.splitext(filename)[1].lower()

        if ext not in ALLOWED_IMAGE_EXTS:
            flash("Poster must be PNG, JPG, JPEG, or WEBP.", "error")
            return redirect(url_for("admin_edit_show", show_id=show_id))

        conn = None
        try:
            conn = get_conn()
            row = dbx.get_show_by_id(conn, show_id)
            if not row:
                abort(404)
            row = dict(row)

            corps = row.get("corps", "")
            year = row.get("year") or 0
            out_name = poster_filename_for_show(corps, year, ext)
            out_path = POSTER_DIR / out_name

            # Save file
            file.save(out_path)

            # Save poster_url as a local static URL (this will work in templates)
            poster_url = url_for("static", filename=f"posters/{out_name}")

            # Update DB (use your actual db function name)
            dbx.update_show_poster_url(conn, show_id, poster_url)
            conn.commit()

            flash("Poster uploaded.", "ok")
        finally:
            if conn is not None:
                conn.close()

        return redirect(url_for("admin_edit_show", show_id=show_id))


    @app.post("/show/<int:show_id>/review")
    @login_required
    def review_show(show_id: int):
        text = (request.form.get("review_text") or "").strip()

        conn = None
        try:
            conn = get_conn()
            if not dbx.show_detail(conn, show_id):
                abort(404)
            try:
                dbx.upsert_review(conn, show_id, int(session["uid"]), text)
            except ValueError as e:
                flash(str(e), "error")
                return redirect(url_for("show_detail", show_id=show_id))
        finally:
            if conn is not None:
                conn.close()

        flash("Saved your review.", "ok")
        return redirect(url_for("show_detail", show_id=show_id))


    @app.post("/show/<int:show_id>/review/delete")
    @login_required
    def delete_my_review(show_id: int):
        conn = None
        try:
            conn = get_conn()
            dbx.delete_review(conn, show_id, int(session["uid"]))
        finally:
            if conn is not None:
                conn.close()

        flash("Deleted your review.", "ok")
        return redirect(url_for("show_detail", show_id=show_id))

    @app.get("/admin/roles")
    @admin_required
    def admin_roles():
        conn = None
        try:
            conn = get_conn()
            roles = dbx.list_roles(conn)
        finally:
            if conn is not None:
                conn.close()
        return render_template("admin_roles.html", roles=roles)


    @app.post("/admin/roles")
    @admin_required
    def admin_roles_post():
        name = (request.form.get("name") or "").strip()
        color = (request.form.get("color") or "").strip()

        conn = None
        try:
            conn = get_conn()
            try:
                dbx.create_role(conn, name, color)
            except ValueError as e:
                flash(str(e), "error")
                return redirect(url_for("admin_roles"))
            except sqlite3.IntegrityError:
                flash("Role name already exists.", "error")
                return redirect(url_for("admin_roles"))
        finally:
            if conn is not None:
                conn.close()

        flash("Role created.", "ok")
        return redirect(url_for("admin_roles"))


    @app.post("/admin/roles/<int:role_id>/delete")
    @admin_required
    def admin_delete_role(role_id: int):
        conn = None
        try:
            conn = get_conn()
            dbx.delete_role(conn, role_id)
        finally:
            if conn is not None:
                conn.close()
        flash("Role deleted.", "ok")
        return redirect(url_for("admin_roles"))


    @app.get("/admin/user/<username>/roles")
    @admin_required
    def admin_user_roles(username: str):
        conn = None
        try:
            conn = get_conn()
            u = dbx.user_by_username(conn, username)
            if not u:
                abort(404)

            roles = dbx.list_roles(conn)
            user_roles = dbx.roles_for_user(conn, int(u["id"]))
            user_role_ids = {int(r["id"]) for r in user_roles}

        finally:
            if conn is not None:
                conn.close()

        return render_template(
            "admin_user_roles.html",
            u=u,
            roles=roles,
            user_roles=user_roles,
            user_role_ids=user_role_ids,
        )



    @app.post("/admin/user/<username>/roles/assign")
    @admin_required
    def admin_user_roles_assign(username: str):
        role_id = int(request.form.get("role_id") or "0")
        conn = None
        try:
            conn = get_conn()
            u = dbx.user_by_username(conn, username)
            if not u:
                abort(404)
            dbx.assign_role_to_user(conn, int(u["id"]), role_id, assigned_by_user_id=int(session["uid"]))
        finally:
            if conn is not None:
                conn.close()
        flash("Role assigned.", "ok")
        return redirect(url_for("admin_user_roles", username=username))


    @app.post("/admin/user/<username>/roles/remove")
    @admin_required
    def admin_user_roles_remove(username: str):
        role_id = int(request.form.get("role_id") or "0")
        conn = None
        try:
            conn = get_conn()
            u = dbx.user_by_username(conn, username)
            if not u:
                abort(404)
            dbx.remove_role_from_user(conn, int(u["id"]), role_id)
        finally:
            if conn is not None:
                conn.close()
        flash("Role removed.", "ok")
        return redirect(url_for("admin_user_roles", username=username))





    @app.get("/me")
    @login_required
    def me_profile():
        conn = None
        u = None
        recent_ratings = []
        recent_reviews = []
        roles_for_me = []

        try:
            conn = get_conn()
            uid = int(session["uid"])

            u = dbx.user_by_id(conn, uid)
            if not u:
                abort(404)
            u = dict(u)

            avg, cnt = dbx.user_avg_rating(conn, uid)
            u["avg_rating"] = avg
            u["avg_rating_cnt"] = cnt

            viewer_id = int(session["uid"]) if session.get("uid") else None

            u["followers_count"] = dbx.follower_count(conn, int(u["id"]))
            u["following_count"] = dbx.following_count(conn, int(u["id"]))

            if viewer_id:
                u["viewer_is_following"] = dbx.is_following(conn, viewer_id, int(u["id"]))
                u["viewer_friend_status"] = dbx.friends_status(conn, viewer_id, int(u["id"]))
            else:
                u["viewer_is_following"] = False
                u["viewer_friend_status"] = "none"

            # ✅ roles list for this user
            roles_for_me = [dict(x) for x in (dbx.roles_for_user(conn, uid) or [])]

            recent_ratings = [dict(x) for x in dbx.list_recent_ratings_for_user(conn, uid, limit=20)]
            recent_reviews = [dict(x) for x in dbx.list_recent_reviews_for_user(conn, uid, limit=20)]

            for rr in recent_ratings:
                if not rr.get("poster_url"):
                    rr["poster_url"] = poster_url_for_show(rr.get("corps", ""), rr.get("year") or 0)

            for rv in recent_reviews:
                if not rv.get("poster_url"):
                    rv["poster_url"] = poster_url_for_show(rv.get("corps", ""), rv.get("year") or 0)

            if not u.get("avatar_url"):
                u["avatar_url"] = pfp_url_for_username(u.get("username", ""))

        finally:
            if conn is not None:
                conn.close()

        return render_template(
            "profile.html",
            profile_user=u,
            user_roles=roles_for_me,
            recent_ratings=recent_ratings,
            recent_reviews=recent_reviews,
            me=u,
            is_me=True,
        )

    @app.get("/u/<username>")
    def user_profile(username: str):
        conn = None
        profile = None
        recent_ratings = []
        recent_reviews = []
        roles_for_user = []
        me = None

        
        try:
            conn = get_conn()

            profile = dbx.user_by_username(conn, username)
            if not profile:
                abort(404)
            profile = dict(profile)

            avg, cnt = dbx.user_avg_rating(conn, int(profile["id"]))
            profile["avg_rating"] = avg
            profile["avg_rating_cnt"] = cnt


            viewer_id = int(session["uid"]) if session.get("uid") else None

            profile["followers_count"] = dbx.follower_count(conn, int(profile["id"]))
            profile["following_count"] = dbx.following_count(conn, int(profile["id"]))

            if viewer_id:
                profile["viewer_is_following"] = dbx.is_following(conn, viewer_id, int(profile["id"]))
                profile["viewer_friend_status"] = dbx.friends_status(conn, viewer_id, int(profile["id"]))
            else:
                profile["viewer_is_following"] = False
                profile["viewer_friend_status"] = "none"



            roles_for_user = [dict(x) for x in (dbx.roles_for_user(conn, int(profile["id"])) or [])]

            recent_ratings = [dict(x) for x in dbx.list_recent_ratings_for_user(conn, int(profile["id"]), limit=20)]
            recent_reviews = [dict(x) for x in dbx.list_recent_reviews_for_user(conn, int(profile["id"]), limit=20)]

            for rr in recent_ratings:
                if not rr.get("poster_url"):
                    rr["poster_url"] = poster_url_for_show(rr.get("corps", ""), rr.get("year") or 0)

            for rv in recent_reviews:
                if not rv.get("poster_url"):
                    rv["poster_url"] = poster_url_for_show(rv.get("corps", ""), rv.get("year") or 0)

            if not profile.get("avatar_url"):
                profile["avatar_url"] = pfp_url_for_username(profile.get("username", ""))

            if session.get("uid"):
                me_row = dbx.user_by_id(conn, int(session["uid"]))
                me = dict(me_row) if me_row else None

                

        finally:
            if conn is not None:
                conn.close()

        is_me = bool(me and me.get("username") == profile.get("username"))

        return render_template(
            "profile.html",
            profile_user=profile,
            user_roles=roles_for_user,
            recent_ratings=recent_ratings,
            recent_reviews=recent_reviews,
            me=me,
            is_me=is_me,
        )
    

    @app.get("/me/customize")
    @login_required
    def customize_profile():
        conn = None
        try:
            conn = get_conn()
            u = dbx.user_by_id(conn, int(session["uid"]))
            if not u:
                abort(404)
        finally:
            if conn is not None:
                conn.close()

        return render_template("profile_customize.html", u=u)


    @app.post("/me/customize")
    @login_required
    def customize_profile_post():
        avatar_url  = (request.form.get("avatar_url") or "").strip()
        banner_url  = (request.form.get("banner_url") or "").strip()
        theme_color = (request.form.get("theme_color") or "").strip()

        conn = None
        try:
            conn = get_conn()
            dbx.update_user_profile_style(conn, int(session["uid"]), avatar_url, banner_url, theme_color)
        finally:
            if conn is not None:
                conn.close()

        flash("Profile updated.", "ok")
        return redirect(url_for("me_profile"))




    # =========================================================
# Admin: list users so admin can assign roles
# =========================================================
    @app.get("/admin/users")
    @admin_required
    def admin_users():
        conn = None
        try:
            conn = get_conn()
            users = dbx.list_users(conn)  # you need this helper (below)
            users = [dict(u) for u in users]
        finally:
            if conn is not None:
                conn.close()

        return render_template("admin_users.html", users=users)

    @app.get("/admin/roles/<int:role_id>/edit")
    @admin_required
    def admin_edit_role(role_id: int):
        conn = None
        try:
            conn = get_conn()
            r = dbx.role_by_id(conn, role_id)
            if not r:
                abort(404)
        finally:
            if conn is not None:
                conn.close()

        return render_template("admin_role_edit.html", r=r)


    @app.post("/admin/roles/<int:role_id>/edit")
    @admin_required
    def admin_edit_role_post(role_id: int):
        name = (request.form.get("name") or "").strip()
        color = (request.form.get("color") or "").strip()

        conn = None
        try:
            conn = get_conn()
            try:
                dbx.update_role(conn, role_id, name, color)
            except ValueError as e:
                flash(str(e), "error")
                return redirect(url_for("admin_edit_role", role_id=role_id))
            except sqlite3.IntegrityError:
                flash("That role name already exists.", "error")
                return redirect(url_for("admin_edit_role", role_id=role_id))
        finally:
            if conn is not None:
                conn.close()

        flash("Role updated.", "ok")
        return redirect(url_for("admin_roles"))


    @app.post("/show/<int:show_id>/review/<int:review_user_id>/vote")
    @login_required
    def vote_review(show_id: int, review_user_id: int):
        vote = int(request.form.get("vote") or "0")
        if vote not in (1, -1):
            abort(400)

        voter_user_id = int(session["uid"])

        # Optional: prevent voting on your own review
        if voter_user_id == int(review_user_id):
            flash("You can't vote on your own review.", "error")
            return redirect(url_for("show_detail", show_id=show_id))

        conn = None
        try:
            conn = get_conn()
            dbx.set_review_vote(conn, show_id, review_user_id, voter_user_id, vote)
        finally:
            if conn is not None:
                conn.close()

        return redirect(url_for("show_detail", show_id=show_id))





    @app.get("/top")
    def top_shows():
        mode = (request.args.get("mode") or "top").strip().lower()
        if mode not in ("top", "bottom"):
            mode = "top"

        conn = None
        try:
            conn = get_conn()

            rows = dbx.list_top_shows_with_top_review(conn, limit=25, mode=mode)

            # ---- PATCH: ensure poster + avatar URLs exist for template ----
            rows2 = []
            for r in rows or []:
                d = dict(r)

                # Posters: prefer DB URL, else filesystem fallback
                if not d.get("poster_url"):
                    d["poster_url"] = poster_url_for_show(d.get("corps", ""), d.get("year") or 0)

                # Top review avatar: prefer DB URL, else filesystem fallback by username
                if d.get("top_review_username") and not d.get("top_review_avatar_url"):
                    d["top_review_avatar_url"] = pfp_url_for_username(d.get("top_review_username", ""))

                rows2.append(d)

            rows = rows2

        finally:
            if conn is not None:
                conn.close()

        return render_template("top_shows.html", rows=rows, mode=mode)
    

    # =========================================================
# SOCIAL ROUTES — follow + friends
# =========================================================

    @app.post("/u/<username>/follow")
    @login_required
    def follow_user(username: str):
        conn = None
        try:
            conn = get_conn()
            me_id = int(session["uid"])
            u = dbx.user_by_username(conn, username)
            if not u:
                abort(404)
            dbx.follow(conn, me_id, int(u["id"]))
            conn.commit()
        finally:
            if conn is not None:
                conn.close()
        return redirect(url_for("user_profile", username=username))


    @app.post("/u/<username>/unfollow")
    @login_required
    def unfollow_user(username: str):
        conn = None
        try:
            conn = get_conn()
            me_id = int(session["uid"])
            u = dbx.user_by_username(conn, username)
            if not u:
                abort(404)
            dbx.unfollow(conn, me_id, int(u["id"]))
            conn.commit()
        finally:
            if conn is not None:
                conn.close()
        return redirect(url_for("user_profile", username=username))


    @app.get("/u/<username>/followers")
    def user_followers(username: str):
        conn = None
        try:
            conn = get_conn()
            u = dbx.user_by_username(conn, username)
            if not u:
                abort(404)
            u = dict(u)

            
            rows = [dict(x) for x in dbx.list_followers(conn, int(u["id"]), limit=500)]
            return render_template("followers.html", profile_user=u, rows=rows)
        finally:
            if conn is not None:
                conn.close()


    @app.get("/u/<username>/following")
    def user_following(username: str):
        conn = None
        try:
            conn = get_conn()
            u = dbx.user_by_username(conn, username)
            if not u:
                abort(404)
            u = dict(u)
            rows = [dict(x) for x in dbx.list_following(conn, int(u["id"]), limit=500)]
            return render_template("following.html", profile_user=u, rows=rows)
        finally:
            if conn is not None:
                conn.close()


    @app.get("/me/friends")
    @login_required
    def my_friends():
        conn = None
        try:
            conn = get_conn()
            me_id = int(session["uid"])
            me = dbx.user_by_id(conn, me_id)
            me = dict(me) if me else None

            friends = [dict(x) for x in dbx.list_friends(conn, me_id, limit=500)]
            incoming = [dict(x) for x in dbx.list_incoming_friend_requests(conn, me_id, limit=200)]

            return render_template("friends.html", me=me, friends=friends, incoming=incoming)
        finally:
            if conn is not None:
                conn.close()


    @app.post("/u/<username>/friend/request")
    @login_required
    def friend_request(username: str):
        conn = None
        try:
            conn = get_conn()
            me_id = int(session["uid"])
            u = dbx.user_by_username(conn, username)
            if not u:
                abort(404)
            dbx.send_friend_request(conn, me_id, int(u["id"]))
            conn.commit()
        finally:
            if conn is not None:
                conn.close()
        return redirect(url_for("user_profile", username=username))


    @app.post("/u/<username>/friend/accept")
    @login_required
    def friend_accept(username: str):
        conn = None
        try:
            conn = get_conn()
            me_id = int(session["uid"])
            u = dbx.user_by_username(conn, username)
            if not u:
                abort(404)
            # requester = them, addressee = me
            dbx.accept_friend_request(conn, int(u["id"]), me_id)
            conn.commit()
        finally:
            if conn is not None:
                conn.close()
        return redirect(url_for("my_friends"))


    @app.post("/u/<username>/friend/decline")
    @login_required
    def friend_decline(username: str):
        conn = None
        try:
            conn = get_conn()
            me_id = int(session["uid"])
            u = dbx.user_by_username(conn, username)
            if not u:
                abort(404)
            dbx.decline_friend_request(conn, int(u["id"]), me_id)
            conn.commit()
        finally:
            if conn is not None:
                conn.close()
        return redirect(url_for("my_friends"))


    @app.post("/u/<username>/friend/remove")
    @login_required
    def friend_remove(username: str):
        conn = None
        try:
            conn = get_conn()
            me_id = int(session["uid"])
            u = dbx.user_by_username(conn, username)
            if not u:
                abort(404)
            dbx.remove_friend(conn, me_id, int(u["id"]))
            conn.commit()
        finally:
            if conn is not None:
                conn.close()
        return redirect(url_for("user_profile", username=username))




    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("base.html", body="Forbidden"), 403

    return app

from flask import Flask

app = Flask(__name__)


if __name__ == "__main__":
    # Local dev only
    app.run(debug=True)