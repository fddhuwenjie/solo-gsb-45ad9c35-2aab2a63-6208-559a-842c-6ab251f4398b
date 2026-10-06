"""Local dual-paper printing & answer-verification web app.

Run:  gunicorn server:app   (see Dockerfile / docker-compose.yml)
"""
import os
import sqlite3

from flask import Flask, g, jsonify, render_template, request

from .db import get_db, init_app

app = Flask(__name__)
app.config["DATABASE"] = os.environ.get(
    "DATABASE_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "exam.db")
)
app.config["JSON_AS_ASCII"] = False
init_app(app)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


@app.errorhandler(ApiError)
def _handle_api_error(err):
    return jsonify({"ok": False, "error": err.message}), err.status


@app.errorhandler(404)
def _handle_404(err):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "接口不存在"}), 404
    return err, 404


def payload():
    data = request.get_json(silent=True)
    if data is None:
        raise ApiError(400, "请求体必须是 JSON")
    return data


def require_text(data, key, label):
    val = data.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ApiError(400, f"{label}不能为空")
    return val.strip()


def fetch_questions(db):
    """Return live bank questions with options and usage counters."""
    questions = [dict(r) for r in db.execute(
        """SELECT q.id, q.stem,
                  (SELECT COUNT(DISTINCT s.id)
                     FROM sealed_question sq
                     JOIN seal s ON s.id = sq.seal_id
                    WHERE sq.source_question_id = q.id) AS seal_count
             FROM question q ORDER BY q.id"""
    ).fetchall()]
    for q in questions:
        q["options"] = [dict(r) for r in db.execute(
            "SELECT id, question_id, position, content FROM option "
            "WHERE question_id = ? ORDER BY position", (q["id"],)
        ).fetchall()]
        ans = db.execute(
            "SELECT option_id FROM bank_answer WHERE question_id = ?", (q["id"],)
        ).fetchone()
        q["correct_option_id"] = ans["option_id"] if ans else None
    return questions


# ---------------------------------------------------------------------------
# question bank
# ---------------------------------------------------------------------------

@app.get("/api/questions")
def list_questions():
    return jsonify({"ok": True, "questions": fetch_questions(get_db())})


@app.post("/api/questions")
def create_question():
    data = payload()
    stem = require_text(data, "stem", "题干")
    options = data.get("options")
    if not isinstance(options, list) or len(options) != 4:
        raise ApiError(400, "必须提供四个选项")
    texts = []
    for i, opt in enumerate(options):
        if not isinstance(opt, str) or not opt.strip():
            raise ApiError(400, f"第 {i + 1} 个选项内容不能为空")
        texts.append(opt.strip())
    correct = data.get("correct_position")
    if not isinstance(correct, int) or not 0 <= correct <= 3:
        raise ApiError(400, "正确选项位置必须是 0~3")

    db = get_db()
    cur = db.execute("INSERT INTO question (stem) VALUES (?)", (stem,))
    qid = cur.lastrowid
    option_ids = []
    for i, text in enumerate(texts):
        oid = db.execute(
            "INSERT INTO option (question_id, position, content) VALUES (?, ?, ?)",
            (qid, i, text),
        ).lastrowid
        option_ids.append(oid)
    db.execute(
        "INSERT INTO bank_answer (question_id, option_id) VALUES (?, ?)",
        (qid, option_ids[correct]),
    )
    db.commit()
    q = next(q for q in fetch_questions(db) if q["id"] == qid)
    return jsonify({"ok": True, "question": q})


@app.put("/api/questions/<int:qid>")
def update_question(qid):
    data = payload()
    db = get_db()
    row = db.execute("SELECT id FROM question WHERE id = ?", (qid,)).fetchone()
    if row is None:
        raise ApiError(404, "题目不存在")
    stem = require_text(data, "stem", "题干")
    texts = data.get("options")
    if not isinstance(texts, list) or len(texts) != 4:
        raise ApiError(400, "必须提供四个选项")
    clean = []
    for i, opt in enumerate(texts):
        if not isinstance(opt, str) or not opt.strip():
            raise ApiError(400, f"第 {i + 1} 个选项内容不能为空")
        clean.append(opt.strip())
    correct = data.get("correct_position")
    if not isinstance(correct, int) or not 0 <= correct <= 3:
        raise ApiError(400, "正确选项位置必须是 0~3")

    existing = db.execute(
        "SELECT id FROM option WHERE question_id = ? ORDER BY position", (qid,)
    ).fetchall()
    db.execute("UPDATE question SET stem = ? WHERE id = ?", (stem, qid))
    for i, text in enumerate(clean):
        db.execute("UPDATE option SET content = ? WHERE id = ?", (text, existing[i]["id"]))
    db.execute(
        "UPDATE bank_answer SET option_id = ? WHERE question_id = ?",
        (existing[correct]["id"], qid),
    )
    db.commit()
    q = next(q for q in fetch_questions(db) if q["id"] == qid)
    return jsonify({"ok": True, "question": q})


