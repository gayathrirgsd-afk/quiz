"""
PDF → Quiz  |  Streamlit App
-----------------------------
Upload a PDF containing multiple-choice questions (with an answer key,
in almost any common format) and this app will:
  1. Extract text from the PDF
  2. Parse it into structured questions (stem, options A-D, correct answer,
     explanation, and unit/topic if headings like "Unit 1", "Chapter 2" exist)
  3. Let the instructor review/fix anything the parser missed, including
     which unit each question belongs to
  4. Launch an interactive quiz for students: pick a unit (or all units),
     answer, submit, see a scored + color-coded review with a unit-wise
     breakdown, and retake with a shuffle
  5. Track every attempt in a Dashboard tab: score history per student,
     per-unit performance, over time

Run with:  streamlit run app.py
"""

import csv
import io
import json
import os
import random
import re
from datetime import datetime

import streamlit as st

# --------------------------------------------------------------------------
# PDF TEXT EXTRACTION
# --------------------------------------------------------------------------

def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Try pdfplumber first (better layout handling), fall back to pypdf."""
    text_parts = []
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                text_parts.append(page_text)
        text = "\n".join(text_parts)
        if text.strip():
            return text
    except Exception:
        pass

    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(file_bytes))
        for page in reader.pages:
            text_parts.append(page.extract_text() or "")
        return "\n".join(text_parts)
    except Exception as e:
        raise RuntimeError(f"Could not read PDF: {e}")


# --------------------------------------------------------------------------
# QUESTION PARSER
# --------------------------------------------------------------------------
# Handles common textbook / exam-bank layouts:
#   1. Question text...            Q1. Question text...
#   A) option                      A. option
#   B) option                      (B) option
#   Answer: C                      Ans: C          Correct Answer: C
#   Explanation: ...               Explanation - ...
#
# Unit/topic headings like "Unit 1: Basic Algebra", "Chapter 2 - Cells",
# "Section 3", "Module 4: Networking", "Topic: Recursion" are detected and
# applied to every question that follows until the next heading.
#
# Multi-line question stems and option text are supported (a continuation
# line is appended to whichever field is currently "open").

RE_PAGE_NOISE = re.compile(r"^\s*page\s*\d+(\s*(of|/)\s*\d+)?\s*$", re.IGNORECASE)
RE_QUESTION_START = re.compile(r"^\s*(?:Q(?:uestion)?\.?\s*)?(\d{1,3})[\.\)]\s*(.*)$")
RE_OPTION_START = re.compile(r"^\s*\(?([A-Da-d])\)?[\.\):]\s+(.*)$")
RE_ANSWER_LINE = re.compile(
    r"^\s*(?:correct\s+)?(?:answer|ans)\s*[:\-]?\s*\(?([A-Da-d])\)?\.?\s*(.*)$",
    re.IGNORECASE,
)
RE_EXPLANATION_LINE = re.compile(r"^\s*explanation\s*[:\-]?\s*(.*)$", re.IGNORECASE)
RE_UNIT_HEADING = re.compile(
    r"^\s*(unit|chapter|section|module|topic)\b\s*[:\-]?\s*(\d+)?\s*[:\-]?\s*(.*)$",
    re.IGNORECASE,
)

DEFAULT_UNIT = "General"


def parse_questions(raw_text: str):
    lines = [ln.rstrip() for ln in raw_text.split("\n")]

    questions = []
    current = None
    active_field = None  # ("stem",) or ("option", "A") or ("answer",) or ("explanation",)
    current_unit = DEFAULT_UNIT

    def start_new_question(qnum, first_line_text):
        nonlocal current, active_field
        if current is not None and (current["stem"].strip() or current["options"]):
            questions.append(current)
        current = {
            "number": qnum,
            "stem": first_line_text.strip(),
            "options": {},       # letter -> text
            "option_order": [],  # preserve insertion order
            "answer": None,
            "explanation": "",
            "unit": current_unit,
        }
        active_field = ("stem",)

    for line in lines:
        if not line.strip():
            continue
        if RE_PAGE_NOISE.match(line):
            continue

        m_q = RE_QUESTION_START.match(line)
        m_opt = RE_OPTION_START.match(line)
        m_ans = RE_ANSWER_LINE.match(line)
        m_exp = RE_EXPLANATION_LINE.match(line)
        m_unit = RE_UNIT_HEADING.match(line)

        # Only treat a numbered line as a new question if it doesn't also
        # look like an option line (e.g. "10 g of NaCl..." shouldn't be
        # mistaken for question #10).
        if m_q and not m_opt:
            start_new_question(m_q.group(1), m_q.group(2))
            continue

        # A standalone "Unit X: ..." / "Chapter 2 - ..." heading (and not
        # simultaneously an option/answer line) starts a new topic context
        # for every question parsed after it.
        if m_unit and not m_opt and not m_ans:
            current_unit = line.strip()
            active_field = None
            continue

        if current is None:
            continue

        if m_opt:
            letter = m_opt.group(1).upper()
            text = m_opt.group(2).strip()
            current["options"][letter] = text
            if letter not in current["option_order"]:
                current["option_order"].append(letter)
            active_field = ("option", letter)
            continue

        if m_ans:
            letter = m_ans.group(1).upper()
            trailing = m_ans.group(2).strip()
            current["answer"] = letter
            if trailing:
                current["explanation"] = (current["explanation"] + " " + trailing).strip()
            active_field = ("answer",)
            continue

        if m_exp:
            text = m_exp.group(1).strip()
            current["explanation"] = (current["explanation"] + " " + text).strip()
            active_field = ("explanation",)
            continue

        # Continuation line - append to whatever field is currently open
        if active_field is None:
            continue
        kind = active_field[0]
        if kind == "stem":
            current["stem"] = (current["stem"] + " " + line.strip()).strip()
        elif kind == "option":
            letter = active_field[1]
            current["options"][letter] = (current["options"][letter] + " " + line.strip()).strip()
        elif kind == "answer" or kind == "explanation":
            current["explanation"] = (current["explanation"] + " " + line.strip()).strip()

    if current is not None and (current["stem"].strip() or current["options"]):
        questions.append(current)

    # Clean-up: drop questions with fewer than 2 options (junk/noise)
    cleaned = [q for q in questions if len(q["options"]) >= 2]
    return cleaned


def normalize_questions(qs):
    """Ensure every question dict (from PDF parse, loaded JSON, or manual
    add) has all expected keys, so older saved quizzes still work."""
    for q in qs:
        q.setdefault("unit", DEFAULT_UNIT)
        q.setdefault("explanation", "")
        q.setdefault("answer", "")
        q.setdefault("options", {})
        q.setdefault("option_order", list(q["options"].keys()))
        if not q["unit"]:
            q["unit"] = DEFAULT_UNIT
    return qs


def get_units(qs):
    seen = []
    for q in qs:
        u = q.get("unit", DEFAULT_UNIT)
        if u not in seen:
            seen.append(u)
    return seen


# --------------------------------------------------------------------------
# ATTEMPT HISTORY (persisted to a local CSV next to this script)
# --------------------------------------------------------------------------

ATTEMPTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attempts_log.csv")
ATTEMPT_FIELDS = ["timestamp", "student_name", "unit_filter", "score", "total", "percentage", "unit_breakdown"]


def log_attempt(student_name, unit_filter, score, total, pct, unit_breakdown):
    is_new = not os.path.exists(ATTEMPTS_FILE)
    try:
        with open(ATTEMPTS_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=ATTEMPT_FIELDS)
            if is_new:
                writer.writeheader()
            writer.writerow({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "student_name": student_name or "Anonymous",
                "unit_filter": unit_filter,
                "score": score,
                "total": total,
                "percentage": round(pct * 100, 1),
                "unit_breakdown": json.dumps(unit_breakdown),
            })
    except Exception:
        pass  # don't let logging failures break the quiz experience


def load_attempts():
    if not os.path.exists(ATTEMPTS_FILE):
        return []
    rows = []
    try:
        with open(ATTEMPTS_FILE, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    row["unit_breakdown"] = json.loads(row.get("unit_breakdown") or "{}")
                except Exception:
                    row["unit_breakdown"] = {}
                rows.append(row)
    except Exception:
        pass
    return rows


# --------------------------------------------------------------------------
# SHARED "CURRENT QUIZ" (so every student's browser sees what the instructor
# prepared, without each of them re-uploading the PDF themselves)
# --------------------------------------------------------------------------
# Streamlit gives every browser tab its own private session_state, so a PDF
# parsed in the instructor's browser is invisible to a student's browser by
# default. To fix that, "Prepare Quiz" also writes the question bank to a
# shared file next to app.py; every new student session (and a manual
# refresh button) reads it back in from there.

CURRENT_QUIZ_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "current_quiz.json")


def publish_current_quiz(questions):
    try:
        with open(CURRENT_QUIZ_FILE, "w", encoding="utf-8") as f:
            json.dump(questions, f)
        return True
    except Exception:
        return False


def load_current_quiz():
    if not os.path.exists(CURRENT_QUIZ_FILE):
        return None
    try:
        with open(CURRENT_QUIZ_FILE, "r", encoding="utf-8") as f:
            return normalize_questions(json.load(f))
    except Exception:
        return None


# --------------------------------------------------------------------------
# STREAMLIT APP
# --------------------------------------------------------------------------

st.set_page_config(page_title="PDF → Quiz", page_icon="📝", layout="centered")

CUSTOM_CSS = """
<style>
.stApp { background-color: #f7f8fb; }
.quiz-card {
    background: white;
    border-radius: 14px;
    padding: 1.2rem 1.4rem;
    margin-bottom: 1rem;
    box-shadow: 0 1px 4px rgba(0,0,0,0.08);
    border: 1px solid #eaeaf0;
}
.quiz-card.correct { border-left: 6px solid #22c55e; }
.quiz-card.incorrect { border-left: 6px solid #ef4444; }
.answer-correct { color: #15803d; font-weight: 600; }
.answer-incorrect { color: #b91c1c; font-weight: 600; }
.badge {
    display:inline-block; padding: 2px 10px; border-radius: 999px;
    font-size: 0.75rem; font-weight: 700; margin-left: 8px;
}
.badge-correct { background:#dcfce7; color:#15803d; }
.badge-incorrect { background:#fee2e2; color:#b91c1c; }
.badge-unanswered { background:#f1f5f9; color:#475569; }
.unit-chip {
    display:inline-block; padding: 1px 10px; border-radius: 999px;
    font-size: 0.7rem; font-weight: 700; background:#eef2ff; color:#4338ca;
    margin-left: 8px;
}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# ---- session state defaults ----
defaults = {
    "questions": [],       # parsed master question bank
    "quiz_order": [],      # list of indices into questions, current order
    "responses": {},       # index -> selected letter
    "submitted": False,    # legacy flag, kept for compatibility
    "unit_submitted": {},  # unit name -> True once that unit's own Submit button was clicked
    "combined_logged": False,  # whether the "all units done" combined attempt was already logged
    "student_name": "",
    "selected_unit": "All Units",
    "last_score": None,    # (score, total, pct, unit_breakdown) for the last submission
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


def reset_quiz(shuffle=True, unit_filter=None):
    questions = st.session_state["questions"]
    if unit_filter and unit_filter != "All Units":
        order = [i for i, q in enumerate(questions) if q.get("unit", DEFAULT_UNIT) == unit_filter]
    else:
        order = list(range(len(questions)))
    if shuffle:
        random.shuffle(order)
    st.session_state["quiz_order"] = order
    st.session_state["responses"] = {}
    st.session_state["submitted"] = False
    st.session_state["unit_submitted"] = {}
    st.session_state["combined_logged"] = False
    st.session_state["last_score"] = None


def units_in_order(order, questions):
    """Unique unit names among the current quiz order, in first-seen order."""
    seen = []
    for qi in order:
        u = questions[qi].get("unit", DEFAULT_UNIT)
        if u not in seen:
            seen.append(u)
    return seen


def render_review_card(pos, qi, questions):
    """Render one color-coded, answered question card (used after a unit is submitted)."""
    q = questions[qi]
    selected = st.session_state["responses"].get(qi)
    is_correct = selected == q["answer"]
    card_class = "correct" if is_correct else "incorrect"
    badge = (
        "<span class='badge badge-correct'>Correct</span>"
        if is_correct
        else "<span class='badge badge-incorrect'>Incorrect</span>"
        if selected
        else "<span class='badge badge-unanswered'>Unanswered</span>"
    )
    rows = []
    for letter in q["option_order"]:
        text = q["options"][letter]
        if letter == q["answer"]:
            rows.append(f"<div class='answer-correct'>✅ {letter}. {text} (correct answer)</div>")
        elif letter == selected and not is_correct:
            rows.append(f"<div class='answer-incorrect'>❌ {letter}. {text} (your answer)</div>")
        else:
            rows.append(f"<div>{letter}. {text}</div>")
    explanation_html = ""
    if not is_correct and q.get("explanation"):
        explanation_html = f"<div style='margin-top:8px;color:#475569;'><i>💡 {q['explanation']}</i></div>"
    st.markdown(
        f"""<div class='quiz-card {card_class}'>
        <b>Q{pos+1}. {q['stem']}</b>{badge}<span class='unit-chip'>{q.get('unit', DEFAULT_UNIT)}</span>
        <div style='margin-top:10px;'>{''.join(rows)}</div>
        {explanation_html}
        </div>""",
        unsafe_allow_html=True,
    )


# ---- Auto-load the instructor's published quiz into brand-new sessions ----
# (each new browser tab / student PC starts with empty session_state, so
# without this a student would never see what the instructor prepared)
if not st.session_state.get("_bootstrapped"):
    st.session_state["_bootstrapped"] = True
    if not st.session_state["questions"]:
        shared = load_current_quiz()
        if shared:
            st.session_state["questions"] = shared
            reset_quiz(shuffle=True)


st.title("📝 PDF → Quiz Generator")
st.caption("Upload a question-bank PDF, review the parsed questions, then let students take a unit-wise quiz and track their scores.")

tab_build, tab_quiz, tab_dash = st.tabs(
    ["📄 Build Quiz (Instructor)", "🎓 Take Quiz (Student)", "📊 Dashboard"]
)

# =========================================================================
# TAB 1: BUILD / UPLOAD / REVIEW
# =========================================================================
with tab_build:
    st.subheader("1. Upload a PDF")
    uploaded = st.file_uploader("Choose a PDF with MCQs and an answer key", type=["pdf"])

    col_a, col_b = st.columns(2)
    with col_a:
        parse_clicked = st.button("🔍 Parse Questions", type="primary", disabled=uploaded is None)
    with col_b:
        uploaded_json = st.file_uploader("...or load a saved quiz (.json)", type=["json"], key="json_up")

    if parse_clicked and uploaded is not None:
        with st.spinner("Reading and parsing PDF..."):
            try:
                text = extract_text_from_pdf(uploaded.read())
                qs = normalize_questions(parse_questions(text))
                if not qs:
                    st.error(
                        "No questions could be detected. The PDF may be a scanned "
                        "image (no selectable text) or use an unusual layout — "
                        "try the manual JSON option below."
                    )
                else:
                    st.session_state["questions"] = qs
                    reset_quiz(shuffle=False)
                    n_units = len(get_units(qs))
                    st.success(
                        f"Parsed {len(qs)} question(s) across {n_units} unit(s)/topic(s). "
                        "Review them below, then start the quiz."
                    )
            except Exception as e:
                st.error(f"Error parsing PDF: {e}")

    if uploaded_json is not None:
        try:
            data = json.loads(uploaded_json.read().decode("utf-8"))
            st.session_state["questions"] = normalize_questions(data)
            reset_quiz(shuffle=False)
            st.success(f"Loaded {len(data)} question(s) from JSON.")
        except Exception as e:
            st.error(f"Invalid JSON file: {e}")

    if st.session_state["questions"]:
        st.subheader("2. Review & fix parsed questions")
        st.caption(
            "Fix question text, option text, the correct answer, or which unit/topic "
            "a question belongs to. Add missing options, delete junk entries, or add "
            "a brand-new question by hand."
        )

        existing_units = get_units(st.session_state["questions"])
        with st.expander("⚡ Bulk-assign a unit to ALL questions at once"):
            bulk_unit = st.text_input("Unit / topic name", key="bulk_unit_input", placeholder="e.g. Unit 1: Basics")
            if st.button("Apply to all questions", key="bulk_apply") and bulk_unit.strip():
                for q in st.session_state["questions"]:
                    q["unit"] = bulk_unit.strip()
                st.rerun()

        for i, q in enumerate(st.session_state["questions"]):
            bad_answer = q["answer"] and q["answer"] not in q["option_order"]
            flag = " ⚠️" if (not q["answer"] or bad_answer) else ""
            title = f"Q{i+1} [{q.get('unit', DEFAULT_UNIT)}]: {q['stem'][:55]}{'...' if len(q['stem'])>55 else ''}{flag}"
            with st.expander(title):
                q["stem"] = st.text_area("Question", value=q["stem"], key=f"stem_{i}")

                q["unit"] = st.text_input(
                    "Unit / Topic", value=q.get("unit", DEFAULT_UNIT), key=f"unit_{i}"
                ).strip() or DEFAULT_UNIT

                for letter in list(q["option_order"]):
                    opt_col, del_col = st.columns([5, 1])
                    with opt_col:
                        q["options"][letter] = st.text_input(
                            f"Option {letter}", value=q["options"][letter], key=f"opt_{i}_{letter}"
                        )
                    with del_col:
                        st.write("")  # align button with the text input
                        can_delete = len(q["option_order"]) > 2
                        if st.button("🗑️", key=f"delopt_{i}_{letter}", disabled=not can_delete,
                                     help="Remove this option" if can_delete else "A question needs at least 2 options"):
                            q["option_order"].remove(letter)
                            del q["options"][letter]
                            if q["answer"] == letter:
                                q["answer"] = ""
                            st.rerun()

                add_col, _ = st.columns([1, 3])
                with add_col:
                    if st.button("➕ Add option", key=f"addopt_{i}"):
                        next_letter = None
                        for cand in "ABCDEFGH":
                            if cand not in q["option_order"]:
                                next_letter = cand
                                break
                        if next_letter:
                            q["option_order"].append(next_letter)
                            q["options"][next_letter] = ""
                        st.rerun()

                q["answer"] = st.text_input(
                    "Correct answer (letter)", value=q["answer"] or "", key=f"ans_{i}"
                ).strip().upper()
                if bad_answer:
                    st.error(f"Answer '{q['answer']}' doesn't match any option ({', '.join(q['option_order'])}).")
                elif not q["answer"]:
                    st.warning("No correct answer set yet.")

                q["explanation"] = st.text_area(
                    "Explanation (optional, shown to students who get it wrong)",
                    value=q.get("explanation", ""), key=f"exp_{i}"
                )
                if st.button(f"🗑️ Delete Q{i+1}", key=f"del_{i}"):
                    st.session_state["questions"].pop(i)
                    reset_quiz(shuffle=False)
                    st.rerun()

        if st.button("➕ Add a new question"):
            st.session_state["questions"].append({
                "number": str(len(st.session_state["questions"]) + 1),
                "stem": "",
                "options": {"A": "", "B": "", "C": "", "D": ""},
                "option_order": ["A", "B", "C", "D"],
                "answer": "",
                "explanation": "",
                "unit": existing_units[0] if existing_units else DEFAULT_UNIT,
            })
            reset_quiz(shuffle=False)
            st.rerun()

        missing_answers = [
            i + 1 for i, q in enumerate(st.session_state["questions"])
            if not q["answer"] or q["answer"] not in q["option_order"]
        ]
        if missing_answers:
            st.warning(f"These questions need a valid correct answer before you can start the quiz: {missing_answers}")

        st.subheader("3. Save or launch")
        c1, c2 = st.columns(2)
        with c1:
            quiz_json = json.dumps(st.session_state["questions"], indent=2)
            st.download_button(
                "💾 Download quiz as JSON (reuse later)",
                data=quiz_json,
                file_name="quiz_bank.json",
                mime="application/json",
            )
        with c2:
            if st.button("🚀 Publish Quiz to Students →", type="primary", disabled=bool(missing_answers)):
                reset_quiz(shuffle=True)
                published = publish_current_quiz(st.session_state["questions"])
                if published:
                    st.success(
                        "Quiz published! Every student connected to this server on the LAN "
                        "will now see it in their 'Take Quiz' tab (they may need to click "
                        "'🔄 Refresh quiz from instructor' if they already had the page open)."
                    )
                else:
                    st.warning(
                        "Quiz is ready in YOUR browser, but couldn't be saved to disk to share "
                        "with other machines. Students on other PCs won't see it automatically — "
                        "use the 'Download quiz as JSON' button and have them load that file instead."
                    )

# =========================================================================
# TAB 2: TAKE QUIZ
# =========================================================================
with tab_quiz:
    questions = st.session_state["questions"]

    refresh_col1, refresh_col2 = st.columns([3, 1])
    with refresh_col2:
        if st.button("🔄 Refresh quiz from instructor"):
            shared = load_current_quiz()
            if shared:
                st.session_state["questions"] = shared
                reset_quiz(shuffle=True)
                st.success("Loaded the latest published quiz.")
                st.rerun()
            else:
                st.warning("No published quiz found yet — ask your instructor to click 'Publish Quiz to Students'.")

    if not questions:
        with refresh_col1:
            st.info(
                "No quiz loaded yet. Either your instructor hasn't published one yet "
                "(click 🔄 Refresh above once they have), or if you're the instructor: "
                "go to **Build Quiz** → upload a PDF → **Publish Quiz to Students**."
            )
    else:
        units = ["All Units"] + get_units(questions)

        st.subheader("Quiz setup")
        s1, s2 = st.columns([1, 1])
        with s1:
            st.session_state["student_name"] = st.text_input(
                "Your name", value=st.session_state["student_name"], placeholder="e.g. Priya Sharma"
            )
        with s2:
            st.session_state["selected_unit"] = st.selectbox(
                "Unit / Topic", options=units,
                index=units.index(st.session_state["selected_unit"]) if st.session_state["selected_unit"] in units else 0,
            )

        if st.button("▶️ Start / Restart Quiz", type="primary"):
            reset_quiz(shuffle=True, unit_filter=st.session_state["selected_unit"])
            st.rerun()

        order = st.session_state["quiz_order"]
        total = len(order)

        if total == 0:
            st.info("Click **Start / Restart Quiz** above to begin.")
        else:
            unit_names = units_in_order(order, questions)
            st.markdown("---")
            st.subheader(f"Quiz — {st.session_state['selected_unit']} · {total} question(s) · {len(unit_names)} unit(s)")

            # ---- One section per unit, each with its own Submit button ----
            for u in unit_names:
                qidxs = [qi for qi in order if questions[qi].get("unit", DEFAULT_UNIT) == u]
                is_unit_submitted = st.session_state["unit_submitted"].get(u, False)

                st.markdown(f"### 📘 {u}  <span class='unit-chip'>{len(qidxs)} question(s)</span>", unsafe_allow_html=True)

                if not is_unit_submitted:
                    with st.form(f"quiz_form_{u}"):
                        for pos, qi in enumerate(qidxs):
                            q = questions[qi]
                            st.markdown(f"<div class='quiz-card'><b>Q{pos+1}. {q['stem']}</b></div>", unsafe_allow_html=True)
                            letters = q["option_order"]
                            prev = st.session_state["responses"].get(qi)
                            default_index = letters.index(prev) if prev in letters else None
                            choice = st.radio(
                                "Select one:",
                                options=letters,
                                format_func=lambda letter, _q=q: f"{letter}. {_q['options'][letter]}",
                                index=default_index,
                                key=f"radio_{qi}",
                                label_visibility="collapsed",
                            )
                            st.session_state["responses"][qi] = choice
                            st.write("")  # spacing
                        submit_unit = st.form_submit_button(f"✅ Submit {u}", type="primary")
                    if submit_unit:
                        st.session_state["unit_submitted"][u] = True
                        u_correct = sum(
                            1 for qi in qidxs if st.session_state["responses"].get(qi) == questions[qi]["answer"]
                        )
                        u_total = len(qidxs)
                        log_attempt(
                            st.session_state["student_name"], u,
                            u_correct, u_total, (u_correct / u_total if u_total else 0),
                            {u: {"correct": u_correct, "total": u_total}},
                        )
                        st.rerun()
                else:
                    u_correct = sum(
                        1 for qi in qidxs if st.session_state["responses"].get(qi) == questions[qi]["answer"]
                    )
                    u_total = len(qidxs)
                    u_pct = u_correct / u_total if u_total else 0
                    m1, m2 = st.columns(2)
                    m1.metric(f"{u} score", f"{u_correct} / {u_total}")
                    m2.metric("Percentage", f"{u_pct*100:.0f}%")
                    st.progress(u_pct)
                    for pos, qi in enumerate(qidxs):
                        render_review_card(pos, qi, questions)
                    if st.button(f"🔁 Retake {u} only", key=f"retake_unit_{u}"):
                        for qi in qidxs:
                            st.session_state["responses"].pop(qi, None)
                        st.session_state["unit_submitted"][u] = False
                        st.rerun()
                st.markdown("---")

            all_done = all(st.session_state["unit_submitted"].get(u, False) for u in unit_names)

            if all_done:
                correct_count = sum(
                    1 for qi in order if st.session_state["responses"].get(qi) == questions[qi]["answer"]
                )
                pct = correct_count / total if total else 0
                unit_stats = {}
                for qi in order:
                    u = questions[qi].get("unit", DEFAULT_UNIT)
                    unit_stats.setdefault(u, [0, 0])
                    unit_stats[u][1] += 1
                    if st.session_state["responses"].get(qi) == questions[qi]["answer"]:
                        unit_stats[u][0] += 1

                if not st.session_state["combined_logged"] and len(unit_names) > 1:
                    unit_breakdown = {u: {"correct": c, "total": t} for u, (c, t) in unit_stats.items()}
                    log_attempt(
                        st.session_state["student_name"], "All Units (combined)",
                        correct_count, total, pct, unit_breakdown,
                    )
                    st.session_state["combined_logged"] = True

                st.subheader("📊 Overall Results Dashboard")
                m1, m2, m3 = st.columns(3)
                m1.metric("Total score", f"{correct_count} / {total}")
                m2.metric("Percentage", f"{pct*100:.0f}%")
                m3.metric("Unit(s) covered", str(len(unit_stats)))
                st.progress(pct)

                if len(unit_stats) > 1:
                    st.markdown("**Unit-wise score points**")
                    chart_data = {u: (c / t * 100 if t else 0) for u, (c, t) in unit_stats.items()}
                    st.bar_chart(chart_data)
                    for u, (c, t) in unit_stats.items():
                        st.write(f"- **{u}**: {c}/{t} correct ({c/t*100:.0f}%)")

                st.markdown("---")
                if st.button("🔄 Retake Entire Quiz (shuffled)", type="primary"):
                    reset_quiz(shuffle=True, unit_filter=st.session_state["selected_unit"])
                    st.rerun()

# =========================================================================
# TAB 3: DASHBOARD
# =========================================================================
with tab_dash:
    st.subheader("📊 Score Dashboard")
    st.caption("Every submitted quiz attempt is recorded here (stored locally next to app.py).")

    attempts = load_attempts()

    if not attempts:
        st.info("No quiz attempts recorded yet. Submit a quiz in the **Take Quiz** tab to see it here.")
    else:
        all_names = sorted({a["student_name"] for a in attempts})
        f1, f2 = st.columns(2)
        with f1:
            name_filter = st.selectbox("Filter by student", options=["All students"] + all_names)
        with f2:
            all_units_seen = sorted({a["unit_filter"] for a in attempts})
            unit_filter_dash = st.selectbox("Filter by unit taken", options=["All"] + all_units_seen)

        filtered = attempts
        if name_filter != "All students":
            filtered = [a for a in filtered if a["student_name"] == name_filter]
        if unit_filter_dash != "All":
            filtered = [a for a in filtered if a["unit_filter"] == unit_filter_dash]

        if not filtered:
            st.warning("No attempts match this filter.")
        else:
            pcts = [float(a["percentage"]) for a in filtered]
            m1, m2, m3 = st.columns(3)
            m1.metric("Attempts", len(filtered))
            m2.metric("Average score", f"{sum(pcts)/len(pcts):.0f}%")
            m3.metric("Best score", f"{max(pcts):.0f}%")

            st.markdown("**Score over time**")
            time_series = {a["timestamp"]: float(a["percentage"]) for a in filtered}
            st.line_chart(time_series)

            # Aggregate unit-wise performance across all attempts in the filter
            unit_totals = {}
            for a in filtered:
                for u, stats in a.get("unit_breakdown", {}).items():
                    unit_totals.setdefault(u, [0, 0])
                    unit_totals[u][0] += stats.get("correct", 0)
                    unit_totals[u][1] += stats.get("total", 0)
            if unit_totals:
                st.markdown("**Average performance by unit**")
                unit_pct = {u: (c / t * 100 if t else 0) for u, (c, t) in unit_totals.items()}
                st.bar_chart(unit_pct)

            st.markdown("**Attempt history**")
            table_rows = [
                {
                    "Timestamp": a["timestamp"],
                    "Student": a["student_name"],
                    "Unit": a["unit_filter"],
                    "Score": f"{a['score']}/{a['total']}",
                    "Percentage": f"{a['percentage']}%",
                }
                for a in sorted(filtered, key=lambda x: x["timestamp"], reverse=True)
            ]
            st.table(table_rows)

        st.markdown("---")
        with st.expander("🧹 Instructor: clear all recorded attempts"):
            st.warning("This permanently deletes every recorded attempt for every student.")
            confirm = st.checkbox("I understand this cannot be undone")
            if st.button("Delete all history", disabled=not confirm):
                try:
                    os.remove(ATTEMPTS_FILE)
                    st.success("History cleared.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not clear history: {e}")