@app.delete("/api/questions/<int:qid>")
def delete_question(qid):
    db = get_db()
    row = db.execute("SELECT id FROM question WHERE id = ?", (qid,)).fetchone()
    if row is None:
        raise ApiError(404, "题目不存在")
    db.execute("DELETE FROM question WHERE id = ?", (qid,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# arrangement drafts (A / B)
# ---------------------------------------------------------------------------

def normalize_draft(db, paper, raw):
    """Validate/coerce a draft body into {questions:[{id, options:[ids]}]}.

    Unknown question/option ids and options of other questions are dropped;
    a question missing its four options falls back to bank order.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("questions"), list):
        raise ApiError(400, f"{paper} 卷草稿格式应为 {{questions: [...]}}")
    bank = {q["id"]: q for q in fetch_questions(db)}
    seen = set()
    result = []
    for item in raw["questions"]:
        if not isinstance(item, dict):
            raise ApiError(400, f"{paper} 卷草稿条目格式错误")
        qid = item.get("id")
        if not isinstance(qid, int) or qid not in bank or qid in seen:
            continue
        seen.add(qid)
        opt_ids = item.get("options")
        valid = (
            isinstance(opt_ids, list)
            and len(opt_ids) == 4
            and len(set(opt_ids)) == 4
            and all(o["id"] in opt_ids for o in bank[qid]["options"])
        )
        ordered = list(opt_ids) if valid else [o["id"] for o in bank[qid]["options"]]
        result.append({"id": qid, "options": ordered})
    return {"questions": result}


def save_draft(db, paper, draft):
    db.execute("INSERT INTO paper_draft (paper) VALUES (?) "
               "ON CONFLICT(paper) DO UPDATE SET updated_at = datetime('now')", (paper,))
    db.execute("DELETE FROM paper_draft_question WHERE paper = ?", (paper,))
    db.execute("DELETE FROM paper_draft_option WHERE paper = ?", (paper,))
    for pos, q in enumerate(draft["questions"]):
        db.execute(
            "INSERT INTO paper_draft_question (paper, question_id, position) VALUES (?, ?, ?)",
            (paper, q["id"], pos),
        )
        for opos, oid in enumerate(q["options"]):
            db.execute(
                "INSERT INTO paper_draft_option (paper, question_id, option_id, position) "
                "VALUES (?, ?, ?, ?)",
                (paper, q["id"], oid, opos),
            )


def load_draft(db, paper):
    rows = db.execute(
        "SELECT question_id, position FROM paper_draft_question "
        "WHERE paper = ? ORDER BY position",
        (paper,),
    ).fetchall()
    questions = []
    bank = {q["id"]: q for q in fetch_questions(db)}
    for r in rows:
        if r["question_id"] not in bank:
            continue
        opt_rows = db.execute(
            "SELECT option_id FROM paper_draft_option WHERE paper = ? AND question_id = ? "
            "ORDER BY position", (paper, r["question_id"])
        ).fetchall()
        opt_ids = [x["option_id"] for x in opt_rows]
        valid = (
            len(opt_ids) == 4
            and len(set(opt_ids)) == 4
            and all(o["id"] in opt_ids for o in bank[r["question_id"]]["options"])
        )
        if not valid:
            opt_ids = [o["id"] for o in bank[r["question_id"]]["options"]]
        questions.append({"id": r["question_id"], "options": opt_ids})
    return {"questions": questions}


@app.get("/api/draft/<paper>")
def get_draft(paper):
    if paper not in ("A", "B"):
        raise ApiError(404, "卷别只支持 A / B")
    db = get_db()
    draft = load_draft(db, paper)
    # fill in bank questions never arranged yet, in bank order
    arranged = {q["id"] for q in draft["questions"]}
    for q in fetch_questions(db):
        if q["id"] not in arranged:
            draft["questions"].append({"id": q["id"], "options": [o["id"] for o in q["options"]]})
    return jsonify({"ok": True, "paper": paper, "draft": draft})


@app.put("/api/draft/<paper>")
def put_draft(paper):
    if paper not in ("A", "B"):
        raise ApiError(404, "卷别只支持 A / B")
    db = get_db()
    draft = normalize_draft(db, paper, payload())
    save_draft(db, paper, draft)
    db.commit()
    return jsonify({"ok": True, "draft": draft})


# ---------------------------------------------------------------------------
# seals
# ---------------------------------------------------------------------------

@app.post("/api/seals")
def create_seal():
    data = payload()
    note = (data.get("note") or "").strip() if isinstance(data.get("note"), str) else ""
    db = get_db()

    # Source of truth: persisted drafts (optionally overridden in the body).
    body = data.get("papers") if isinstance(data.get("papers"), dict) else {}
    drafts = {}
    for paper in ("A", "B"):
        if paper in body:
            drafts[paper] = normalize_draft(db, paper, body[paper])
        else:
            drafts[paper] = load_draft(db, paper)

    for paper in ("A", "B"):
        if not drafts[paper]["questions"]:
            raise ApiError(400, f"{paper} 卷至少要包含一道题")
    ids_a = [q["id"] for q in drafts["A"]["questions"]]
    ids_b = [q["id"] for q in drafts["B"]["questions"]]
    if set(ids_a) != set(ids_b) or len(ids_a) != len(set(ids_a)):
        raise ApiError(400, "A、B 两卷必须包含相同的题目集合（顺序可以不同）")

    # Pre-flight: every bank question must still have an answer on record.
    for item in drafts["A"]["questions"]:
        ans = db.execute(
            "SELECT option_id FROM bank_answer WHERE question_id = ?", (item["id"],)
        ).fetchone()
        if ans is None:
            raise ApiError(400, f"题目 Q{item['id']} 缺少正确答案，无法封存")

    version = (db.execute("SELECT COALESCE(MAX(version), 0) + 1 AS v FROM seal").fetchone()["v"])
    cur = db.execute("INSERT INTO seal (version, note) VALUES (?, ?)", (version, note))
    seal_id = cur.lastrowid

    try:
        # Build one shared snapshot (question + options + answer) per bank item.
        snapshots = {}  # source question id -> {"sq_id":, "options": {src_oid: so_id}}
        for item in drafts["A"]["questions"]:
            qrow = db.execute("SELECT stem FROM question WHERE id = ?", (item["id"],)).fetchone()
            sq_id = db.execute(
                "INSERT INTO sealed_question (seal_id, source_question_id, stable_code, stem) "
                "VALUES (?, ?, ?, ?)",
                (seal_id, item["id"], f"Q{item['id']}", qrow["stem"]),
            ).lastrowid
            option_by_id = {}
            for src_oid in item["options"]:
                orow = db.execute("SELECT content FROM option WHERE id = ?", (src_oid,)).fetchone()
                so_id = db.execute(
                    "INSERT INTO sealed_option (sealed_question_id, source_option_id, "
                    "stable_code, content) VALUES (?, ?, ?, ?)",
                    (sq_id, src_oid, f"O{src_oid}", orow["content"]),
                ).lastrowid
                option_by_id[src_oid] = so_id

            answer_row = db.execute(
                "SELECT option_id FROM bank_answer WHERE question_id = ?", (item["id"],)
            ).fetchone()
            db.execute(
                "UPDATE sealed_question SET correct_sealed_option_id = ? WHERE id = ?",
                (option_by_id[answer_row["option_id"]], sq_id),
            )
            snapshots[item["id"]] = {"sq_id": sq_id, "options": option_by_id}

        # Each paper maps the shared snapshots to its own numbers and A-D letters.
        for paper in ("A", "B"):
            paper_id = db.execute(
                "INSERT INTO paper (seal_id, name) VALUES (?, ?)", (seal_id, paper)
            ).lastrowid
            for number, item in enumerate(drafts[paper]["questions"], start=1):
                snap = snapshots[item["id"]]
                pq_id = db.execute(
                    "INSERT INTO paper_question (paper_id, sealed_question_id, number) "
                    "VALUES (?, ?, ?)",
                    (paper_id, snap["sq_id"], number),
                ).lastrowid
                for letter, src_oid in zip("ABCD", item["options"]):
                    db.execute(
                        "INSERT INTO paper_option (paper_question_id, sealed_option_id, letter) "
                        "VALUES (?, ?, ?)",
                        (pq_id, snap["options"][src_oid], letter),
                    )
        db.commit()
    except sqlite3.DatabaseError:
        db.rollback()
        raise ApiError(500, "封存写入失败，已回滚，未生成新版本")
    return jsonify({"ok": True, "seal_id": seal_id, "version": version})


def _seal_summary(db, seal_row):
    s = dict(seal_row)
    s["question_count"] = db.execute(
        "SELECT COUNT(*) c FROM sealed_question WHERE seal_id = ?", (s["id"],)
    ).fetchone()["c"]
    s["erratum_count"] = db.execute(
        "SELECT COUNT(*) c FROM erratum_batch WHERE seal_id = ?", (s["id"],)
    ).fetchone()["c"]
    s["card_count"] = db.execute(
        "SELECT COUNT(*) c FROM answer_card WHERE seal_id = ?", (s["id"],)
    ).fetchone()["c"]
    return s


@app.get("/api/seals")
def list_seals():
    db = get_db()
    seals = [_seal_summary(db, r) for r in db.execute(
        "SELECT id, version, note, created_at FROM seal ORDER BY version DESC"
    ).fetchall()]
    return jsonify({"ok": True, "seals": seals})


def _build_seal_detail(db, seal_id):
    seal = db.execute(
        "SELECT id, version, note, created_at FROM seal WHERE id = ?", (seal_id,)
    ).fetchone()
    if seal is None:
        raise ApiError(404, "封存卷不存在")
    detail = _seal_summary(db, seal)
    detail["papers"] = {}
    for paper_name in ("A", "B"):
        prow = db.execute(
            "SELECT id FROM paper WHERE seal_id = ? AND name = ?", (seal_id, paper_name)
        ).fetchone()
        questions_out = []
        pq_rows = db.execute(
            "SELECT id, number, sealed_question_id FROM paper_question "
            "WHERE paper_id = ? ORDER BY number", (prow["id"],)
        ).fetchall()
        for pq in pq_rows:
            sq = db.execute(
                "SELECT id, source_question_id, stable_code, stem, correct_sealed_option_id "
                "FROM sealed_question WHERE id = ?", (pq["sealed_question_id"],)
            ).fetchone()
            options = []
            for po in db.execute(
                """SELECT po.letter, so.id AS sealed_option_id, so.source_option_id,
                          so.stable_code, so.content
                     FROM paper_option po
                     JOIN sealed_option so ON so.id = po.sealed_option_id
                    WHERE po.paper_question_id = ? ORDER BY po.letter""",
                (pq["id"],)
            ).fetchall():
                od = dict(po)
                od["is_correct"] = (po["sealed_option_id"] == sq["correct_sealed_option_id"])
                options.append(od)
            qd = dict(sq)
            qd["number"] = pq["number"]
            qd["options"] = options
            qd["correct_letter"] = next(
                (o["letter"] for o in options if o["is_correct"]), None
            )
            questions_out.append(qd)
        detail["papers"][paper_name] = {"name": paper_name, "questions": questions_out}
    return detail


@app.get("/api/seals/<int:seal_id>")
def get_seal(seal_id):
    return jsonify({"ok": True, "seal": _build_seal_detail(get_db(), seal_id)})


# ---------------------------------------------------------------------------
# errata
# ---------------------------------------------------------------------------

def _errata(db, seal_id):
    # letter -> sealed option stable code, per paper per question
    letter_codes = {"A": {}, "B": {}}
    for pname in ("A", "B"):
        for r in db.execute(
            """SELECT pq.sealed_question_id, po.letter, so.stable_code
                 FROM paper p
                 JOIN paper_question pq ON pq.paper_id = p.id
                 JOIN paper_option po ON po.paper_question_id = pq.id
                 JOIN sealed_option so ON so.id = po.sealed_option_id
                WHERE p.seal_id = ? AND p.name = ?""",
            (seal_id, pname),
        ).fetchall():
            letter_codes[pname].setdefault(r["sealed_question_id"], {})[r["letter"]] = r["stable_code"]

    batches = []
    for b in db.execute(
        "SELECT id, reason, created_at FROM erratum_batch WHERE seal_id = ? ORDER BY id",
        (seal_id,),
    ).fetchall():
        items = []
        for ei in db.execute(
            """SELECT ei.id, ei.sealed_question_id, ei.new_sealed_option_id,
                      ei.paper_a_old_letter, ei.paper_a_new_letter,
                      ei.paper_b_old_letter, ei.paper_b_new_letter,
                      sq.source_question_id, sq.stable_code AS question_code,
                      so_new.stable_code AS new_option_code
                 FROM erratum_item ei
                 JOIN sealed_question sq ON sq.id = ei.sealed_question_id
                 JOIN sealed_option so_new ON so_new.id = ei.new_sealed_option_id
                WHERE ei.batch_id = ? ORDER BY ei.id""",
            (b["id"],),
        ).fetchall():
            d = dict(ei)
            sq_id = d["sealed_question_id"]
            d["paper_a_old_option_code"] = letter_codes["A"].get(sq_id, {}).get(d["paper_a_old_letter"])
            d["paper_a_new_option_code"] = letter_codes["A"].get(sq_id, {}).get(d["paper_a_new_letter"])
            d["paper_b_old_option_code"] = letter_codes["B"].get(sq_id, {}).get(d["paper_b_old_letter"])
            d["paper_b_new_option_code"] = letter_codes["B"].get(sq_id, {}).get(d["paper_b_new_letter"])
            items.append(d)
        bd = dict(b)
        bd["items"] = items
        batches.append(bd)
    return batches


@app.get("/api/seals/<int:seal_id>/errata")
def list_errata(seal_id):
    db = get_db()
    if db.execute("SELECT 1 FROM seal WHERE id = ?", (seal_id,)).fetchone() is None:
        raise ApiError(404, "封存卷不存在")
    return jsonify({"ok": True, "batches": _errata(db, seal_id)})


@app.post("/api/seals/<int:seal_id>/errata")
def create_errata(seal_id):
    data = payload()
    reason = data.get("reason")
    reason = reason.strip() if isinstance(reason, str) else ""
    items_raw = data.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        raise ApiError(400, "勘误至少包含一条修正")

    db = get_db()
    seal = db.execute("SELECT id FROM seal WHERE id = ?", (seal_id,)).fetchone()
    if seal is None:
        raise ApiError(404, "封存卷不存在")

    # ---- phase 1: validate everything, write nothing ---------------------
    sealed_questions = {
        r["id"]: dict(r) for r in db.execute(
            "SELECT id, source_question_id, stable_code, correct_sealed_option_id "
            "FROM sealed_question WHERE seal_id = ?", (seal_id,)
        ).fetchall()
    }
    sealed_options = {}   # sealed_option_id -> row
    options_of_question = {}  # sq_id -> {sealed_option_id: row}
    for r in db.execute(
        """SELECT so.id, so.sealed_question_id, so.source_option_id, so.stable_code
             FROM sealed_option so
            WHERE so.sealed_question_id IN
                  (SELECT id FROM sealed_question WHERE seal_id = ?)""",
        (seal_id,),
    ).fetchall():
        d = dict(r)
        sealed_options[d["id"]] = d
        options_of_question.setdefault(d["sealed_question_id"], {})[d["id"]] = d

    # paper mapping: sq_id -> {letter: sealed_option_id}
    paper_map = {"A": {}, "B": {}}
    for pname in ("A", "B"):
        for r in db.execute(
            """SELECT pq.sealed_question_id, po.letter, po.sealed_option_id
                 FROM paper p
                 JOIN paper_question pq ON pq.paper_id = p.id
                 JOIN paper_option po ON po.paper_question_id = pq.id
                WHERE p.seal_id = ? AND p.name = ?""",
            (seal_id, pname),
        ).fetchall():
            paper_map[pname].setdefault(r["sealed_question_id"], {})[r["letter"]] = r["sealed_option_id"]

    prepared = []
    seen = set()
    for idx, it in enumerate(items_raw, start=1):
        if not isinstance(it, dict):
            raise ApiError(400, f"第 {idx} 条勘误格式错误")
        sq_id = it.get("sealed_question_id")
        new_so_id = it.get("new_sealed_option_id")
        if not isinstance(sq_id, int) or sq_id not in sealed_questions:
            raise ApiError(400, f"第 {idx} 条：题目不属于本次封存卷")
        if not isinstance(new_so_id, int) or new_so_id not in sealed_options:
            raise ApiError(400, f"第 {idx} 条（{sealed_questions[sq_id]['stable_code']}）：选项不属于封存题目")
        if new_so_id not in options_of_question[sq_id]:
            raise ApiError(400, f"第 {idx} 条（{sealed_questions[sq_id]['stable_code']}）：选项不属于封存题目")
        if sq_id in seen:
            raise ApiError(400, f"第 {idx} 条：{sealed_questions[sq_id]['stable_code']} 重复提交")
        sq = sealed_questions[sq_id]
        if new_so_id == sq["correct_sealed_option_id"]:
            raise ApiError(400, f"第 {idx} 条（{sq['stable_code']}）：新答案与原答案一致，无需勘误")
        missing = [p for p in ("A", "B") if sq_id not in paper_map[p]]
        if missing:
            raise ApiError(400,
                f"第 {idx} 条（{sq['stable_code']}）：{'、'.join(missing)} 卷缺少该题映射，整次拒绝")

        def letters(pname):
            mapping = paper_map[pname][sq_id]
            old = next(l for l, so in mapping.items() if so == sq["correct_sealed_option_id"])
            new = next(l for l, so in mapping.items() if so == new_so_id)
            return old, new

        a_old, a_new = letters("A")
        b_old, b_new = letters("B")
        prepared.append({
            "sq": sq, "new_so_id": new_so_id,
            "a": (a_old, a_new), "b": (b_old, b_new),
        })
        seen.add(sq_id)

    # ---- phase 2: commit --------------------------------------------------
    try:
        batch_id = db.execute(
            "INSERT INTO erratum_batch (seal_id, reason) VALUES (?, ?)", (seal_id, reason)
        ).lastrowid
        for p in prepared:
            db.execute(
                """INSERT INTO erratum_item
                   (batch_id, sealed_question_id, new_sealed_option_id,
                    paper_a_old_letter, paper_a_new_letter,
                    paper_b_old_letter, paper_b_new_letter)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (batch_id, p["sq"]["id"], p["new_so_id"],
                 p["a"][0], p["a"][1], p["b"][0], p["b"][1]),
            )
            db.execute(
                "UPDATE sealed_question SET correct_sealed_option_id = ? WHERE id = ?",
                (p["new_so_id"], p["sq"]["id"]),
            )
        db.commit()
    except sqlite3.DatabaseError as exc:
        db.rollback()
        raise ApiError(500, f"勘误写入失败，已回滚：{exc}")

    return jsonify({
        "ok": True,
        "batch_id": batch_id,
        "applied": [
            {
                "question_code": p["sq"]["stable_code"],
                "A": {"old": p["a"][0], "new": p["a"][1]},
                "B": {"old": p["b"][0], "new": p["b"][1]},
            }
            for p in prepared
        ],
    })


# ---------------------------------------------------------------------------
# answer-card registration & post-erratum score review
# ---------------------------------------------------------------------------

def _paper_key(db, seal_id, paper_name):
    """number -> {pq_id, sq_id, correct_so_id, letter_to_so} for one paper."""
    prow = db.execute(
        "SELECT id FROM paper WHERE seal_id = ? AND name = ?", (seal_id, paper_name)
    ).fetchone()
    if prow is None:
        return None
    questions = {}
    for pq in db.execute(
        "SELECT id, number, sealed_question_id FROM paper_question "
        "WHERE paper_id = ? ORDER BY number", (prow["id"],)
    ).fetchall():
        sq = db.execute(
            "SELECT correct_sealed_option_id FROM sealed_question WHERE id = ?",
            (pq["sealed_question_id"],),
        ).fetchone()
        letter_to_so = {}
        for po in db.execute(
            "SELECT letter, sealed_option_id FROM paper_option WHERE paper_question_id = ?",
            (pq["id"],),
        ).fetchall():
            letter_to_so[po["letter"]] = po["sealed_option_id"]
        questions[pq["number"]] = {
            "pq_id": pq["id"],
            "sq_id": pq["sealed_question_id"],
            "correct_so_id": sq["correct_sealed_option_id"],
            "letter_to_so": letter_to_so,
        }
    return questions


def _card_row(db, card_id):
    return db.execute(
        "SELECT id, seal_id, paper_name, candidate_code, registered_total, created_at "
        "FROM answer_card WHERE id = ?", (card_id,)
    ).fetchone()


def _card_detail(db, card_id, seal_id=None):
    """Registered card plus its always-current re-scoring against the key."""
    card = _card_row(db, card_id)
    if card is None or (seal_id is not None and card["seal_id"] != seal_id):
        raise ApiError(404, "答题卡不存在")
    d = dict(card)
    items = []
    current_total = 0
    for r in db.execute(
        """SELECT aci.number, aci.chosen_sealed_option_id, aci.registered_score,
                  pq.sealed_question_id, sq.stable_code AS question_code,
                  sq.correct_sealed_option_id,
                  po_ch.letter AS chosen_letter,
                  po_cur.letter AS current_letter
             FROM answer_card_item aci
             JOIN paper_question pq ON pq.id = aci.paper_question_id
             JOIN sealed_question sq ON sq.id = pq.sealed_question_id
             LEFT JOIN paper_option po_ch ON po_ch.paper_question_id = pq.id
                  AND po_ch.sealed_option_id = aci.chosen_sealed_option_id
             LEFT JOIN paper_option po_cur ON po_cur.paper_question_id = pq.id
                  AND po_cur.sealed_option_id = sq.correct_sealed_option_id
            WHERE aci.card_id = ? ORDER BY aci.number""",
        (card_id,),
    ).fetchall():
        it = dict(r)
        it["blank"] = it["chosen_sealed_option_id"] is None
        # Re-score against the answer key as it stands now (errata flip it).
        it["current_score"] = (
            1 if not it["blank"]
            and it["chosen_sealed_option_id"] == it["correct_sealed_option_id"]
            else 0
        )
        it["changed"] = it["current_score"] != it["registered_score"]
        current_total += it["current_score"]
        items.append(it)
    d["question_count"] = len(items)
    d["current_total"] = current_total
    d["changed_count"] = sum(1 for it in items if it["changed"])
    d["items"] = items
    d["changed_items"] = [
        {"number": it["number"], "question_code": it["question_code"],
         "registered_score": it["registered_score"], "current_score": it["current_score"],
         "chosen_letter": it["chosen_letter"],
         "registered_letter": it["chosen_letter"],
         "current_letter": it["current_letter"]}
        for it in items if it["changed"]
    ]
    return d


@app.post("/api/seals/<int:seal_id>/cards")
def register_card(seal_id):
    data = payload()
    paper_name = data.get("paper")
    if paper_name not in ("A", "B"):
        raise ApiError(400, "请选择 A 卷或 B 卷")
    candidate_code = require_text(data, "candidate_code", "考生编号")
    answers_raw = data.get("answers")
    if not isinstance(answers_raw, list):
        raise ApiError(400, "作答必须是逐题答案列表")

    db = get_db()
    if db.execute("SELECT 1 FROM seal WHERE id = ?", (seal_id,)).fetchone() is None:
        raise ApiError(404, "封存卷不存在")

    questions = _paper_key(db, seal_id, paper_name)
    if not questions:
        raise ApiError(400, f"{paper_name} 卷没有题目，无法登记答题卡")

    # ---- phase 1: validate every entry, write nothing --------------------
    if db.execute(
        "SELECT 1 FROM answer_card WHERE seal_id = ? AND candidate_code = ?",
        (seal_id, candidate_code),
    ).fetchone() is not None:
        raise ApiError(409, f"考生 {candidate_code} 在本封存版本已登记过答题卡，不能重复登记")

    prepared = {}
    for idx, ans in enumerate(answers_raw, start=1):
        if not isinstance(ans, dict):
            raise ApiError(400, f"第 {idx} 条作答格式错误")
        number = ans.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise ApiError(400, f"第 {idx} 条作答：题号必须是整数")
        if number not in questions:
            raise ApiError(400, f"题号 {number} 不属于 {paper_name} 卷（多余题号），整张拒绝")
        if number in prepared:
            raise ApiError(400, f"题号 {number} 重复出现，整张拒绝")
        raw_letter = ans.get("letter")
        if raw_letter is None:
            letter = ""
        elif isinstance(raw_letter, str):
            letter = raw_letter.strip().upper()
        else:
            raise ApiError(400, f"第 {number} 题：作答字母必须是 A–D 或留空")
        if letter == "":
            chosen_so_id = None
        else:
            chosen_so_id = questions[number]["letter_to_so"].get(letter)
            if chosen_so_id is None:
                raise ApiError(400, f"第 {number} 题：该卷没有字母 {letter} 选项，整张拒绝")
        registered_score = (
            1 if chosen_so_id is not None
            and chosen_so_id == questions[number]["correct_so_id"] else 0
        )
        prepared[number] = (chosen_so_id, registered_score)

    missing = sorted(set(questions) - set(prepared))
    if missing:
        raise ApiError(400, "缺少题号 " + "、".join(map(str, missing)) + "，整张拒绝")

    # ---- phase 2: commit --------------------------------------------------
    try:
        cur = db.execute(
            "INSERT INTO answer_card (seal_id, paper_name, candidate_code, registered_total) "
            "VALUES (?, ?, ?, ?)",
            (seal_id, paper_name, candidate_code,
             sum(score for _, score in prepared.values())),
        )
        card_id = cur.lastrowid
        for number in sorted(prepared):
            chosen_so_id, score = prepared[number]
            db.execute(
                "INSERT INTO answer_card_item "
                "(card_id, paper_question_id, number, chosen_sealed_option_id, registered_score) "
                "VALUES (?, ?, ?, ?, ?)",
                (card_id, questions[number]["pq_id"], number, chosen_so_id, score),
            )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        raise ApiError(409, f"考生 {candidate_code} 在本封存版本已登记过答题卡，不能重复登记")
    except sqlite3.DatabaseError as exc:
        db.rollback()
        raise ApiError(500, f"登记写入失败，已回滚，未留下记录：{exc}")

    return jsonify({"ok": True, "card": _card_detail(db, card_id, seal_id)})


@app.get("/api/seals/<int:seal_id>/cards")
def list_cards(seal_id):
    db = get_db()
    if db.execute("SELECT 1 FROM seal WHERE id = ?", (seal_id,)).fetchone() is None:
        raise ApiError(404, "封存卷不存在")
    cards = []
    for ac in db.execute(
        "SELECT id, seal_id, paper_name, candidate_code, registered_total, created_at "
        "FROM answer_card WHERE seal_id = ? ORDER BY id", (seal_id,)
    ).fetchall():
        d = dict(ac)
        d["question_count"] = db.execute(
            "SELECT COUNT(*) c FROM answer_card_item WHERE card_id = ?", (ac["id"],)
        ).fetchone()["c"]
        d["current_total"] = db.execute(
            """SELECT COUNT(*) c
                 FROM answer_card_item aci
                 JOIN paper_question pq ON pq.id = aci.paper_question_id
                 JOIN sealed_question sq ON sq.id = pq.sealed_question_id
                WHERE aci.card_id = ?
                  AND aci.chosen_sealed_option_id IS sq.correct_sealed_option_id""",
            (ac["id"],),
        ).fetchone()["c"]
        d["changed"] = d["current_total"] != d["registered_total"]
        cards.append(d)
    return jsonify({"ok": True, "cards": cards})


@app.get("/api/seals/<int:seal_id>/cards/<int:card_id>")
def get_card(seal_id, card_id):
    return jsonify({"ok": True, "card": _card_detail(get_db(), card_id, seal_id)})


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

@app.get("/")
def page_home():
    return render_template("index.html", active="home")


@app.get("/questions")
def page_questions():
    return render_template("index.html", active="questions")


@app.get("/arrange")
def page_arrange():
    return render_template("index.html", active="arrange")


@app.get("/seals")
def page_seals():
    return render_template("index.html", active="seals")


@app.get("/seals/<int:seal_id>")
def page_seal_detail(seal_id):
    return render_template("index.html", active="seals")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
